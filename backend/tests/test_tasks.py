import time
from pathlib import Path

from fastapi.testclient import TestClient


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


def test_health(client: TestClient) -> None:
    response = client.get("/api/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["processor"] == "model-gateway+runner"
    assert response.json()["runner"] == "ready"
    assert response.json()["runner_state"] == "ready"
    assert response.json()["runner_generation"] == 1
    assert response.json()["runner_consecutive_failures"] == 0
    assert response.json()["runner_restart_attempts"] == 0
    assert response.json()["runner_retry_in_seconds"] is None
    assert response.json()["runner_last_failure_code"] is None
    assert response.json()["model_provider"] == "fake-local"


def test_task_event_vertical_slice(client: TestClient, session_token: str) -> None:
    create_response = client.post(
        "/api/v1/tasks",
        json={
            "goal": "验证前后端任务事件闭环",
            "privacy_mode": "local_only",
            "constraints": ["read_only"],
        },
    )

    assert create_response.status_code == 201
    created = create_response.json()
    task_id = created["task_id"]
    assert created["status"] == "created"
    assert created["event_stream"].endswith(task_id)

    task = _wait_for_status(client, task_id, "succeeded")

    events_response = client.get(f"/api/v1/tasks/{task_id}/events?after_seq=0")
    assert events_response.status_code == 200
    events = events_response.json()
    event_types = [event["type"] for event in events]
    sequences = [event["seq"] for event in events]

    assert event_types[0] == "task.created"
    assert event_types.count("model.started") == 2
    assert event_types.count("model.usage") == 2
    assert "task.classified" in event_types
    assert "plan.proposed" in event_types
    assert "tool.completed" in event_types
    assert event_types[-1] == "task.completed"
    assert sequences == list(range(1, len(events) + 1))
    assert task["last_event_seq"] == len(events)
    assert all(event["timestamp"].endswith("Z") for event in events)
    requested = next(event for event in events if event["type"] == "tool.requested")
    completed = next(event for event in events if event["type"] == "tool.completed")
    assert requested["payload"]["tool"] == "computer.disk_usage"
    assert requested["payload"]["tool_version"] == "1.0.0"
    assert len(requested["payload"]["contract_digest"]) == 64
    assert completed["payload"]["tool"] == "computer.disk_usage"
    assert completed["payload"]["call_id"] == requested["payload"]["call_id"]
    assert completed["payload"]["result"]["total_bytes"] > 0
    model_started = [event for event in events if event["type"] == "model.started"]
    model_usage = [event for event in events if event["type"] == "model.usage"]
    plan = next(event for event in events if event["type"] == "plan.proposed")
    classification = next(event for event in events if event["type"] == "task.classified")
    assert [event["payload"]["role"] for event in model_started] == [
        "intent",
        "planner",
    ]
    assert all(event["payload"]["provider_id"] == "fake-local" for event in model_usage)
    assert all(event["payload"]["usage"]["total_tokens"] > 0 for event in model_usage)
    assert plan["payload"]["provider_id"] == "fake-local"
    assert plan["payload"]["steps"][1]["tool_name"] == "computer.disk_usage"
    assert classification["payload"]["classification"]["intent"] == "computer_info"
    assert "messages" not in model_started[0]["payload"]

    with client.websocket_connect(
        f"/api/v1/ws/tasks/{task_id}?after_seq=0",
        subprotocols=["deskpilot.v1", f"deskpilot.auth.{session_token}"],
    ) as socket:
        streamed_types: list[str] = []
        while True:
            message = socket.receive_json()
            streamed_types.append(message["type"])
            if message["type"] == "task.completed":
                break

    assert streamed_types == event_types


def test_missing_task_returns_stable_error(client: TestClient) -> None:
    response = client.get("/api/v1/tasks/tsk_missing")

    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "TASK_NOT_FOUND"


def test_task_history_is_bounded_filtered_and_newest_first(client: TestClient) -> None:
    task_ids: list[str] = []
    for index in range(3):
        created = client.post(
            "/api/v1/tasks",
            json={
                "goal": f"history task {index}",
                "privacy_mode": "local_only",
                "constraints": ["read_only"],
            },
        )
        assert created.status_code == 201
        task_id = str(created.json()["task_id"])
        task_ids.append(task_id)
        _wait_for_status(client, task_id, "succeeded")

    first_page = client.get("/api/v1/tasks", params={"limit": 2, "offset": 0})
    second_page = client.get("/api/v1/tasks", params={"limit": 2, "offset": 2})
    filtered = client.get(
        "/api/v1/tasks",
        params={"status": "succeeded", "limit": 100},
    )

    assert first_page.status_code == 200
    assert first_page.headers["cache-control"] == "no-store"
    assert first_page.json()["total"] == 3
    assert first_page.json()["limit"] == 2
    assert first_page.json()["offset"] == 0
    assert [item["task_id"] for item in first_page.json()["items"]] == list(
        reversed(task_ids[1:])
    )
    assert [item["task_id"] for item in second_page.json()["items"]] == [
        task_ids[0]
    ]
    assert filtered.status_code == 200
    assert filtered.json()["total"] == 3
    assert all(item["status"] == "succeeded" for item in filtered.json()["items"])

    invalid = client.get("/api/v1/tasks", params={"limit": 101})
    assert invalid.status_code == 422


def test_explicit_file_move_requires_approval_and_returns_commit_receipt(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "task-source.txt"
    destination = tmp_path / "task-destination.txt"
    source.write_text("approved task move", encoding="utf-8")

    created = client.post(
        "/api/v1/tasks",
        json={
            "goal": "将我选择的单个文件移动到新路径",
            "privacy_mode": "local_only",
            "constraints": ["single_file", "no_overwrite", "no_cloud"],
            "tool_request": {
                "kind": "file_move",
                "source": str(source),
                "destination": str(destination),
            },
        },
    )

    assert created.status_code == 201
    task_id = str(created.json()["task_id"])
    _wait_for_status(client, task_id, "waiting_approval")
    listed = client.get(
        "/api/v1/approvals",
        params={"status": "pending", "task_id": task_id},
    )
    assert listed.status_code == 200
    approvals = listed.json()
    assert len(approvals) == 1
    approval = approvals[0]
    assert approval["tool_name"] == "file.move"
    assert approval["tool_version"] == "1.0.0"
    assert approval["risk_level"] == "R1"
    assert approval["reversible"] is True
    assert approval["capabilities"] == [
        "filesystem.file.move_destination",
        "filesystem.file.move_source",
    ]
    scopes = {resource["label"]: resource for resource in approval["resource_scope"]}
    assert set(scopes) == {str(source.resolve()), str(destination.resolve())}
    assert scopes[str(source.resolve())]["version"] is not None
    assert scopes[str(destination.resolve())]["version"] is None
    assert source.exists()
    assert not destination.exists()

    approved = client.post(
        f"/api/v1/approvals/{approval['approval_id']}:approve",
        json={"preview_hash": approval["preview_hash"], "scope": "once"},
    )
    assert approved.status_code == 200
    completed = _wait_for_status(client, task_id, "succeeded")
    assert completed["status"] == "succeeded"
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "approved task move"

    events = client.get(f"/api/v1/tasks/{task_id}/events").json()
    event_types = [event["type"] for event in events]
    classification = next(event for event in events if event["type"] == "task.classified")
    plan = next(event for event in events if event["type"] == "plan.proposed")
    requested = next(event for event in events if event["type"] == "tool.requested")
    tool_completed = next(event for event in events if event["type"] == "tool.completed")
    receipt = tool_completed["payload"]["result"]["commit_receipt"]
    assert classification["payload"]["source"] == "explicit_user_request"
    assert classification["payload"]["classification"]["intent"] == "file"
    assert plan["payload"]["source"] == "trusted_application_template"
    assert plan["payload"]["steps"][1]["tool_name"] == "file.move"
    assert "model.started" not in event_types
    assert requested["payload"]["tool"] == "file.move"
    assert len(requested["payload"]["idempotency_key_digest"]) == 64
    assert receipt["status"] == "committed"
    assert receipt["call_id"] == requested["payload"]["call_id"]
    assert receipt["resource_versions_after"]["source"] == "absent"


def test_explicit_file_move_rejects_invalid_resource_before_task_creation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    response = client.post(
        "/api/v1/tasks",
        json={
            "goal": "不要创建无效文件移动任务",
            "tool_request": {
                "kind": "file_move",
                "source": str(tmp_path / "missing.txt"),
                "destination": str(tmp_path / "destination.txt"),
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["code"] == "FILE_MOVE_REQUEST_INVALID"
