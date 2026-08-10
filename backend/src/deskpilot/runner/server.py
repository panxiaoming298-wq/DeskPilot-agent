"""Synchronous isolated Runner server using signed NDJSON over stdin/stdout."""

import base64
import binascii
import sys
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from queue import Empty, Queue
from threading import Event, Thread
from typing import BinaryIO, Literal, TextIO
from uuid import uuid4

from pydantic import BaseModel, JsonValue

from deskpilot.application.tool_registry import (
    ToolRegistryError,
    ToolSchemaValidationError,
)
from deskpilot.domain.tool_contracts import ToolCommitProtocol, ToolIdempotency
from deskpilot.runner.authorization import (
    MissingIdempotencyKeyError,
    MissingPolicyAuthorizationError,
    PolicyAuthorizationExpiredError,
    PolicyAuthorizationMismatchError,
    ToolCallAuthorizer,
    ToolContractMismatchError,
)
from deskpilot.runner.controlled_commit import (
    ControlledCommitBoundary,
    ControlledCommitPhase,
)
from deskpilot.runner.executor import ToolExecutionCancelledError, ToolExecutor
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
    ToolError,
    ToolProgress,
    UnexpectedMessageError,
)
from deskpilot.runner.isolated_executor import IsolatedToolExecutor
from deskpilot.runner.process_isolation import IsolationPolicy, ProcessIsolationError

MAX_ACTIVE_CALLS = 8


@dataclass(frozen=True, slots=True)
class _IncomingFrame:
    frame: bytes


@dataclass(frozen=True, slots=True)
class _CallCompleted:
    call_id: str
    future: Future[BaseModel]


@dataclass(frozen=True, slots=True)
class _InputClosed:
    pass


_ServerEvent = _IncomingFrame | _CallCompleted | _InputClosed


@dataclass(slots=True)
class _ActiveCall:
    request: ToolCallRequest
    future: Future[BaseModel]
    cancellation: Event
    started_at: datetime
    deadline: float
    idempotency: ToolIdempotency
    commit_boundary: ControlledCommitBoundary | None


