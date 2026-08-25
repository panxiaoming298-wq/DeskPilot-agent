import sqlite3
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from pydantic import SecretStr

from deskpilot.application.task_service import TaskRevisionConflictError
from deskpilot.core.config import Settings
from deskpilot.domain.schemas import FileMoveTaskRequest, TaskCreate, TaskStatus
from deskpilot.main import create_app


def _wait_for_status(
    client: TestClient,
    task_id: str,
    expected: str,
    *,
    timeout: float = 12,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        latest = response.json()
        if latest["status"] == expected:
            return latest
        time.sleep(0.01)
    raise AssertionError(f"Task did not reach {expected}; latest={latest}")


def _events(client: TestClient, task_id: str) -> list[dict[str, object]]:
    response = client.get(f"/api/v1/tasks/{task_id}/events")
    assert response.status_code == 200
    return response.json()


def _control(
    client: TestClient,
    task_id: str,
    command: str,
    *,
    reason: str | None = None,
    expected_last_event_seq: int | None = None,
) -> Response:
    if expected_last_event_seq is None:
        snapshot = client.get(f"/api/v1/tasks/{task_id}")
        assert snapshot.status_code == 200
        expected_last_event_seq = int(snapshot.json()["last_event_seq"])
    payload: dict[str, object] = {
        "expected_last_event_seq": expected_last_event_seq,
    }
    if reason is not None:
        payload["reason"] = reason
    return client.post(f"/api/v1/tasks/{task_id}:{command}", json=payload)


def _read_transient_database_file(path: Path, *, timeout: float = 2) -> bytes | None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            return path.read_bytes()
        except FileNotFoundError:
            # SQLite may remove its transient journal after the glob snapshot.
            return None
        except PermissionError:
            # Windows can briefly deny reads while SQLite owns a rollback journal.
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def test_cancel_stops_processor_and_is_idempotent(slow_client: TestClient) -> None:
    created = slow_client.post("/api/v1/tasks", json={"goal": "取消慢任务"}).json()
    task_id = str(created["task_id"])

    running = _wait_for_status(slow_client, task_id, "running")
    expected_last_event_seq = int(running["last_event_seq"])
    cancelled = _control(
        slow_client,
        task_id,
        "cancel",
        reason="用户改变主意",
        expected_last_event_seq=expected_last_event_seq,
    )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    first_events = _events(slow_client, task_id)
    assert first_events[-1]["type"] == "task.cancelled"
    reservation = next(
        event for event in first_events if event["type"] == "task.control_requested"
    )
    assert reservation["payload"] == {
        "from": "running",
        "to": "cancelled",
        "requested_action": "cancel",
        "requested_by": "user",
        "expected_last_event_seq": expected_last_event_seq,
    }
    assert first_events[-1]["payload"] == {
        "from": "running",
        "to": "cancelled",
        "command": "cancel",
        "requested_by": "user",
        "expected_last_event_seq": expected_last_event_seq,
        "control_request_event_id": reservation["event_id"],
        "reason": "用户改变主意",
    }

    time.sleep(0.25)
    repeated = _control(slow_client, task_id, "cancel")
    second_events = _events(slow_client, task_id)

    assert repeated.status_code == 200
    assert repeated.json()["last_event_seq"] == cancelled.json()["last_event_seq"]
    assert second_events == first_events


def test_pause_resume_continues_from_checkpoint_without_duplicate_events(
    slow_client: TestClient,
) -> None:
    created = slow_client.post("/api/v1/tasks", json={"goal": "暂停后继续"}).json()
    task_id = str(created["task_id"])
    _wait_for_status(slow_client, task_id, "running")

    paused = _control(slow_client, task_id, "pause", reason="等待用户确认")
    assert paused.status_code == 200
    assert paused.json()["status"] == "paused"
    paused_seq = paused.json()["last_event_seq"]

    time.sleep(0.25)
    still_paused = slow_client.get(f"/api/v1/tasks/{task_id}").json()
    assert still_paused["status"] == "paused"
    assert still_paused["last_event_seq"] == paused_seq

    repeated_pause = _control(slow_client, task_id, "pause")
    assert repeated_pause.status_code == 200
    assert repeated_pause.json()["last_event_seq"] == paused_seq

    resumed = _control(slow_client, task_id, "resume", reason="确认完成")
    assert resumed.status_code == 200
    assert resumed.json()["status"] == "running"
    completed = _wait_for_status(slow_client, task_id, "succeeded")
    events = _events(slow_client, task_id)
    event_types = [str(event["type"]) for event in events]

    assert [event["seq"] for event in events] == list(range(1, len(events) + 1))
    assert event_types.count("tool.requested") == 1
    assert event_types.count("tool.started") == 1
    assert event_types.count("tool.completed") == 1
    assert event_types.count("model.started") == 2
    assert event_types.count("model.usage") == 2
    assert event_types.count("task.classified") == 1
    assert event_types.count("policy.evaluated") == 1
    assert event_types[-1] == "task.completed"
    assert completed["last_event_seq"] == len(events) == 25
    commands = [
        event["payload"].get("command")
        for event in events
        if isinstance(event["payload"], dict) and "command" in event["payload"]
    ]
    assert commands == ["processor", "processor", "pause", "resume"]


def test_pause_before_running_is_rejected_without_stopping_processor(
    slow_client: TestClient,
) -> None:
    created = slow_client.post("/api/v1/tasks", json={"goal": "过早暂停"}).json()
    task_id = str(created["task_id"])

    response = _control(slow_client, task_id, "pause")

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    problem = response.json()
    assert problem["code"] in {
        "TASK_TRANSITION_NOT_ALLOWED",
        "TASK_REVISION_CONFLICT",
    }
    if problem["code"] == "TASK_TRANSITION_NOT_ALLOWED":
        assert problem["current_status"] in {"created", "classifying"}
    else:
        # The background processor may advance after ``_control`` reads the
        # Task revision but before the command acquires the Task lock.  That
        # stale command must be rejected rather than pause a newer state.
        assert problem["expected_last_event_seq"] < problem["current_last_event_seq"]
    _wait_for_status(slow_client, task_id, "succeeded")


def test_control_requires_an_explicit_task_revision(
    slow_client: TestClient,
) -> None:
    created = slow_client.post("/api/v1/tasks", json={"goal": "缺少版本绑定"}).json()
    task_id = str(created["task_id"])
    _wait_for_status(slow_client, task_id, "running")
    paused = _control(slow_client, task_id, "pause")
    assert paused.status_code == 200
    paused_snapshot = paused.json()
    events_before = _events(slow_client, task_id)

    response = slow_client.post(f"/api/v1/tasks/{task_id}:resume")

    assert response.status_code == 428
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "TASK_REVISION_REQUIRED"
    assert response.json()["current_last_event_seq"] == paused_snapshot["last_event_seq"]
    assert slow_client.get(f"/api/v1/tasks/{task_id}").json() == paused_snapshot
    assert _events(slow_client, task_id) == events_before


def test_stale_control_revision_fails_closed_without_resuming_processor(
    slow_client: TestClient,
) -> None:
    created = slow_client.post("/api/v1/tasks", json={"goal": "过期版本绑定"}).json()
    task_id = str(created["task_id"])
    _wait_for_status(slow_client, task_id, "running")
    paused = _control(slow_client, task_id, "pause")
    assert paused.status_code == 200
    paused_snapshot = paused.json()
    events_before = _events(slow_client, task_id)

    response = _control(
        slow_client,
        task_id,
        "resume",
        expected_last_event_seq=int(paused_snapshot["last_event_seq"]) - 1,
    )

    assert response.status_code == 409
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "TASK_REVISION_CONFLICT"
    assert response.json()["expected_last_event_seq"] == (
        int(paused_snapshot["last_event_seq"]) - 1
    )
    assert response.json()["current_last_event_seq"] == paused_snapshot["last_event_seq"]
    assert slow_client.get(f"/api/v1/tasks/{task_id}").json() == paused_snapshot
    assert _events(slow_client, task_id) == events_before


def test_task_service_rechecks_control_revision_inside_task_lock(
    client: TestClient,
) -> None:
    service = client.app.state.task_service

    async def exercise_atomic_guards() -> None:
        for command in ("pause", "cancel"):
            task = await service.create_task(TaskCreate(goal=f"{command} atomic revision"))
            stale_seq = task.last_event_seq
            await service.append_event(task.task_id, "test.revision.advanced", {})

            with pytest.raises(TaskRevisionConflictError) as raised:
                if command == "pause":
                    await service.transition_task(
                        task.task_id,
                        target=TaskStatus.PAUSED,
                        command="pause",
                        expected_last_event_seq=stale_seq,
                    )
                else:
                    await service.cancel_task(
                        task.task_id,
                        expected_last_event_seq=stale_seq,
                    )

            assert raised.value.expected_last_event_seq == stale_seq
            assert raised.value.current_last_event_seq == stale_seq + 1
            snapshot = await service.get_task(task.task_id)
            assert snapshot.status.value == "created"
            assert snapshot.last_event_seq == stale_seq + 1

    assert client.portal is not None
    client.portal.call(exercise_atomic_guards)


def test_terminal_task_rejects_pause_resume_and_cancel(client: TestClient) -> None:
    created = client.post("/api/v1/tasks", json={"goal": "完成后拒绝控制"}).json()
    task_id = str(created["task_id"])
    _wait_for_status(client, task_id, "succeeded")

    for command in ("pause", "resume", "cancel"):
        response = _control(client, task_id, command)
        assert response.status_code == 409
        assert response.json()["code"] == "TASK_TRANSITION_NOT_ALLOWED"
        assert response.json()["current_status"] == "succeeded"


def test_paused_task_resumes_from_durable_checkpoint_after_api_restart(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    database_path = tmp_path / "restart-control.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.2,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(create_app(settings), headers=headers) as first_client:
        created = first_client.post("/api/v1/tasks", json={"goal": "跨重启恢复"}).json()
        task_id = str(created["task_id"])
        _wait_for_status(first_client, task_id, "running")
        paused = _control(first_client, task_id, "pause")
        assert paused.status_code == 200

    with TestClient(create_app(settings), headers=headers) as restarted_client:
        response = _control(restarted_client, task_id, "resume")
        assert response.status_code == 200
        assert response.json()["status"] == "running"
        completed = _wait_for_status(restarted_client, task_id, "succeeded")
        events = _events(restarted_client, task_id)
        event_types = [str(event["type"]) for event in events]

        assert completed["status"] == "succeeded"
        assert event_types.count("task.classified") == 1
        assert event_types.count("plan.proposed") == 1
        assert event_types.count("tool.requested") == 1
        assert event_types.count("tool.started") == 1
        assert event_types.count("tool.completed") == 1
        assert event_types[-1] == "task.completed"
        assert task_id in (
            restarted_client.app.state.task_runtime_recovery.restored_task_ids
        )


def test_created_structured_file_move_recovers_without_parsing_goal(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    database_path = tmp_path / "restart-created-file-move.db"
    source = tmp_path / "structured-source.txt"
    destination = tmp_path / "structured-destination.txt"
    source.write_text("durable structured request", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
        runner_commit_receipt_database_path=str(tmp_path / "restart-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(create_app(settings), headers=headers) as first_client:
        assert first_client.portal is not None
        created = first_client.portal.call(
            first_client.app.state.task_service.create_task,
            TaskCreate(
                goal="这段目标文字没有包含任何文件路径",
                privacy_mode="local_only",
                constraints=["single_file", "no_overwrite", "no_cloud"],
                tool_request=FileMoveTaskRequest(
                    source=str(source.resolve()),
                    destination=str(destination.resolve()),
                ),
            ),
        )
        task_id = created.task_id

    with TestClient(create_app(settings), headers=headers) as restarted_client:
        _wait_for_status(restarted_client, task_id, "waiting_approval")
        approvals = restarted_client.get(
            "/api/v1/approvals",
            params={"status": "pending", "task_id": task_id},
        ).json()

        assert task_id in (
            restarted_client.app.state.task_runtime_recovery.restored_task_ids
        )
        assert len(approvals) == 1
        labels = {
            resource["label"]
            for resource in approvals[0]["resource_scope"]
        }
        assert labels == {str(source.resolve()), str(destination.resolve())}
        assert source.exists()
        assert not destination.exists()

        async def read_checkpoint_key() -> str:
            loaded = await restarted_client.app.state.task_service.load_task_checkpoints()
            checkpoint = next(
                item for item in loaded.checkpoints if item.payload.task_id == task_id
            )
            assert checkpoint.payload.tool_idempotency_key is not None
            return checkpoint.payload.tool_idempotency_key

        assert restarted_client.portal is not None
        protected_tool_key = restarted_client.portal.call(read_checkpoint_key)
        for database_file in tmp_path.glob(f"{database_path.name}*"):
            database_bytes = _read_transient_database_file(database_file)
            if database_bytes is None:
                continue
            assert protected_tool_key.encode("utf-8") not in database_bytes

        approved = restarted_client.post(
            f"/api/v1/approvals/{approvals[0]['approval_id']}:approve",
            json={
                "preview_hash": approvals[0]["preview_hash"],
                "scope": "once",
            },
        )
        assert approved.status_code == 200
        _wait_for_status(restarted_client, task_id, "succeeded")
        events = _events(restarted_client, task_id)
        assert [event["type"] for event in events].count("tool.requested") == 1
        assert [event["type"] for event in events].count("tool.started") == 1
        assert [event["type"] for event in events].count("tool.completed") == 1
        assert destination.read_text(encoding="utf-8") == "durable structured request"
        assert not source.exists()


def test_corrupt_checkpoint_fails_closed_without_runner_dispatch(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    database_path = tmp_path / "restart-corrupt-checkpoint.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.2,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
        runner_commit_receipt_database_path=str(tmp_path / "corrupt-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(create_app(settings), headers=headers) as first_client:
        created = first_client.post(
            "/api/v1/tasks",
            json={"goal": "损坏的检查点绝不能触发 Tool"},
        ).json()
        task_id = str(created["task_id"])
        _wait_for_status(first_client, task_id, "running")
        assert _control(first_client, task_id, "pause").status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE task_runtime_checkpoints SET protected_payload = ? WHERE task_id = ?",
            (b"tampered-checkpoint", task_id),
        )
        connection.commit()

    with TestClient(create_app(settings), headers=headers) as restarted_client:
        snapshot = restarted_client.get(f"/api/v1/tasks/{task_id}").json()
        events = _events(restarted_client, task_id)

        assert snapshot["status"] == "failed"
        assert task_id in (
            restarted_client.app.state.task_runtime_recovery.failed_task_ids
        )
        assert events[-1]["type"] == "task.failed"
        assert events[-1]["payload"]["code"] == "TASK_CHECKPOINT_INVALID"
        assert "tool.started" not in [event["type"] for event in events]


def test_stale_checkpoint_event_binding_fails_closed_without_runner_dispatch(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    database_path = tmp_path / "restart-stale-checkpoint.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.2,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
        runner_commit_receipt_database_path=str(tmp_path / "stale-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(create_app(settings), headers=headers) as first_client:
        created = first_client.post(
            "/api/v1/tasks",
            json={"goal": "过期事件绑定绝不能触发 Tool"},
        ).json()
        task_id = str(created["task_id"])
        _wait_for_status(first_client, task_id, "running")
        assert _control(first_client, task_id, "pause").status_code == 200

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE task_runtime_checkpoints "
            "SET event_seq = event_seq - 1 WHERE task_id = ?",
            (task_id,),
        )
        connection.commit()

    with TestClient(create_app(settings), headers=headers) as restarted_client:
        snapshot = restarted_client.get(f"/api/v1/tasks/{task_id}").json()
        events = _events(restarted_client, task_id)

        assert snapshot["status"] == "failed"
        assert task_id in (
            restarted_client.app.state.task_runtime_recovery.failed_task_ids
        )
        assert events[-1]["type"] == "task.failed"
        assert events[-1]["payload"]["code"] == "TASK_CHECKPOINT_BINDING_INVALID"
        assert "tool.started" not in [event["type"] for event in events]
