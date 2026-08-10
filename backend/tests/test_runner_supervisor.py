import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from deskpilot.application.runner_client import (
    ProgressCallback,
    RunnerClientError,
    RunnerExitedError,
    RunnerStartupError,
    RunnerUnavailableError,
)
from deskpilot.application.runner_supervisor import (
    RunnerCircuitOpenError,
    RunnerGenerationChangedError,
    RunnerSupervisor,
    RunnerSupervisorState,
)
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.runner.ipc_protocol import ToolCallResult
from tests.authorization_helpers import make_tool_authorization
from tests.fixtures.slow_runner_service import SLOW_CONTRACT


@dataclass
class _SleepRequest:
    delay: float
    future: asyncio.Future[None]
    cancelled: bool = False


class _ControlledClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.requests: list[_SleepRequest] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        request = _SleepRequest(delay, asyncio.get_running_loop().create_future())
        self.requests.append(request)
        try:
            await request.future
        except asyncio.CancelledError:
            request.cancelled = True
            raise

    def pending(self, delay: float) -> list[_SleepRequest]:
        return [
            request
            for request in self.requests
            if request.delay == delay
            and not request.future.done()
            and not request.cancelled
        ]

    def release(self, delay: float) -> None:
        pending = self.pending(delay)
        if not pending:
            raise AssertionError(f"No pending sleep for {delay} seconds")
        self.now += delay
        pending[0].future.set_result(None)


class _FakeRunnerClient:
    def __init__(
        self,
        runner_id: str,
        *,
        start_error: Exception | None = None,
        block_calls: bool = False,
    ) -> None:
        self._runner_id = runner_id
        self._start_error = start_error
        self._block_calls = block_calls
        self._running = False
        self._failure = asyncio.get_running_loop().create_future()
        self._call_result: asyncio.Future[ToolCallResult] | None = None
        self.start_count = 0
        self.stop_count = 0
        self.call_ids: list[str | None] = []

    @property
    def runner_id(self) -> str | None:
        return self._runner_id if self._running else None

    @property
    def process_id(self) -> int | None:
        return 10_000 + int(self._runner_id.rsplit("-", 1)[-1]) if self._running else None

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self.start_count += 1
        if self._start_error is not None:
            raise self._start_error
        self._running = True

    async def stop(self) -> None:
        self.stop_count += 1
        self._running = False
        if self._call_result is not None and not self._call_result.done():
            self._call_result.set_exception(
                RunnerUnavailableError("Fake Runner stopped")
            )

    async def wait_for_failure(self) -> RunnerClientError:
        return await self._failure

    def fail(self, error: RunnerClientError) -> None:
        self._running = False
        if not self._failure.done():
            self._failure.set_result(error)
        if self._call_result is not None and not self._call_result.done():
            self._call_result.set_exception(error)

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
        del (
            task_id,
            step_id,
            tool_name,
            tool_version,
            arguments,
            actor,
            idempotency_key,
            expected_resource_versions,
            authorization,
            progress_callback,
        )
        self.call_ids.append(call_id)
        if self._block_calls:
            self._call_result = asyncio.get_running_loop().create_future()
            return await self._call_result
        occurred_at = datetime.now(UTC)
        return ToolCallResult(
            runner_id=self._runner_id,
            startup_nonce="fake-startup-nonce",
            call_id=call_id or "call-test",
            status="succeeded",
            output={"ok": True},
            started_at=occurred_at,
            finished_at=occurred_at,
        )


class _ClientFactory:
    def __init__(self, clients: list[_FakeRunnerClient]) -> None:
        self._clients = clients
        self.call_count = 0

    def __call__(self) -> _FakeRunnerClient:
        if self.call_count >= len(self._clients):
            raise AssertionError("Runner client factory was called too many times")
        client = self._clients[self.call_count]
        self.call_count += 1
        return client


async def _wait_until(predicate: Callable[[], bool]) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("Condition did not become true")


def _supervisor(
    factory: _ClientFactory,
    clock: _ControlledClock,
    *,
    threshold: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 4.0,
    recovery_timeout: float = 8.0,
    stable_window: float = 20.0,
) -> RunnerSupervisor:
    return RunnerSupervisor(
        client_factory=factory,
        restart_base_delay_seconds=base_delay,
        restart_max_delay_seconds=max_delay,
        circuit_failure_threshold=threshold,
        circuit_recovery_timeout_seconds=recovery_timeout,
        stable_window_seconds=stable_window,
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )


