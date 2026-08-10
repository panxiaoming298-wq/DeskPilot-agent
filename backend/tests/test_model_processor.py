import time
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.core.config import Settings
from deskpilot.main import create_app
from deskpilot.model_providers import FakeModelProvider


def test_model_failure_is_persisted_with_stable_sanitized_error(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'model-failure.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    provider = FakeModelProvider(failure_message="sensitive injected provider detail")

    with TestClient(create_app(settings, model_provider=provider), headers=headers) as client:
        created = client.post("/api/v1/tasks", json={"goal": "触发模型失败"}).json()
        task_id = str(created["task_id"])
        snapshot = created
        for _ in range(100):
            snapshot = client.get(f"/api/v1/tasks/{task_id}").json()
            if snapshot["status"] == "failed":
                break
            time.sleep(0.01)
        events = client.get(f"/api/v1/tasks/{task_id}/events").json()

    event_types = [event["type"] for event in events]
    assert snapshot["status"] == "failed"
    assert event_types == [
        "task.created",
        "task.status_changed",
        "model.started",
        "model.failed",
        "task.failed",
    ]
    assert events[-2]["payload"] == {
        "request_id": events[2]["payload"]["request_id"],
        "role": "intent",
        "provider_id": "fake-local",
        "code": "MODEL_PROVIDER_UNAVAILABLE",
        "retryable": True,
    }
    serialized_events = str(events)
    assert "sensitive injected provider detail" not in serialized_events
    assert events[-1]["payload"] == {
        "error_type": "TaskProcessingError",
        "code": "TASK_PROCESSING_FAILED",
        "message": "Task processing failed before completion.",
    }