def decode_bootstrap_secret(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


class RunnerServer:
    def __init__(
        self,
        *,
        bootstrap: RunnerBootstrap,
        executor: ToolExecutor,
        worker_factory: str,
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: TextIO,
    ) -> None:
        secret = decode_bootstrap_secret(bootstrap.secret)
        self._bootstrap = bootstrap
        self._executor = IsolatedToolExecutor(
            executor=executor,
            worker_factory=worker_factory,
            policy=IsolationPolicy(
                require_windows_sandbox=bootstrap.require_windows_sandbox,
                require_network_isolation=bootstrap.require_network_isolation,
                worker_runtime_root=bootstrap.worker_runtime_root,
                worker_runtime_bundle=bootstrap.worker_runtime_bundle,
                appcontainer_profile_journal_path=(
                    bootstrap.appcontainer_profile_journal_path
                ),
                memory_limit_bytes=bootstrap.worker_memory_limit_bytes,
                active_process_limit=bootstrap.worker_active_process_limit,
            ),
            commit_receipt_database_path=bootstrap.commit_receipt_database_path,
        )
        self._stdin = stdin
        self._stdout = stdout
        self._stderr = stderr
        self._codec = NdjsonIpcCodec()
        self._signer = IpcSigner(key_id=bootstrap.key_id, secret=secret)
        self._verifier = IpcVerifier(
            key_id=bootstrap.key_id,
            secret=secret,
            startup_nonce=bootstrap.startup_nonce,
        )
        self._authorizer = ToolCallAuthorizer(
            verifier=self._verifier,
            registry=executor.registry,
        )
        self._runner_id = f"runner-{uuid4().hex}"
        self._events: Queue[_ServerEvent] = Queue()
        self._active: dict[str, _ActiveCall] = {}
        self._seen_call_ids: set[str] = set()
        self._pool = ThreadPoolExecutor(
            max_workers=4,
            thread_name_prefix="deskpilot-tool",
        )

    def run(self) -> int:
        self._send_payload(
            RunnerHello(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                supported_protocols=(self._bootstrap.protocol_version,),
                isolation_mode=self._executor.isolation_mode.value,
                network_isolation_mode=self._executor.network_isolation_mode.value,
                per_call_process_isolation=True,
                occurred_at=datetime.now(UTC),
            )
        )
        reader = Thread(target=self._read_stdin, name="runner-stdin", daemon=True)
        reader.start()
        next_heartbeat = time.monotonic() + self._bootstrap.heartbeat_interval_seconds

        try:
            while True:
                now = time.monotonic()
                next_deadline = min(
                    (active.deadline for active in self._active.values()),
                    default=next_heartbeat,
                )
                wake_at = min(next_heartbeat, next_deadline)
                try:
                    event = self._events.get(timeout=max(0.0, wake_at - now))
                except Empty:
                    event = None

                if isinstance(event, _InputClosed):
                    return 0
                if isinstance(event, _IncomingFrame):
                    self._handle_frame(event.frame)
                elif isinstance(event, _CallCompleted):
                    self._handle_completion(event)

                current = time.monotonic()
                self._expire_calls(current)
                if current >= next_heartbeat:
                    self._send_heartbeat()
                    next_heartbeat = current + self._bootstrap.heartbeat_interval_seconds
        except (BrokenPipeError, OSError):
            return 1
        finally:
            for active in self._active.values():
                active.cancellation.set()
                active.future.cancel()
            self._active.clear()
            self._pool.shutdown(wait=True, cancel_futures=True)

    def _read_stdin(self) -> None:
        while True:
            frame = self._stdin.readline(DEFAULT_MAX_FRAME_BYTES + 1)
            if not frame:
                self._events.put(_InputClosed())
                return
            self._events.put(_IncomingFrame(frame))

    def _handle_frame(self, frame: bytes) -> None:
        try:
            envelope = self._codec.decode(frame)
            if isinstance(envelope.payload, ToolCallRequest):
                self._handle_call(envelope)
            elif isinstance(envelope.payload, ToolCancelRequest):
                payload = self._verifier.verify(envelope)
                if not isinstance(payload, ToolCancelRequest):
                    raise UnexpectedMessageError("Verified payload is not tool.cancel")
                self._handle_cancel(payload)
            elif isinstance(envelope.payload, ToolCommitReceiptRequest):
                payload = self._verifier.verify(envelope)
                if not isinstance(payload, ToolCommitReceiptRequest):
                    raise UnexpectedMessageError(
                        "Verified payload is not tool.commit_receipt.get"
                    )
                self._handle_commit_receipt_request(payload)
            else:
                self._verifier.verify(envelope)
                raise UnexpectedMessageError("Control plane sent a Runner-only response message")
        except IpcProtocolError as error:
            self._diagnose(error.code)

    def _handle_call(self, envelope: SignedIpcEnvelope) -> None:
        request = envelope.payload
        if not isinstance(request, ToolCallRequest):
            raise UnexpectedMessageError("Runner call handler received another message type")
        try:
            authorized = self._authorizer.authorize(envelope)
        except (
            ToolRegistryError,
            ToolSchemaValidationError,
            ToolContractMismatchError,
            MissingIdempotencyKeyError,
            MissingPolicyAuthorizationError,
            PolicyAuthorizationExpiredError,
            PolicyAuthorizationMismatchError,
        ) as error:
            self._send_failure(request.call_id, error)
            return

        if request.call_id in self._seen_call_ids:
            self._send_failure_code(
                request.call_id,
                code="TOOL_CALL_ID_REUSED",
                message="Runner call_id must be unique for one process lifetime",
            )
            return
        self._seen_call_ids.add(request.call_id)
        if len(self._active) >= MAX_ACTIVE_CALLS:
            self._send_failure_code(
                request.call_id,
                code="RUNNER_BUSY",
                message="Runner active call limit has been reached",
                retryable=True,
            )
            return

        cancellation = Event()
        started_at = datetime.now(UTC)
        commit_boundary = (
            ControlledCommitBoundary()
            if authorized.registration.contract.execution.commit_protocol
            is ToolCommitProtocol.BROKERED
            else None
        )
        future = self._pool.submit(
            self._executor.execute,
            authorized,
            cancellation,
            commit_boundary,
        )
        self._active[request.call_id] = _ActiveCall(
            request=request,
            future=future,
            cancellation=cancellation,
            started_at=started_at,
            deadline=time.monotonic()
            + authorized.registration.contract.execution.timeout_seconds,
            idempotency=authorized.registration.contract.execution.idempotency,
            commit_boundary=commit_boundary,
        )
        future.add_done_callback(partial(self._queue_completion, request.call_id))
        self._send_payload(
            ToolProgress(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                call_id=request.call_id,
                sequence=0,
                message="Tool execution started",
                percent=0,
                occurred_at=started_at,
            )
        )

    def _handle_cancel(self, request: ToolCancelRequest) -> None:
        active = self._active.get(request.call_id)
        if active is None:
            self._send_result(
                call_id=request.call_id,
                status="unknown",
                started_at=datetime.now(UTC),
                error=ToolError(
                    code="TOOL_CALL_NOT_ACTIVE",
                    message="The requested call is not active in this Runner session",
                ),
            )
            return
        active.cancellation.set()
        active.future.cancel()

    def _handle_commit_receipt_request(
        self,
        request: ToolCommitReceiptRequest,
    ) -> None:
        self._send_payload(
            ToolCommitReceiptResult(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                call_id=request.call_id,
                receipt=self._executor.commit_receipt_for_call(request.call_id),
                occurred_at=datetime.now(UTC),
            )
        )

    def _queue_completion(self, call_id: str, future: Future[BaseModel]) -> None:
        self._events.put(_CallCompleted(call_id, future))

    def _handle_completion(self, event: _CallCompleted) -> None:
        active = self._active.pop(event.call_id, None)
        if active is None:
            return
        commit_snapshot = (
            active.commit_boundary.snapshot()
            if active.commit_boundary is not None
            else None
        )
        if (
            commit_snapshot is not None
            and commit_snapshot.phase is ControlledCommitPhase.COMMITTED
            and commit_snapshot.output is not None
        ):
            self._send_success(
                event.call_id,
                active.started_at,
                commit_snapshot.output,
            )
            return
        if event.future.cancelled():
            self._send_result(
                call_id=event.call_id,
                status="cancelled",
                started_at=active.started_at,
                error=ToolError(code="TOOL_CANCELLED", message="Tool call was cancelled"),
            )
            return
        if (
            active.cancellation.is_set()
            and active.idempotency is not ToolIdempotency.IDEMPOTENT
        ):
            if commit_snapshot is not None and commit_snapshot.phase in {
                ControlledCommitPhase.BEFORE_COMMIT,
                ControlledCommitPhase.NO_EFFECT,
            }:
                self._send_result(
                    call_id=event.call_id,
                    status="cancelled",
                    started_at=active.started_at,
                    error=ToolError(
                        code="TOOL_CANCELLED",
                        message="Tool call was cancelled before its commit boundary",
                    ),
                )
                return
            self._send_result(
                call_id=event.call_id,
                status="unknown",
                started_at=active.started_at,
                error=ToolError(
                    code="TOOL_CANCEL_OUTCOME_UNKNOWN",
                    message=(
                        "Cancellation was requested after a non-replay-safe Tool "
                        "started; the outcome cannot be proven"
                    ),
                ),
            )
            return
        try:
            output = event.future.result()
        except ToolExecutionCancelledError as error:
            self._send_result(
                call_id=event.call_id,
                status="cancelled",
                started_at=active.started_at,
                error=ToolError(code=error.code, message=str(error)),
            )
        except Exception as error:
            commit_snapshot = (
                active.commit_boundary.snapshot()
                if active.commit_boundary is not None
                else None
            )
            if commit_snapshot is not None and commit_snapshot.phase in {
                ControlledCommitPhase.COMMITTING,
                ControlledCommitPhase.UNKNOWN,
            }:
                self._send_result(
                    call_id=event.call_id,
                    status="unknown",
                    started_at=active.started_at,
                    error=ToolError(
                        code="TOOL_COMMIT_OUTCOME_UNKNOWN",
                        message=(
                            "The Tool crossed its commit boundary but no durable "
                            "committed receipt could be returned"
                        ),
                    ),
                )
                return
            self._send_failure_code(
                event.call_id,
                code=getattr(error, "code", "TOOL_EXECUTION_FAILED"),
                message=f"Tool execution failed: {type(error).__name__}",
                started_at=active.started_at,
            )
        else:
            self._send_success(event.call_id, active.started_at, output)

    def _expire_calls(self, now: float) -> None:
        expired = [
            call_id
            for call_id, active in self._active.items()
            if active.deadline <= now
        ]
        for call_id in expired:
            active = self._active.pop(call_id)
            active.cancellation.set()
            cancelled_before_execution = active.future.cancel()
            commit_snapshot = (
                active.commit_boundary.snapshot()
                if active.commit_boundary is not None
                else None
            )
            if (
                commit_snapshot is not None
                and commit_snapshot.phase is ControlledCommitPhase.COMMITTED
                and commit_snapshot.output is not None
            ):
                self._send_success(
                    call_id,
                    active.started_at,
                    commit_snapshot.output,
                )
                continue
            if (
                cancelled_before_execution
                or active.idempotency is ToolIdempotency.IDEMPOTENT
                or (
                    commit_snapshot is not None
                    and commit_snapshot.phase
                    in {
                        ControlledCommitPhase.BEFORE_COMMIT,
                        ControlledCommitPhase.NO_EFFECT,
                    }
                )
            ):
                self._send_failure_code(
                    call_id,
                    code="TOOL_TIMEOUT",
                    message="Tool exceeded its Contract timeout",
                    started_at=active.started_at,
                )
            else:
                self._send_result(
                    call_id=call_id,
                    status="unknown",
                    started_at=active.started_at,
                    error=ToolError(
                        code="TOOL_TIMEOUT_OUTCOME_UNKNOWN",
                        message=(
                            "A non-replay-safe Tool exceeded its timeout after execution "
                            "started; the outcome cannot be proven"
                        ),
                    ),
                )

    def _send_success(
        self,
        call_id: str,
        started_at: datetime,
        output: BaseModel,
    ) -> None:
        self._send_payload(
            ToolProgress(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                call_id=call_id,
                sequence=1,
                message="Tool execution completed",
                percent=100,
                occurred_at=datetime.now(UTC),
            )
        )
        self._send_result(
            call_id=call_id,
            status="succeeded",
            started_at=started_at,
            output=output.model_dump(mode="json"),
        )

    def _send_heartbeat(self) -> None:
        self._send_payload(
            RunnerHeartbeat(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                occurred_at=datetime.now(UTC),
                active_call_ids=tuple(sorted(self._active)),
            )
        )

    def _send_failure(self, call_id: str, error: Exception) -> None:
        self._send_failure_code(
            call_id,
            code=getattr(error, "code", "TOOL_AUTHORIZATION_FAILED"),
            message=str(error),
        )

    def _send_failure_code(
        self,
        call_id: str,
        *,
        code: str,
        message: str,
        retryable: bool = False,
        started_at: datetime | None = None,
    ) -> None:
        self._send_result(
            call_id=call_id,
            status="failed",
            started_at=started_at or datetime.now(UTC),
            error=ToolError(
                code=code,
                message=message[:1_000],
                retryable=retryable,
            ),
        )

    def _send_result(
        self,
        *,
        call_id: str,
        status: Literal["succeeded", "failed", "cancelled", "unknown"],
        started_at: datetime,
        output: dict[str, JsonValue] | None = None,
        error: ToolError | None = None,
    ) -> None:
        self._send_payload(
            ToolCallResult(
                runner_id=self._runner_id,
                startup_nonce=self._bootstrap.startup_nonce,
                call_id=call_id,
                status=status,
                output=output,
                error=error,
                started_at=started_at,
                finished_at=datetime.now(UTC),
            )
        )

    def _send_payload(self, payload: IpcPayload) -> None:
        envelope = self._signer.sign(payload)
        self._stdout.write(self._codec.encode(envelope))
        self._stdout.flush()

    def _diagnose(self, code: str) -> None:
        self._stderr.write(f"Runner rejected IPC message: {code}\n")
        self._stderr.flush()


def run_server_from_stdio(executor: ToolExecutor, *, worker_factory: str) -> int:
    bootstrap_frame = sys.stdin.buffer.readline(4_097)
    try:
        bootstrap = BootstrapCodec().decode(bootstrap_frame)
        secret = decode_bootstrap_secret(bootstrap.secret)
        if len(secret) < 32:
            raise ValueError("Runner bootstrap secret must be at least 32 bytes")
    except (IpcProtocolError, ValueError, binascii.Error) as error:
        code = getattr(error, "code", "RUNNER_BOOTSTRAP_INVALID")
        sys.stderr.write(f"Runner bootstrap rejected: {code}\n")
        sys.stderr.flush()
        return 2
    try:
        server = RunnerServer(
            bootstrap=bootstrap,
            executor=executor,
            worker_factory=worker_factory,
            stdin=sys.stdin.buffer,
            stdout=sys.stdout.buffer,
            stderr=sys.stderr,
        )
    except ProcessIsolationError as error:
        sys.stderr.write(f"Runner isolation unavailable: {error.code}\n")
        sys.stderr.flush()
        return 3
    return server.run()