@pytest.mark.asyncio
async def test_initial_failure_degrades_then_restarts_with_capped_backoff() -> None:
    clock = _ControlledClock()
    clients = [
        _FakeRunnerClient("runner-1", start_error=RunnerStartupError("one")),
        _FakeRunnerClient("runner-2", start_error=RunnerStartupError("two")),
        _FakeRunnerClient("runner-3", start_error=RunnerStartupError("three")),
        _FakeRunnerClient("runner-4"),
    ]
    factory = _ClientFactory(clients)
    supervisor = _supervisor(factory, clock, base_delay=1, max_delay=2)

    try:
        await asyncio.gather(supervisor.start(), supervisor.start())

        first = supervisor.snapshot()
        assert first.state is RunnerSupervisorState.BACKOFF
        assert first.consecutive_failures == 1
        assert first.retry_in_seconds == 1
        assert first.last_failure_code == "RUNNER_STARTUP_FAILED"
        assert factory.call_count == 1

        clock.release(1)
        await _wait_until(lambda: len(clock.pending(2)) == 1)
        assert factory.call_count == 2

        clock.release(2)
        await _wait_until(lambda: len(clock.pending(2)) == 1)
        assert factory.call_count == 3

        clock.release(2)
        await _wait_until(lambda: supervisor.runner_id == "runner-4")

        ready = supervisor.snapshot()
        assert ready.state is RunnerSupervisorState.READY
        assert ready.generation == 1
        assert ready.start_attempts == 4
        assert ready.consecutive_failures == 3
        assert ready.total_failures == 3
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_circuit_allows_one_half_open_generation_and_resets_only_when_stable() -> None:
    clock = _ControlledClock()
    clients = [
        _FakeRunnerClient("runner-1", start_error=RunnerStartupError("one")),
        _FakeRunnerClient("runner-2", start_error=RunnerStartupError("two")),
        _FakeRunnerClient("runner-3"),
        _FakeRunnerClient("runner-4"),
    ]
    factory = _ClientFactory(clients)
    supervisor = _supervisor(
        factory,
        clock,
        threshold=2,
        recovery_timeout=8,
        stable_window=20,
    )

    try:
        await supervisor.start()
        clock.release(1)
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.OPEN)

        opened = supervisor.snapshot()
        assert opened.consecutive_failures == 2
        assert opened.retry_in_seconds == 8
        assert factory.call_count == 2
        with pytest.raises(RunnerCircuitOpenError):
            supervisor.ensure_ready()

        clock.release(8)
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.HALF_OPEN)
        assert supervisor.runner_id == "runner-3"
        assert factory.call_count == 3
        assert supervisor.snapshot().consecutive_failures == 2

        lease = supervisor.ensure_ready()
        arguments: dict[str, object] = {}
        result = await supervisor.call_tool(
            task_id="task-half-open",
            step_id="step-half-open",
            tool_name=SLOW_CONTRACT.name,
            tool_version=SLOW_CONTRACT.version,
            arguments=arguments,
            actor="pytest",
            expected_runner_id=lease.runner_id,
            call_id="call-half-open",
            authorization=make_tool_authorization(
                SLOW_CONTRACT,
                task_id="task-half-open",
                step_id="step-half-open",
                call_id="call-half-open",
                actor_id="pytest",
                arguments=arguments,
            ),
        )
        assert result.status == "succeeded"
        assert clients[2].call_ids == ["call-half-open"]

        await _wait_until(lambda: len(clock.pending(20)) == 1)
        clock.release(20)
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.READY)
        assert supervisor.snapshot().consecutive_failures == 0

        clients[2].fail(RunnerExitedError("after stable"))
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.BACKOFF)
        assert supervisor.snapshot().consecutive_failures == 1
        assert factory.call_count == 3

        clock.release(1)
        await _wait_until(lambda: supervisor.runner_id == "runner-4")
        assert supervisor.state is RunnerSupervisorState.READY
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_failed_half_open_probe_reopens_for_a_full_recovery_window() -> None:
    clock = _ControlledClock()
    clients = [
        _FakeRunnerClient("runner-1", start_error=RunnerStartupError("initial")),
        _FakeRunnerClient("runner-2"),
        _FakeRunnerClient("runner-3"),
    ]
    factory = _ClientFactory(clients)
    supervisor = _supervisor(factory, clock, threshold=1, recovery_timeout=8)

    try:
        await supervisor.start()
        assert supervisor.state is RunnerSupervisorState.OPEN

        clock.release(8)
        await _wait_until(lambda: supervisor.runner_id == "runner-2")
        assert supervisor.state is RunnerSupervisorState.HALF_OPEN

        clients[1].fail(RunnerExitedError("probe failed"))
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.OPEN)
        assert factory.call_count == 2
        assert supervisor.snapshot().retry_in_seconds == 8

        await asyncio.sleep(0)
        assert factory.call_count == 2
        clock.release(8)
        await _wait_until(lambda: supervisor.runner_id == "runner-3")
        assert supervisor.state is RunnerSupervisorState.HALF_OPEN
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_generation_lease_prevents_replay_or_dispatch_to_replacement() -> None:
    clock = _ControlledClock()
    clients = [
        _FakeRunnerClient("runner-1", block_calls=True),
        _FakeRunnerClient("runner-2"),
    ]
    factory = _ClientFactory(clients)
    supervisor = _supervisor(factory, clock)

    try:
        await supervisor.start()
        lease = supervisor.ensure_ready()
        arguments: dict[str, object] = {}
        authorization = make_tool_authorization(
            SLOW_CONTRACT,
            task_id="task-old",
            step_id="step-old",
            call_id="call-old",
            actor_id="pytest",
            arguments=arguments,
        )
        call = asyncio.create_task(
            supervisor.call_tool(
                task_id="task-old",
                step_id="step-old",
                tool_name=SLOW_CONTRACT.name,
                tool_version=SLOW_CONTRACT.version,
                arguments=arguments,
                actor="pytest",
                expected_runner_id=lease.runner_id,
                call_id="call-old",
                authorization=authorization,
            )
        )
        await _wait_until(lambda: clients[0].call_ids == ["call-old"])

        clients[0].fail(RunnerExitedError("lost result"))
        with pytest.raises(RunnerExitedError):
            await call
        await _wait_until(lambda: supervisor.state is RunnerSupervisorState.BACKOFF)

        clock.release(1)
        await _wait_until(lambda: supervisor.runner_id == "runner-2")
        with pytest.raises(RunnerGenerationChangedError):
            await supervisor.call_tool(
                task_id="task-old",
                step_id="step-old",
                tool_name=SLOW_CONTRACT.name,
                tool_version=SLOW_CONTRACT.version,
                arguments=arguments,
                actor="pytest",
                expected_runner_id=lease.runner_id,
                call_id="call-old",
                authorization=authorization,
            )

        assert clients[0].call_ids == ["call-old"]
        assert clients[1].call_ids == []
        assert supervisor.ensure_ready().generation == lease.generation + 1
    finally:
        await supervisor.stop()


