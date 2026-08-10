"""Generation-bound recovery supervisor for the independent Tool Runner."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from deskpilot.application.runner_client import (
    ProgressCallback,
    RunnerClientError,
    RunnerStartupError,
    RunnerUnavailableError,
)
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.runner.ipc_protocol import ToolCallResult

logger = logging.getLogger(__name__)


class RunnerSupervisorState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BACKOFF = "backoff"
    OPEN = "open"
    HALF_OPEN = "half_open"


class RunnerSupervisorUnavailableError(RunnerUnavailableError):
    code = "RUNNER_SUPERVISOR_UNAVAILABLE"


class RunnerCircuitOpenError(RunnerSupervisorUnavailableError):
    code = "RUNNER_CIRCUIT_OPEN"


class RunnerGenerationChangedError(RunnerSupervisorUnavailableError):
    code = "RUNNER_GENERATION_CHANGED"


class RunnerClientPort(Protocol):
    @property
    def runner_id(self) -> str | None: ...

    @property
    def process_id(self) -> int | None: ...

    @property
    def is_running(self) -> bool: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_for_failure(self) -> RunnerClientError: ...

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
    ) -> ToolCallResult: ...

    async def get_commit_receipt(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ToolCommitReceipt | None: ...


RunnerClientFactory = Callable[[], RunnerClientPort]
Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RunnerLease:
    """An immutable reference to exactly one successfully started generation."""

    runner_id: str
    generation: int
    client: RunnerClientPort


@dataclass(frozen=True, slots=True)
class RunnerSupervisorSnapshot:
    state: RunnerSupervisorState
    runner_id: str | None
    process_id: int | None
    generation: int
    start_attempts: int
    consecutive_failures: int
    total_failures: int
    next_retry_at_monotonic: float | None
    retry_in_seconds: float | None
    stable_since_monotonic: float | None
    stable_for_seconds: float | None
    last_failure_code: str | None


class RunnerSupervisor:
    """Own one Runner generation at a time and recover it without replaying calls."""

    def __init__(
        self,
        *,
        client_factory: RunnerClientFactory,
        restart_base_delay_seconds: float = 0.25,
        restart_max_delay_seconds: float = 5.0,
        circuit_failure_threshold: int = 3,
        circuit_recovery_timeout_seconds: float = 30.0,
        stable_window_seconds: float = 10.0,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if restart_base_delay_seconds < 0:
            raise ValueError("Runner restart base delay cannot be negative")
        if restart_max_delay_seconds < restart_base_delay_seconds:
            raise ValueError("Runner restart max delay cannot be below its base delay")
        if circuit_failure_threshold < 1:
            raise ValueError("Runner circuit failure threshold must be positive")
        if circuit_recovery_timeout_seconds < 0:
            raise ValueError("Runner circuit recovery timeout cannot be negative")
        if stable_window_seconds <= 0:
            raise ValueError("Runner stable window must be positive")

        self._client_factory = client_factory
        self._restart_base_delay_seconds = restart_base_delay_seconds
        self._restart_max_delay_seconds = restart_max_delay_seconds
        self._circuit_failure_threshold = circuit_failure_threshold
        self._circuit_recovery_timeout_seconds = circuit_recovery_timeout_seconds
        self._stable_window_seconds = stable_window_seconds
        self._monotonic = monotonic
        self._sleep = sleep

        self._state = RunnerSupervisorState.STOPPED
        self._lease: RunnerLease | None = None
        self._candidate: RunnerClientPort | None = None
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._first_attempt_complete = asyncio.Event()
        self._stopping = False

        self._generation = 0
        self._start_attempts = 0
        self._consecutive_failures = 0
        self._total_failures = 0
        self._next_retry_at: float | None = None
        self._stable_since: float | None = None
        self._last_failure: RunnerClientError | None = None

    @property
    def state(self) -> RunnerSupervisorState:
        return self._state

    @property
    def runner_id(self) -> str | None:
        lease = self._lease
        return lease.runner_id if lease is not None else None

    @property
    def process_id(self) -> int | None:
        lease = self._lease
        return lease.client.process_id if lease is not None else None

    @property
    def is_running(self) -> bool:
        lease = self._lease
        return (
            lease is not None
            and self._state
            in {RunnerSupervisorState.READY, RunnerSupervisorState.HALF_OPEN}
            and lease.client.is_running
        )

    def snapshot(self) -> RunnerSupervisorSnapshot:
        now = self._monotonic()
        lease = self._lease
        next_retry_at = self._next_retry_at
        stable_since = self._stable_since
        return RunnerSupervisorSnapshot(
            state=self._state,
            runner_id=lease.runner_id if lease is not None else None,
            process_id=lease.client.process_id if lease is not None else None,
            generation=self._generation,
            start_attempts=self._start_attempts,
            consecutive_failures=self._consecutive_failures,
            total_failures=self._total_failures,
            next_retry_at_monotonic=next_retry_at,
            retry_in_seconds=(
                max(0.0, next_retry_at - now) if next_retry_at is not None else None
            ),
            stable_since_monotonic=stable_since,
            stable_for_seconds=(
                max(0.0, now - stable_since) if stable_since is not None else None
            ),
            last_failure_code=(
                self._last_failure.code if self._last_failure is not None else None
            ),
        )

    async def start(self) -> None:
        """Try once, then return even if recovery must continue in the background."""
        async with self._lifecycle_lock:
            lifecycle = self._lifecycle_task
            if lifecycle is None or lifecycle.done():
                self._stopping = False
                self._first_attempt_complete = asyncio.Event()
                self._state = RunnerSupervisorState.STARTING
                lifecycle = asyncio.create_task(
                    self._run_lifecycle(), name="runner-supervisor"
                )
                self._lifecycle_task = lifecycle
            first_attempt_complete = self._first_attempt_complete
        await first_attempt_complete.wait()

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stopping = True
            self._state = RunnerSupervisorState.STOPPED
            self._next_retry_at = None
            lifecycle = self._lifecycle_task
            if lifecycle is not None and not lifecycle.done():
                lifecycle.cancel()

        if lifecycle is not None:
            await asyncio.gather(lifecycle, return_exceptions=True)

        async with self._lifecycle_lock:
            if self._lifecycle_task is lifecycle:
                self._lifecycle_task = None
            self._state = RunnerSupervisorState.STOPPED
            self._lease = None
            self._candidate = None
            self._stable_since = None
            self._first_attempt_complete.set()

    def ensure_ready(
        self, *, expected_runner_id: str | None = None
    ) -> RunnerLease:
        lease = self._lease
        if self._state is RunnerSupervisorState.OPEN:
            raise RunnerCircuitOpenError("Tool Runner recovery circuit is open")
        if (
            lease is None
            or self._state
            not in {RunnerSupervisorState.READY, RunnerSupervisorState.HALF_OPEN}
            or not lease.client.is_running
        ):
            raise RunnerSupervisorUnavailableError(
                f"Tool Runner is unavailable while supervisor is {self._state.value}"
            )
        if expected_runner_id is not None and expected_runner_id != lease.runner_id:
            raise RunnerGenerationChangedError(
                "Tool Runner generation changed before the call was dispatched"
            )
        return lease

    async def call_tool(
        self,
        *,
        task_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        arguments: dict[str, object],
        actor: str,
        expected_runner_id: str | None = None,
        call_id: str | None = None,
        idempotency_key: str | None = None,
        expected_resource_versions: dict[str, str] | None = None,
        authorization: ToolAuthorizationGrant,
        progress_callback: ProgressCallback | None = None,
    ) -> ToolCallResult:
        lease = self.ensure_ready(expected_runner_id=expected_runner_id)
        return await lease.client.call_tool(
            task_id=task_id,
            step_id=step_id,
            tool_name=tool_name,
            tool_version=tool_version,
            arguments=arguments,
            actor=actor,
            call_id=call_id,
            idempotency_key=idempotency_key,
            expected_resource_versions=expected_resource_versions,
            authorization=authorization,
            progress_callback=progress_callback,
        )

    async def get_commit_receipt(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ToolCommitReceipt | None:
        """Query the current generation's durable journal without replaying a call."""
        lease = self.ensure_ready()
        return await lease.client.get_commit_receipt(
            call_id,
            timeout_seconds=timeout_seconds,
        )

    async def _run_lifecycle(self) -> None:
        half_open_attempt = False
        try:
            while not self._stopping:
                self._next_retry_at = None
                self._state = (
                    RunnerSupervisorState.HALF_OPEN
                    if half_open_attempt
                    else RunnerSupervisorState.STARTING
                )
                self._start_attempts += 1
                client: RunnerClientPort | None = None
                try:
                    client = self._client_factory()
                    self._candidate = client
                    await client.start()
                    runner_id = client.runner_id
                    if runner_id is None or not client.is_running:
                        raise RunnerStartupError(
                            "Tool Runner reported no live identity after startup"
                        )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._candidate = None
                    if client is not None:
                        await self._safe_stop(client)
                    failure = self._normalize_failure(error)
                    self._record_failure(failure)
                    self._first_attempt_complete.set()
                    half_open_attempt = await self._wait_before_restart()
                    continue

                if self._stopping:
                    await self._safe_stop(client)
                    break

                self._candidate = None
                self._generation += 1
                lease = RunnerLease(
                    runner_id=runner_id,
                    generation=self._generation,
                    client=client,
                )
                self._lease = lease
                self._stable_since = self._monotonic()
                self._state = (
                    RunnerSupervisorState.HALF_OPEN
                    if half_open_attempt
                    else RunnerSupervisorState.READY
                )
                self._first_attempt_complete.set()

                failure, failed_while_half_open = await self._monitor_generation(
                    lease,
                    half_open=half_open_attempt,
                )
                if self._stopping:
                    break
                if self._lease is lease:
                    self._lease = None
                self._stable_since = None
                self._record_failure(failure)
                self._state = self._recovery_state(
                    force_open=failed_while_half_open
                )
                await self._safe_stop(client)
                half_open_attempt = await self._wait_before_restart(
                    force_open=failed_while_half_open
                )
        except asyncio.CancelledError:
            pass
        finally:
            current_lease = self._lease
            candidate = self._candidate
            self._lease = None
            self._candidate = None
            self._stable_since = None
            if current_lease is not None:
                await self._safe_stop(current_lease.client)
            if candidate is not None and (
                current_lease is None or candidate is not current_lease.client
            ):
                await self._safe_stop(candidate)
            self._first_attempt_complete.set()
            if self._stopping:
                self._state = RunnerSupervisorState.STOPPED

    async def _monitor_generation(
        self,
        lease: RunnerLease,
        *,
        half_open: bool,
    ) -> tuple[RunnerClientError, bool]:
        failure_task = asyncio.create_task(
            lease.client.wait_for_failure(),
            name=f"runner-failure-{lease.generation}",
        )
        stable_task: asyncio.Future[None] = asyncio.ensure_future(
            self._sleep(self._stable_window_seconds),
        )
        try:
            done, _ = await asyncio.wait(
                (failure_task, stable_task),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if failure_task in done:
                return self._failure_task_result(failure_task), half_open

            if not lease.client.is_running:
                failure = await failure_task
                return failure, half_open

            if self._lease is lease and not self._stopping:
                self._consecutive_failures = 0
                self._state = RunnerSupervisorState.READY
            failure = await failure_task
            return failure, False
        finally:
            for task in (failure_task, stable_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(failure_task, stable_task, return_exceptions=True)

    def _failure_task_result(
        self, task: asyncio.Task[RunnerClientError]
    ) -> RunnerClientError:
        try:
            return task.result()
        except RunnerClientError as error:
            return error
        except Exception as error:
            return RunnerUnavailableError(
                f"Runner failure monitor failed: {type(error).__name__}"
            )

    def _record_failure(self, failure: RunnerClientError) -> None:
        self._last_failure = failure
        self._total_failures += 1
        self._consecutive_failures += 1

    async def _wait_before_restart(self, *, force_open: bool = False) -> bool:
        if self._stopping:
            return False
        self._state = self._recovery_state(force_open=force_open)
        if self._state is RunnerSupervisorState.OPEN:
            delay = self._circuit_recovery_timeout_seconds
            next_attempt_half_open = True
        else:
            exponent = max(0, self._consecutive_failures - 1)
            delay = min(
                self._restart_max_delay_seconds,
                self._restart_base_delay_seconds * (2**exponent),
            )
            next_attempt_half_open = False
        self._next_retry_at = self._monotonic() + delay
        await self._sleep(delay)
        self._next_retry_at = None
        return next_attempt_half_open

    def _recovery_state(self, *, force_open: bool) -> RunnerSupervisorState:
        if (
            force_open
            or self._consecutive_failures >= self._circuit_failure_threshold
        ):
            return RunnerSupervisorState.OPEN
        return RunnerSupervisorState.BACKOFF

    @staticmethod
    async def _safe_stop(client: RunnerClientPort) -> None:
        try:
            await client.stop()
        except Exception:
            logger.exception("Failed to stop a Tool Runner generation")

    @staticmethod
    def _normalize_failure(error: Exception) -> RunnerClientError:
        if isinstance(error, RunnerClientError):
            return error
        return RunnerStartupError(
            f"Tool Runner startup raised {type(error).__name__}"
        )
