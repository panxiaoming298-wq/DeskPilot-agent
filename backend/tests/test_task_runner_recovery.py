import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, select

from deskpilot.application.runner_client import RunnerExitedError
from deskpilot.application.runner_supervisor import RunnerLease, RunnerSupervisor
from deskpilot.core.config import Settings
from deskpilot.infrastructure.models import ToolCallRecord
from deskpilot.main import create_app
from deskpilot.runner.ipc_protocol import ToolCallResult


class _RestartingRunner:
    def __init__(self) -> None:
        self._runner_id = "runner-before-crash"
        self._generation = 1
        self._running = False
        self.call_ids: list[str] = []

    @property
    def runner_id(self) -> str:
        return self._runner_id

    @property
    def process_id(self) -> int:
        return 10_001 + self._generation

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False

    def ensure_ready(self, *, expected_runner_id: str | None = None) -> RunnerLease:
        if expected_runner_id is not None and expected_runner_id != self._runner_id:
            raise RunnerExitedError("Runner generation changed")
        return RunnerLease(
            runner_id=self._runner_id,
            generation=self._generation,
            client=cast(Any, self),
        )

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
        **_: object,
    ) -> ToolCallResult:
        del task_id, step_id, tool_name, tool_version, arguments, actor
        assert call_id is not None
        assert expected_runner_id == self._runner_id
        self.call_ids.append(call_id)
        if len(self.call_ids) == 1:
            self._runner_id = "runner-after-restart"
            self._generation += 1
            raise RunnerExitedError("Runner exited after the call crossed IPC")

        now = datetime.now(UTC)
        return ToolCallResult(
            runner_id=self._runner_id,
            startup_nonce="test-startup-nonce-0002",
            call_id=call_id,
            status="succeeded",
            output={
                "requested_path": ".",
                "resolved_path": "D:\\test",
                "total_bytes": 100,
                "used_bytes": 40,
                "free_bytes": 60,
                "used_percent": 40.0,
            },
            started_at=now,
            finished_at=now,
        )


def _wait_for_terminal(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    snapshot: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        snapshot = response.json()
        if snapshot["status"] in {"succeeded", "failed", "cancelled"}:
            return snapshot
        time.sleep(0.01)
    raise AssertionError(f"Task did not finish; latest={snapshot}")


def test_lost_runner_call_becomes_unknown_without_replay_and_next_task_recovers(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    database_path = tmp_path / "runner-recovery.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    runner = _RestartingRunner()

    with TestClient(
        create_app(
            settings,
            runner_supervisor=cast(RunnerSupervisor, runner),
        ),
        headers=headers,
    ) as client:
        first = client.post("/api/v1/tasks", json={"goal": "第一次调用丢失结果"}).json()
        first_snapshot = _wait_for_terminal(client, str(first["task_id"]))
        first_events = client.get(
            f"/api/v1/tasks/{first['task_id']}/events"
        ).json()

        second = client.post("/api/v1/tasks", json={"goal": "恢复后执行新任务"}).json()
        second_snapshot = _wait_for_terminal(client, str(second["task_id"]))
        second_events = client.get(
            f"/api/v1/tasks/{second['task_id']}/events"
        ).json()

    first_types = [event["type"] for event in first_events]
    assert first_snapshot["status"] == "failed"
    assert first_types.count("tool.requested") == 1
    assert first_types.count("tool.started") == 1
    assert first_types.count("tool.unknown") == 1
    assert "tool.completed" not in first_types
    assert first_types[-1] == "task.failed"
    unknown = next(event for event in first_events if event["type"] == "tool.unknown")
    assert unknown["payload"]["runner_id"] == "runner-before-crash"
    assert unknown["payload"]["code"] == "RUNNER_EXITED"
    assert unknown["payload"]["retryable"] is False
    assert unknown["payload"]["requires_reconciliation"] is True
    assert first_events[-1]["payload"]["code"] == "TOOL_RESULT_UNKNOWN"

    assert second_snapshot["status"] == "succeeded"
    assert [event["type"] for event in second_events].count("tool.completed") == 1
    assert len(runner.call_ids) == 2
    assert runner.call_ids[0] != runner.call_ids[1]

    engine = create_engine(f"sqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            calls = connection.execute(
                select(
                    ToolCallRecord.call_id,
                    ToolCallRecord.status,
                    ToolCallRecord.runner_id,
                ).order_by(ToolCallRecord.requested_at)
            ).all()
        assert calls == [
            (runner.call_ids[0], "unknown", "runner-before-crash"),
            (runner.call_ids[1], "succeeded", "runner-after-restart"),
        ]
    finally:
        engine.dispose()
