"""Async control-plane client for the independent signed Tool Runner process."""

import asyncio
import base64
import logging
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import ToolIdempotency
from deskpilot.runner.ipc_codec import (
    DEFAULT_MAX_FRAME_BYTES,
    BootstrapCodec,
    NdjsonIpcCodec,
)
from deskpilot.runner.ipc_protocol import (
    IpcPayload,
    IpcProtocolError,
    IpcSigner,
    IpcVerifier,
    RunnerBootstrap,
    RunnerHeartbeat,
    RunnerHello,
    SignedIpcEnvelope,
    ToolCallRequest,
    ToolCallResult,
    ToolCancelRequest,
    ToolCommitReceiptRequest,
    ToolCommitReceiptResult,
    ToolProgress,
    UnexpectedMessageError,
)
from deskpilot.runner.worker_runtime import prepare_worker_runtime

logger = logging.getLogger(__name__)


class RunnerClientError(RuntimeError):
    code = "RUNNER_CLIENT_ERROR"


class RunnerStartupError(RunnerClientError):
    code = "RUNNER_STARTUP_FAILED"


class RunnerUnavailableError(RunnerClientError):
    code = "RUNNER_UNAVAILABLE"


class RunnerExitedError(RunnerUnavailableError):
    code = "RUNNER_EXITED"


class RunnerHeartbeatTimeoutError(RunnerUnavailableError):
    code = "RUNNER_HEARTBEAT_TIMEOUT"


class RunnerCallTimeoutError(RunnerClientError):
    code = "RUNNER_CALL_TIMEOUT"


class RunnerProtocolViolationError(RunnerClientError):
    code = "RUNNER_PROTOCOL_VIOLATION"


class RunnerCallConflictError(RunnerClientError):
    code = "RUNNER_CALL_ID_CONFLICT"


ProgressCallback = Callable[[ToolProgress], None]


@dataclass(slots=True)
class _PendingCall:
    future: asyncio.Future[ToolCallResult]
    progress_callback: ProgressCallback | None
    tool_name: str
    tool_version: str