@pytest.mark.asyncio
async def test_stop_during_backoff_cancels_recovery_and_never_revives() -> None:
    clock = _ControlledClock()
    clients = [
        _FakeRunnerClient("runner-1", start_error=RunnerStartupError("initial")),
        _FakeRunnerClient("runner-2"),
    ]
    factory = _ClientFactory(clients)
    supervisor = _supervisor(factory, clock)

    await supervisor.start()
    await _wait_until(lambda: len(clock.pending(1)) == 1)
    await supervisor.stop()

    assert supervisor.state is RunnerSupervisorState.STOPPED
    assert supervisor.is_running is False
    assert factory.call_count == 1
    assert clock.pending(1) == []
    await asyncio.sleep(0)
    assert factory.call_count == 1


def test_supervisor_rejects_invalid_recovery_policy() -> None:
    clock = _ControlledClock()
    factory = _ClientFactory([])

    with pytest.raises(ValueError, match="base delay"):
        _supervisor(factory, clock, base_delay=-1)
    with pytest.raises(ValueError, match="max delay"):
        _supervisor(factory, clock, base_delay=2, max_delay=1)
    with pytest.raises(ValueError, match="threshold"):
        _supervisor(factory, clock, threshold=0)
    with pytest.raises(ValueError, match="recovery timeout"):
        _supervisor(factory, clock, recovery_timeout=-1)
    with pytest.raises(ValueError, match="stable window"):
        _supervisor(factory, clock, stable_window=0)
