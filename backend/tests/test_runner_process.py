import asyncio
import os
import signal
import sys
from pathlib import Path

import pytest

from deskpilot.application.runner_client import (
    RunnerClient,
    RunnerExitedError,
    RunnerStartupError,
)
from deskpilot.application.runner_supervisor import RunnerSupervisor
from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.tools import create_builtin_registry
from deskpilot.tools.computer import DISK_USAGE_CONTRACT
from tests.authorization_helpers import make_tool_authorization
from tests.fixtures.slow_runner_service import (
    ISOLATION_PROBE_CONTRACT,
    NON_IDEMPOTENT_SLOW_CONTRACT,
    SLOW_CONTRACT,
    IsolationProbeInput,
    IsolationProbeOutput,
    SlowInput,
    SlowOutput,
    project_isolation_probe_resources,
    project_non_idempotent_slow_resources,
    project_slow_resources,
)


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel isolation test")
@pytest.mark.asyncio
async def test_each_call_uses_a_low_integrity_restricted_job_worker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = ToolRegistry()
    registry.register(
        ISOLATION_PROBE_CONTRACT,
        IsolationProbeInput,
        IsolationProbeOutput,
        project_isolation_probe_resources,
    )
    secret_name = "DESKPILOT_TEST_PARENT_SECRET"
    monkeypatch.setenv(secret_name, "must-not-cross-worker-boundary")
    client = RunnerClient(
        registry=registry,
        command=(sys.executable, "-m", "tests.fixtures.slow_runner_service"),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        outputs: list[dict[str, object]] = []
        for index in range(2):
            call_id = f"call-isolation-{index}"
            arguments = {
                "write_path": str(tmp_path / f"blocked-{index}.txt"),
                "secret_name": secret_name,
            }
            result = await client.call_tool(
                task_id=f"task-isolation-{index}",
                step_id=f"step-isolation-{index}",
                tool_name=ISOLATION_PROBE_CONTRACT.name,
                tool_version=ISOLATION_PROBE_CONTRACT.version,
                arguments=arguments,
                actor="pytest",
                call_id=call_id,
                authorization=make_tool_authorization(
                    ISOLATION_PROBE_CONTRACT,
                    task_id=f"task-isolation-{index}",
                    step_id=f"step-isolation-{index}",
                    call_id=call_id,
                    actor_id="pytest",
                    arguments=arguments,
                ),
            )
            assert result.status == "succeeded"
            assert result.output is not None
            outputs.append(result.output)

        assert client.isolation_mode == "windows_restricted"
        assert outputs[0]["process_id"] != outputs[1]["process_id"]
        assert all(output["process_id"] != client.process_id for output in outputs)
        assert all(output["integrity_level_rid"] == 4_096 for output in outputs)
        assert all(int(output["privilege_count"]) <= 1 for output in outputs)
        assert all(output["in_job"] is True for output in outputs)
        assert all(output["write_succeeded"] is False for output in outputs)
        assert all(output["child_process_succeeded"] is False for output in outputs)
        assert all(output["secret_present"] is False for output in outputs)
        assert not await asyncio.to_thread(
            lambda: tuple(tmp_path.glob("blocked-*.txt"))
        )
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_independent_runner_executes_real_disk_usage(tmp_path: Path) -> None:
    progress_sequences: list[int] = []
    task_id = "task-integration-1"
    call_id = "call-integration-1"
    arguments = {"path": str(tmp_path)}
    client = RunnerClient(
        registry=create_builtin_registry(),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        result = await client.call_tool(
            task_id=task_id,
            step_id="step-disk-1",
            tool_name="computer.disk_usage",
            tool_version="1.0.0",
            arguments=arguments,
            actor="pytest",
            call_id=call_id,
            authorization=make_tool_authorization(
                DISK_USAGE_CONTRACT,
                task_id=task_id,
                step_id="step-disk-1",
                call_id=call_id,
                actor_id="pytest",
                arguments=arguments,
            ),
            progress_callback=lambda progress: progress_sequences.append(progress.sequence),
        )
        await asyncio.sleep(0.25)

        assert client.runner_id is not None
        assert client.process_id is not None
        assert client.process_id != os.getpid()
        assert client.heartbeat_count >= 1
        assert result.status == "succeeded"
        assert result.output is not None
        assert result.output["resolved_path"] == str(tmp_path)
        assert int(result.output["total_bytes"]) > 0
        assert progress_sequences == [0, 1]
    finally:
        await client.stop()

    assert client.is_running is False


@pytest.mark.asyncio
async def test_runner_returns_structured_failure_for_missing_path(
    tmp_path: Path,
) -> None:
    task_id = "task-integration-2"
    call_id = "call-integration-2"
    arguments = {"path": str(tmp_path / "missing")}
    client = RunnerClient(registry=create_builtin_registry())
    try:
        await client.start()
        result = await client.call_tool(
            task_id=task_id,
            step_id="step-disk-2",
            tool_name="computer.disk_usage",
            tool_version="1.0.0",
            arguments=arguments,
            actor="pytest",
            call_id=call_id,
            authorization=make_tool_authorization(
                DISK_USAGE_CONTRACT,
                task_id=task_id,
                step_id="step-disk-2",
                call_id=call_id,
                actor_id="pytest",
                arguments=arguments,
            ),
        )

        assert result.status == "failed"
        assert result.output is None
        assert result.error is not None
        assert result.error.code == "TOOL_RESOURCE_PROJECTION_FAILED"
        assert "projected" in result.error.message.lower()
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_runner_enforces_timeout_and_cooperative_cancel() -> None:
    registry = ToolRegistry()
    registry.register(
        SLOW_CONTRACT,
        SlowInput,
        SlowOutput,
        project_slow_resources,
    )
    client = RunnerClient(
        registry=registry,
        command=(sys.executable, "-m", "tests.fixtures.slow_runner_service"),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        timeout_arguments = {"delay_seconds": 2.0}
        timed_out = await client.call_tool(
            task_id="task-timeout",
            step_id="step-timeout",
            tool_name="test.slow",
            tool_version="1.0.0",
            arguments=timeout_arguments,
            actor="pytest",
            call_id="call-timeout",
            authorization=make_tool_authorization(
                SLOW_CONTRACT,
                task_id="task-timeout",
                step_id="step-timeout",
                call_id="call-timeout",
                actor_id="pytest",
                arguments=timeout_arguments,
            ),
        )
        assert timed_out.status == "failed"
        assert timed_out.error is not None
        assert timed_out.error.code == "TOOL_TIMEOUT"

        started = asyncio.Event()
        cancel_arguments = {"delay_seconds": 2.0}
        call = asyncio.create_task(
            client.call_tool(
                task_id="task-cancel",
                step_id="step-cancel",
                tool_name="test.slow",
                tool_version="1.0.0",
                arguments=cancel_arguments,
                actor="pytest",
                call_id="call-cancel",
                authorization=make_tool_authorization(
                    SLOW_CONTRACT,
                    task_id="task-cancel",
                    step_id="step-cancel",
                    call_id="call-cancel",
                    actor_id="pytest",
                    arguments=cancel_arguments,
                ),
                progress_callback=lambda _: started.set(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await client.cancel_call("call-cancel", "test requested cancellation")
        cancelled = await asyncio.wait_for(call, timeout=1)

        assert cancelled.status == "cancelled"
        assert cancelled.error is not None
        assert cancelled.error.code == "TOOL_CANCELLED"
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_non_idempotent_timeout_is_reported_as_unknown() -> None:
    registry = ToolRegistry()
    registry.register(
        NON_IDEMPOTENT_SLOW_CONTRACT,
        SlowInput,
        SlowOutput,
        project_non_idempotent_slow_resources,
    )
    client = RunnerClient(
        registry=registry,
        command=(sys.executable, "-m", "tests.fixtures.slow_runner_service"),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        arguments = {"delay_seconds": 2.0}
        result = await client.call_tool(
            task_id="task-uncertain-timeout",
            step_id="step-uncertain-timeout",
            tool_name=NON_IDEMPOTENT_SLOW_CONTRACT.name,
            tool_version=NON_IDEMPOTENT_SLOW_CONTRACT.version,
            arguments=arguments,
            actor="pytest",
            call_id="call-uncertain-timeout",
            authorization=make_tool_authorization(
                NON_IDEMPOTENT_SLOW_CONTRACT,
                task_id="task-uncertain-timeout",
                step_id="step-uncertain-timeout",
                call_id="call-uncertain-timeout",
                actor_id="pytest",
                arguments=arguments,
            ),
        )

        assert result.status == "unknown"
        assert result.error is not None
        assert result.error.code == "TOOL_TIMEOUT_OUTCOME_UNKNOWN"
        assert result.error.retryable is False
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_non_idempotent_in_flight_cancel_is_reported_as_unknown() -> None:
    registry = ToolRegistry()
    registry.register(
        NON_IDEMPOTENT_SLOW_CONTRACT,
        SlowInput,
        SlowOutput,
        project_non_idempotent_slow_resources,
    )
    client = RunnerClient(
        registry=registry,
        command=(sys.executable, "-m", "tests.fixtures.slow_runner_service"),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        started = asyncio.Event()
        arguments = {"delay_seconds": 0.2}
        call = asyncio.create_task(
            client.call_tool(
                task_id="task-uncertain-cancel",
                step_id="step-uncertain-cancel",
                tool_name=NON_IDEMPOTENT_SLOW_CONTRACT.name,
                tool_version=NON_IDEMPOTENT_SLOW_CONTRACT.version,
                arguments=arguments,
                actor="pytest",
                call_id="call-uncertain-cancel",
                authorization=make_tool_authorization(
                    NON_IDEMPOTENT_SLOW_CONTRACT,
                    task_id="task-uncertain-cancel",
                    step_id="step-uncertain-cancel",
                    call_id="call-uncertain-cancel",
                    actor_id="pytest",
                    arguments=arguments,
                ),
                progress_callback=lambda _: started.set(),
            )
        )
        await asyncio.wait_for(started.wait(), timeout=1)
        await client.cancel_call("call-uncertain-cancel", "test cancellation")
        result = await asyncio.wait_for(call, timeout=1)

        assert result.status == "unknown"
        assert result.error is not None
        assert result.error.code == "TOOL_CANCEL_OUTCOME_UNKNOWN"
        assert result.error.retryable is False
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_runner_startup_reports_child_exit() -> None:
    client = RunnerClient(
        registry=create_builtin_registry(),
        command=(sys.executable, "-c", "raise SystemExit(7)"),
        startup_timeout_seconds=1,
    )

    with pytest.raises(RunnerStartupError) as startup:
        await client.start()

    assert startup.value.code == "RUNNER_STARTUP_FAILED"
    assert client.is_running is False


@pytest.mark.asyncio
async def test_client_detects_unexpected_runner_exit() -> None:
    client = RunnerClient(
        registry=create_builtin_registry(),
        heartbeat_interval_seconds=0.1,
        heartbeat_timeout_seconds=1,
    )
    try:
        await client.start()
        process_id = client.process_id
        assert process_id is not None
        assert process_id != os.getpid()
        failure_wait = asyncio.create_task(client.wait_for_failure())
        os.kill(process_id, signal.SIGTERM)

        for _ in range(50):
            if not client.is_running:
                break
            await asyncio.sleep(0.02)

        failure = await asyncio.wait_for(failure_wait, timeout=1)
        arguments = {"path": "."}
        with pytest.raises(RunnerExitedError) as unavailable:
            await client.call_tool(
                task_id="task-after-exit",
                step_id="step-after-exit",
                tool_name="computer.disk_usage",
                tool_version="1.0.0",
                arguments=arguments,
                actor="pytest",
                call_id="call-after-exit",
                authorization=make_tool_authorization(
                    DISK_USAGE_CONTRACT,
                    task_id="task-after-exit",
                    step_id="step-after-exit",
                    call_id="call-after-exit",
                    actor_id="pytest",
                    arguments=arguments,
                ),
            )
        assert unavailable.value is failure
        assert client.failure is failure
    finally:
        await client.stop()


@pytest.mark.asyncio
async def test_supervisor_restarts_a_real_runner_after_unexpected_exit() -> None:
    supervisor = RunnerSupervisor(
        client_factory=lambda: RunnerClient(
            registry=create_builtin_registry(),
            heartbeat_interval_seconds=0.1,
            heartbeat_timeout_seconds=1,
        ),
        restart_base_delay_seconds=0.05,
        restart_max_delay_seconds=0.1,
        circuit_failure_threshold=3,
        circuit_recovery_timeout_seconds=0.2,
        stable_window_seconds=1,
    )
    try:
        await supervisor.start()
        first = supervisor.ensure_ready()
        first_process_id = first.client.process_id
        assert first_process_id is not None

        os.kill(first_process_id, signal.SIGTERM)
        for _ in range(150):
            if supervisor.runner_id is not None and supervisor.runner_id != first.runner_id:
                break
            await asyncio.sleep(0.02)

        replacement = supervisor.ensure_ready()
        assert replacement.runner_id != first.runner_id
        assert replacement.generation == first.generation + 1
        assert supervisor.snapshot().total_failures == 1
    finally:
        await supervisor.stop()