class RunnerClient:
    def __init__(
        self,
        *,
        registry: ToolRegistry,
        command: Sequence[str] | None = None,
        heartbeat_interval_seconds: float = 0.5,
        heartbeat_timeout_seconds: float = 3.0,
        startup_timeout_seconds: float = 10.0,
        shutdown_timeout_seconds: float = 2.0,
        call_transport_grace_seconds: float = 2.0,
        require_windows_sandbox: bool | None = None,
        require_network_isolation: bool = False,
        worker_runtime_root: str = "./data/worker-runtime",
        appcontainer_profile_journal_path: str = (
            "./data/runner/appcontainer-profiles.json"
        ),
        commit_receipt_database_path: str = "./data/runner/commit-receipts.db",
        worker_memory_limit_bytes: int = 268_435_456,
        worker_active_process_limit: int = 1,
    ) -> None:
        if heartbeat_interval_seconds < 0.1:
            raise ValueError("Runner heartbeat interval must be at least 0.1 seconds")
        if heartbeat_timeout_seconds <= heartbeat_interval_seconds:
            raise ValueError("Runner heartbeat timeout must exceed its interval")
        self._registry = registry
        self._command = tuple(command or (sys.executable, "-m", "deskpilot.runner.service"))
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._heartbeat_timeout_seconds = heartbeat_timeout_seconds
        self._startup_timeout_seconds = startup_timeout_seconds
        self._shutdown_timeout_seconds = shutdown_timeout_seconds
        self._call_transport_grace_seconds = call_transport_grace_seconds
        self._require_windows_sandbox = (
            os.name == "nt" if require_windows_sandbox is None else require_windows_sandbox
        )
        self._require_network_isolation = require_network_isolation
        self._worker_runtime_root = worker_runtime_root
        self._appcontainer_profile_journal_path = appcontainer_profile_journal_path
        self._commit_receipt_database_path = commit_receipt_database_path
        self._worker_memory_limit_bytes = worker_memory_limit_bytes
        self._worker_active_process_limit = worker_active_process_limit
        self._codec = NdjsonIpcCodec()
        self._bootstrap_codec = BootstrapCodec()
        self._process: asyncio.subprocess.Process | None = None
        self._signer: IpcSigner | None = None
        self._verifier: IpcVerifier | None = None
        self._startup_nonce: str | None = None
        self._runner_id: str | None = None
        self._isolation_mode: str | None = None
        self._network_isolation_mode: str | None = None
        self._reader_task: asyncio.Task[None] | None = None
        self._stderr_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._write_lock = asyncio.Lock()
        self._pending: dict[str, _PendingCall] = {}
        self._receipt_queries: dict[
            str,
            asyncio.Future[ToolCommitReceipt | None],
        ] = {}
        self._last_heartbeat = 0.0
        self._heartbeat_count = 0
        self._failure: RunnerClientError | None = None
        self._failure_event = asyncio.Event()
        self._stopping = False

    @property
    def runner_id(self) -> str | None:
        return self._runner_id

    @property
    def process_id(self) -> int | None:
        return self._process.pid if self._process is not None else None

    @property
    def heartbeat_count(self) -> int:
        return self._heartbeat_count

    @property
    def isolation_mode(self) -> str | None:
        return self._isolation_mode

    @property
    def network_isolation_mode(self) -> str | None:
        return self._network_isolation_mode

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def failure(self) -> RunnerClientError | None:
        return self._failure

    async def start(self) -> None:
        if self.is_running:
            return
        self._stopping = False
        self._failure = None
        self._failure_event.clear()
        secret = secrets.token_bytes(32)
        key_id = f"control-{secrets.token_hex(8)}"
        startup_nonce = secrets.token_urlsafe(24)
        encoded_secret = base64.urlsafe_b64encode(secret).rstrip(b"=").decode("ascii")
        self._signer = IpcSigner(key_id=key_id, secret=secret)
        self._verifier = IpcVerifier(
            key_id=key_id,
            secret=secret,
            startup_nonce=startup_nonce,
        )
        self._startup_nonce = startup_nonce
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

        try:
            worker_runtime_bundle: str | None = None
            if self._require_network_isolation:
                bundle = await asyncio.to_thread(
                    prepare_worker_runtime,
                    Path(self._worker_runtime_root),
                )
                worker_runtime_bundle = str(bundle.root)
            self._process = await asyncio.create_subprocess_exec(
                *self._command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=DEFAULT_MAX_FRAME_BYTES + 1,
                creationflags=creation_flags,
            )
            bootstrap = RunnerBootstrap(
                key_id=key_id,
                secret=encoded_secret,
                startup_nonce=startup_nonce,
                heartbeat_interval_seconds=self._heartbeat_interval_seconds,
                require_windows_sandbox=self._require_windows_sandbox,
                require_network_isolation=self._require_network_isolation,
                worker_runtime_root=self._worker_runtime_root,
                worker_runtime_bundle=worker_runtime_bundle,
                appcontainer_profile_journal_path=(
                    self._appcontainer_profile_journal_path
                ),
                commit_receipt_database_path=self._commit_receipt_database_path,
                worker_memory_limit_bytes=self._worker_memory_limit_bytes,
                worker_active_process_limit=self._worker_active_process_limit,
            )
            await self._write_raw(self._bootstrap_codec.encode(bootstrap))
            hello_envelope = await asyncio.wait_for(
                self._read_envelope(),
                timeout=self._startup_timeout_seconds,
            )
            payload = self._require_verifier().verify(hello_envelope)
            if not isinstance(payload, RunnerHello):
                raise RunnerStartupError("Runner did not send runner.hello as its first response")
            if "deskpilot.runner.v1" not in payload.supported_protocols:
                raise RunnerStartupError("Runner does not support deskpilot.runner.v1")
            if not payload.per_call_process_isolation:
                raise RunnerStartupError("Runner does not isolate every Tool call in a process")
            if (
                self._require_windows_sandbox
                and payload.isolation_mode
                not in {"windows_restricted", "windows_appcontainer"}
            ):
                raise RunnerStartupError(
                    "Runner did not establish the required Windows security sandbox"
                )
            if (
                self._require_network_isolation
                and payload.network_isolation_mode != "appcontainer"
            ):
                raise RunnerStartupError(
                    "Runner did not establish the required OS network isolation"
                )
            self._runner_id = payload.runner_id
            self._isolation_mode = payload.isolation_mode
            self._network_isolation_mode = payload.network_isolation_mode
            self._last_heartbeat = time.monotonic()
            self._heartbeat_count = 0
            self._reader_task = asyncio.create_task(
                self._reader_loop(), name="runner-client-reader"
            )
            self._stderr_task = asyncio.create_task(
                self._stderr_loop(), name="runner-client-stderr"
            )
            self._watchdog_task = asyncio.create_task(
                self._watchdog_loop(), name="runner-client-watchdog"
            )
        except Exception as error:
            await self.stop()
            if isinstance(error, RunnerStartupError):
                raise
            raise RunnerStartupError(
                f"Failed to start Tool Runner: {type(error).__name__}: {error}"
            ) from error

    async def wait_for_failure(self) -> RunnerClientError:
        """Wait for this Runner generation's first terminal transport failure."""
        if self._failure is None:
            await self._failure_event.wait()
        failure = self._failure
        if failure is None:
            raise RuntimeError("Runner failure notification had no failure")
        return failure

    async def stop(self) -> None:
        self._stopping = True
        stopped_error = RunnerUnavailableError("Tool Runner is stopping")
        self._fail_pending(stopped_error)
        process = self._process
        watchdog = self._watchdog_task
        if watchdog is not None and watchdog is not asyncio.current_task():
            watchdog.cancel()
            await asyncio.gather(watchdog, return_exceptions=True)
        self._watchdog_task = None
        if process is not None and process.stdin is not None:
            try:
                process.stdin.close()
                await process.stdin.wait_closed()
            except (BrokenPipeError, ConnectionError):
                pass
        if process is not None and process.returncode is None:
            try:
                await asyncio.wait_for(
                    process.wait(), timeout=self._shutdown_timeout_seconds
                )
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=1)
                except TimeoutError:
                    process.kill()
                    await process.wait()

        current = asyncio.current_task()
        io_tasks = [
            task
            for task in (self._reader_task, self._stderr_task)
            if task is not None and task is not current
        ]
        if io_tasks:
            _, unfinished = await asyncio.wait(io_tasks, timeout=0.5)
            for task in unfinished:
                task.cancel()
            await asyncio.gather(*io_tasks, return_exceptions=True)
        elif process is not None:
            drains = []
            if process.stdout is not None:
                drains.append(process.stdout.read())
            if process.stderr is not None:
                drains.append(process.stderr.read())
            if drains:
                await asyncio.gather(*drains, return_exceptions=True)

        background = [task for task in io_tasks if not task.done()]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._reader_task = None
        self._stderr_task = None
        self._process = None
        self._signer = None
        self._verifier = None
        self._startup_nonce = None
        self._runner_id = None
        self._isolation_mode = None

    async def call_tool(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, object],
        actor: str,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        expected_resource_versions: dict[str, str] | None = None,
        authorization: ToolAuthorizationGrant,
        progress_callback: ProgressCallback | None = None,
    ) -> ToolCallResult:
        self._ensure_available()
        registration = self._registry.resolve(tool_name, tool_version)
        parsed_arguments = self._registry.validate_input(
            tool_name, tool_version, arguments
        )
        if (
            registration.contract.execution.idempotency is ToolIdempotency.KEY_REQUIRED
            and idempotency_key is None
        ):
            raise ValueError("Tool Contract requires an idempotency key")

        resolved_call_id = call_id or f"call-{secrets.token_hex(16)}"
        if resolved_call_id in self._pending:
            raise RunnerCallConflictError(f"Runner call is already pending: {resolved_call_id}")
        now = datetime.now(UTC)
        ttl_seconds = min(
            60,
            max(5, registration.contract.execution.timeout_seconds + 5),
        )
        request = ToolCallRequest(
            call_id=resolved_call_id,
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            tool_version=tool_version,
            contract_digest=registration.contract.digest,
            arguments=parsed_arguments.model_dump(mode="json"),
            actor=actor,
            idempotency_key=idempotency_key,
            expected_resource_versions=expected_resource_versions or {},
            authorization=authorization,
            issued_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            nonce=secrets.token_urlsafe(24),
            startup_nonce=self._require_startup_nonce(),
        )
        future = asyncio.get_running_loop().create_future()
        self._pending[resolved_call_id] = _PendingCall(
            future,
            progress_callback,
            tool_name,
            tool_version,
        )
        try:
            await self._send_payload(request)
            return await asyncio.wait_for(
                future,
                timeout=(
                    registration.contract.execution.timeout_seconds
                    + self._call_transport_grace_seconds
                ),
            )
        except TimeoutError as error:
            self._pending.pop(resolved_call_id, None)
            await self._best_effort_cancel(resolved_call_id, "Control-plane timeout")
            raise RunnerCallTimeoutError(
                f"Runner call timed out: {resolved_call_id}"
            ) from error
        except asyncio.CancelledError:
            self._pending.pop(resolved_call_id, None)
            await self._best_effort_cancel(resolved_call_id, "Caller cancelled")
            raise
        except Exception:
            self._pending.pop(resolved_call_id, None)
            raise

    async def cancel_call(self, call_id: str, reason: str) -> None:
        self._ensure_available()
        now = datetime.now(UTC)
        request = ToolCancelRequest(
            call_id=call_id,
            reason=reason,
            issued_at=now,
            expires_at=now + timedelta(seconds=10),
            nonce=secrets.token_urlsafe(24),
            startup_nonce=self._require_startup_nonce(),
        )
        await self._send_payload(request)

    async def get_commit_receipt(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ToolCommitReceipt | None:
        """Query durable Runner evidence without replaying the Tool call."""
        self._ensure_available()
        if call_id in self._receipt_queries:
            raise RunnerCallConflictError(
                f"Runner commit receipt query is already pending: {call_id}"
            )
        now = datetime.now(UTC)
        request = ToolCommitReceiptRequest(
            call_id=call_id,
            issued_at=now,
            expires_at=now + timedelta(seconds=min(10, max(1, timeout_seconds))),
            nonce=secrets.token_urlsafe(24),
            startup_nonce=self._require_startup_nonce(),
        )
        future: asyncio.Future[ToolCommitReceipt | None] = (
            asyncio.get_running_loop().create_future()
        )
        self._receipt_queries[call_id] = future
        try:
            await self._send_payload(request)
            return await asyncio.wait_for(future, timeout=timeout_seconds)
        except TimeoutError as error:
            raise RunnerCallTimeoutError(
                f"Runner commit receipt query timed out: {call_id}"
            ) from error
        finally:
            self._receipt_queries.pop(call_id, None)

    async def _best_effort_cancel(self, call_id: str, reason: str) -> None:
        try:
            await self.cancel_call(call_id, reason)
        except (RunnerClientError, IpcProtocolError, BrokenPipeError, ConnectionError):
            pass

    async def _reader_loop(self) -> None:
        try:
            while True:
                envelope = await self._read_envelope()
                payload = self._require_verifier().verify(envelope)
                self._validate_runner_identity(payload)
                if isinstance(payload, RunnerHeartbeat):
                    self._last_heartbeat = time.monotonic()
                    self._heartbeat_count += 1
                elif isinstance(payload, ToolProgress):
                    pending = self._pending.get(payload.call_id)
                    if pending is not None and pending.progress_callback is not None:
                        try:
                            pending.progress_callback(payload)
                        except Exception:
                            logger.exception("Runner progress callback failed")
                elif isinstance(payload, ToolCallResult):
                    pending = self._pending.get(payload.call_id)
                    if pending is None or pending.future.done():
                        continue
                    if payload.status == "succeeded" and payload.output is not None:
                        try:
                            self._registry.validate_output(
                                pending.tool_name,
                                pending.tool_version,
                                payload.output,
                            )
                        except Exception as error:
                            raise RunnerProtocolViolationError(
                                "Runner result failed the control-plane output Schema"
                            ) from error
                    self._pending.pop(payload.call_id, None)
                    pending.future.set_result(payload)
                elif isinstance(payload, ToolCommitReceiptResult):
                    future = self._receipt_queries.pop(payload.call_id, None)
                    if future is not None and not future.done():
                        future.set_result(payload.receipt)
                else:
                    raise UnexpectedMessageError(
                        "Runner sent a control-plane command or duplicate hello"
                    )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not self._stopping:
                if isinstance(error, RunnerClientError):
                    client_error = error
                elif isinstance(error, IpcProtocolError):
                    client_error = RunnerProtocolViolationError(
                        f"Runner sent an invalid response: {error.code}"
                    )
                else:
                    client_error = RunnerExitedError(
                        f"Runner response stream ended: {type(error).__name__}"
                    )
                self._notify_failure(client_error)
                await self._terminate_process()

    async def _stderr_loop(self) -> None:
        process = self._require_process()
        if process.stderr is None:
            return
        while True:
            line = await process.stderr.readline()
            if not line:
                return
            logger.warning(
                "Tool Runner diagnostic: %s",
                line.decode("utf-8", errors="replace").strip()[:500],
            )

    async def _watchdog_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                if time.monotonic() - self._last_heartbeat > self._heartbeat_timeout_seconds:
                    error = RunnerHeartbeatTimeoutError("Tool Runner heartbeat timed out")
                    self._notify_failure(error)
                    await self._terminate_process()
                    return
        except asyncio.CancelledError:
            raise

    async def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except TimeoutError:
            process.kill()
            await process.wait()

    async def _read_envelope(self) -> SignedIpcEnvelope:
        process = self._require_process()
        if process.stdout is None:
            raise RunnerExitedError("Tool Runner stdout is unavailable")
        frame = await process.stdout.readline()
        if not frame:
            return_code = await process.wait()
            raise RunnerExitedError(f"Tool Runner exited with code {return_code}")
        return self._codec.decode(frame)

    async def _send_payload(self, payload: IpcPayload) -> None:
        envelope = self._require_signer().sign(payload)
        await self._write_raw(self._codec.encode(envelope))

    async def _write_raw(self, frame: bytes) -> None:
        process = self._require_process()
        if process.stdin is None:
            raise RunnerUnavailableError("Tool Runner stdin is unavailable")
        async with self._write_lock:
            process.stdin.write(frame)
            try:
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionError) as error:
                client_error = RunnerExitedError("Tool Runner command pipe is closed")
                if not self._stopping:
                    self._notify_failure(client_error)
                raise client_error from error

    def _validate_runner_identity(self, payload: IpcPayload) -> None:
        runner_id = getattr(payload, "runner_id", None)
        if runner_id != self._runner_id:
            raise RunnerProtocolViolationError("Runner response identity changed")

    def _fail_pending(self, error: RunnerClientError) -> None:
        pending_calls = tuple(self._pending.values())
        self._pending.clear()
        for pending in pending_calls:
            if not pending.future.done():
                pending.future.set_exception(error)
        receipt_queries = tuple(self._receipt_queries.values())
        self._receipt_queries.clear()
        for future in receipt_queries:
            if not future.done():
                future.set_exception(error)

    def _notify_failure(self, error: RunnerClientError) -> RunnerClientError:
        if self._failure is None:
            self._failure = error
            self._fail_pending(error)
            self._failure_event.set()
        return self._failure

    def _ensure_available(self) -> None:
        if self._failure is not None:
            raise self._failure
        if self._process is not None and self._process.returncode is not None:
            error = RunnerExitedError(
                f"Tool Runner exited with code {self._process.returncode}"
            )
            raise self._notify_failure(error)
        if not self.is_running or self._runner_id is None:
            raise RunnerUnavailableError("Tool Runner is not running")

    def _require_process(self) -> asyncio.subprocess.Process:
        if self._process is None:
            raise RunnerUnavailableError("Tool Runner process is unavailable")
        return self._process

    def _require_signer(self) -> IpcSigner:
        if self._signer is None:
            raise RunnerUnavailableError("Tool Runner signing session is unavailable")
        return self._signer

    def _require_verifier(self) -> IpcVerifier:
        if self._verifier is None:
            raise RunnerUnavailableError("Tool Runner verification session is unavailable")
        return self._verifier

    def _require_startup_nonce(self) -> str:
        if self._startup_nonce is None:
            raise RunnerUnavailableError("Tool Runner startup nonce is unavailable")
        return self._startup_nonce
