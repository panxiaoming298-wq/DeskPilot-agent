import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.processor import TaskRuntimeUnavailableError
from deskpilot.application.runner_supervisor import RunnerLease
from deskpilot.core.config import Settings
from deskpilot.domain.approvals import ApprovalStatus
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.main import create_app
from deskpilot.runner.ipc_protocol import ToolCallResult

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_SESSION_TOKEN = "approval-api-test-session-token-32-characters"


class RecordingRunnerSupervisor:
    def __init__(self) -> None:
        self.calls: list[ToolAuthorizationGrant] = []
        self.started = False

    @property
    def runner_id(self) -> str:
        return "runner-approval-test"

    @property
    def process_id(self) -> int:
        return 4242

    @property
    def is_running(self) -> bool:
        return self.started

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.started = False

    def ensure_ready(self, *, expected_runner_id: str | None = None) -> RunnerLease:
        if expected_runner_id is not None:
            assert expected_runner_id == self.runner_id
        assert self.started
        return RunnerLease(runner_id=self.runner_id, generation=1, client=self)  # type: ignore[arg-type]

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
        progress_callback: Any = None,
    ) -> ToolCallResult:
        del step_id, tool_name, tool_version, actor, idempotency_key, progress_callback
        assert expected_runner_id == self.runner_id
        assert call_id is not None
        assert authorization.task_id == task_id
        assert authorization.call_id == call_id
        assert authorization.approval_id is not None
        assert expected_resource_versions == {}
        self.calls.append(authorization)
        timestamp = datetime.now(UTC)
        path = str(arguments["path"])
        return ToolCallResult(
            runner_id=self.runner_id,
            startup_nonce="approval-test-startup-nonce",
            call_id=call_id,
            status="succeeded",
            output={
                "requested_path": path,
                "resolved_path": path,
                "total_bytes": 100,
                "used_bytes": 40,
                "free_bytes": 60,
                "used_percent": 40.0,
            },
            started_at=timestamp,
            finished_at=timestamp,
        )


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }


@contextmanager
def _approval_client(
    tmp_path: Path,
    name: str,
    *,
    default_headers: bool = True,
) -> Iterator[tuple[TestClient, RecordingRunnerSupervisor]]:
    database_path = tmp_path / name
    canonical_path = str(tmp_path.resolve(strict=True))
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_SESSION_TOKEN),
        cors_origins=[TEST_ORIGIN],
        disk_usage_path=canonical_path,
        policy_approval_ttl_seconds=30,
    )
    policy = BuiltinPolicyEngine(
        allowed_resource_scopes=(("filesystem_path", canonical_path),),
        require_approval_for_r0=True,
        approval_ttl_seconds=30,
    )
    runner = RecordingRunnerSupervisor()
    client_headers = _headers() if default_headers else None
    with TestClient(
        create_app(
            settings,
            policy_engine=policy,
            runner_supervisor=runner,  # type: ignore[arg-type]
        ),
        headers=client_headers,
    ) as client:
        yield client, runner


def _wait_for_status(
    client: TestClient,
    task_id: str,
    expected: str,
    *,
    timeout: float = 3,
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


def _create_pending_task(client: TestClient, goal: str) -> tuple[str, dict[str, object]]:
    created = client.post("/api/v1/tasks", json={"goal": goal})
    assert created.status_code == 201
    task_id = str(created.json()["task_id"])
    _wait_for_status(client, task_id, "waiting_approval")
    listed = client.get(
        "/api/v1/approvals",
        params={"status": "pending", "task_id": task_id},
    )
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    approvals = listed.json()
    assert len(approvals) == 1
    return task_id, approvals[0]


def _events(client: TestClient, task_id: str) -> list[dict[str, object]]:
    response = client.get(f"/api/v1/tasks/{task_id}/events")
    assert response.status_code == 200
    return response.json()


def test_approval_api_requires_session_and_trusted_write_origin(tmp_path: Path) -> None:
    with _approval_client(
        tmp_path,
        "approval-api-security.db",
        default_headers=False,
    ) as (client, runner):
        missing_token = client.get(
            "/api/v1/approvals",
            headers={"Origin": TEST_ORIGIN},
        )
        invalid_origin = client.get(
            "/api/v1/approvals",
            headers={
                "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
                "Origin": "https://attacker.example",
            },
        )
        missing_write_origin = client.post(
            "/api/v1/approvals/apr_missing:approve",
            headers={"Authorization": f"Bearer {TEST_SESSION_TOKEN}"},
            json={"preview_hash": "a" * 64, "scope": "once"},
        )
        untrusted_write_origin = client.post(
            "/api/v1/approvals/apr_missing:reject",
            headers={
                "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
                "Origin": "https://attacker.example",
            },
            json={"preview_hash": "a" * 64, "scope": "once"},
        )

        assert missing_token.status_code == 401
        assert missing_token.json()["code"] == "SESSION_TOKEN_INVALID"
        assert invalid_origin.status_code == 403
        assert invalid_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
        assert missing_write_origin.status_code == 403
        assert missing_write_origin.json()["code"] == "ORIGIN_REQUIRED"
        assert untrusted_write_origin.status_code == 403
        assert untrusted_write_origin.json()["code"] == "ORIGIN_NOT_ALLOWED"
        assert runner.calls == []


def test_forced_r0_approval_waits_then_approves_and_executes_once(
    tmp_path: Path,
) -> None:
    with _approval_client(tmp_path, "approval-api-approve.db") as (client, runner):
        task_id, approval = _create_pending_task(
            client,
            "Wait for explicit approval before reading disk capacity",
        )
        approval_id = str(approval["approval_id"])

        assert approval["status"] == "pending"
        assert approval["risk_level"] == "R0"
        assert approval["policy_revision"] == "builtin-tool-policy-v1"
        assert approval["resource_scope"] == [
            {
                "kind": "filesystem_path",
                "label": str(tmp_path.resolve(strict=True)),
                "operations": ["filesystem.metadata.read"],
                "version": None,
            }
        ]
        assert runner.calls == []
        assert "tool.started" not in [event["type"] for event in _events(client, task_id)]

        stale = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={"preview_hash": "f" * 64, "scope": "once"},
        )
        assert stale.status_code == 409
        assert stale.json()["code"] == "APPROVAL_STALE"
        assert runner.calls == []

        approved = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "preview_hash": approval["preview_hash"],
                "scope": "once",
                "reason": "Preview reviewed",
            },
        )
        assert approved.status_code == 200
        assert approved.headers["cache-control"] == "no-store"
        assert approved.json()["approval"]["status"] == "approved"
        assert approved.json()["task"]["status"] == "running"
        assert approved.json()["replayed"] is False

        completed = _wait_for_status(client, task_id, "succeeded")
        detail = client.get(f"/api/v1/approvals/{approval_id}")
        replayed = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "preview_hash": approval["preview_hash"],
                "scope": "once",
            },
        )
        events = _events(client, task_id)

        assert completed["status"] == "succeeded"
        assert detail.status_code == 200
        assert detail.headers["cache-control"] == "no-store"
        assert detail.json()["consumed_at"] is not None
        assert replayed.status_code == 200
        assert replayed.json()["replayed"] is True
        assert len(runner.calls) == 1
        assert runner.calls[0].approval_id == approval_id
        assert [event["type"] for event in events].count("tool.started") == 1
        assert [event["type"] for event in events].count("tool.completed") == 1
        assert events[-1]["type"] == "task.completed"


def test_pending_approval_survives_restart_and_executes_once_after_approval(
    tmp_path: Path,
) -> None:
    database_name = "approval-api-restart-checkpoint.db"
    with _approval_client(tmp_path, database_name) as (client, first_runner):
        task_id, approval = _create_pending_task(
            client,
            "Resume the exact pending approval after restart",
        )
        approval_id = str(approval["approval_id"])
        preview_hash = str(approval["preview_hash"])
        assert first_runner.calls == []

    with _approval_client(tmp_path, database_name) as (client, restarted_runner):
        detail = client.get(f"/api/v1/approvals/{approval_id}")
        task = client.get(f"/api/v1/tasks/{task_id}")

        assert detail.status_code == 200
        assert detail.json()["status"] == "pending"
        assert task.json()["status"] == "waiting_approval"
        assert task_id in client.app.state.task_runtime_recovery.restored_task_ids
        assert client.app.state.approval_recovery.approvals_cancelled == 0
        assert restarted_runner.calls == []

        approved = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={"preview_hash": preview_hash, "scope": "once"},
        )
        assert approved.status_code == 200
        _wait_for_status(client, task_id, "succeeded")

        events = _events(client, task_id)
        assert len(restarted_runner.calls) == 1
        assert [event["type"] for event in events].count("tool.requested") == 1
        assert [event["type"] for event in events].count("tool.started") == 1
        assert [event["type"] for event in events].count("tool.completed") == 1


def test_approved_unconsumed_checkpoint_auto_continues_after_restart(
    tmp_path: Path,
) -> None:
    database_name = "approval-api-approved-restart.db"
    with _approval_client(tmp_path, database_name) as (client, first_runner):
        task_id, approval = _create_pending_task(
            client,
            "Continue an approved but undispatched checkpoint after restart",
        )
        approval_id = str(approval["approval_id"])

        async def persist_approval_without_runtime_continuation() -> None:
            await client.app.state.task_service.resolve_approval(
                approval_id,
                decision=ApprovalStatus.APPROVED,
                preview_hash=str(approval["preview_hash"]),
            )

        assert client.portal is not None
        client.portal.call(persist_approval_without_runtime_continuation)
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "running"
        assert first_runner.calls == []

    with _approval_client(tmp_path, database_name) as (client, restarted_runner):
        _wait_for_status(client, task_id, "succeeded")
        detail = client.get(f"/api/v1/approvals/{approval_id}").json()
        events = _events(client, task_id)

        assert detail["status"] == "approved"
        assert detail["consumed_at"] is not None
        assert len(restarted_runner.calls) == 1
        assert [event["type"] for event in events].count("tool.requested") == 1
        assert [event["type"] for event in events].count("tool.started") == 1
        assert [event["type"] for event in events].count("tool.completed") == 1


def test_forced_r0_rejection_cancels_without_runner_dispatch(tmp_path: Path) -> None:
    with _approval_client(tmp_path, "approval-api-reject.db") as (client, runner):
        task_id, approval = _create_pending_task(
            client,
            "Reject this disk capacity request",
        )
        approval_id = str(approval["approval_id"])

        rejected = client.post(
            f"/api/v1/approvals/{approval_id}:reject",
            json={
                "preview_hash": approval["preview_hash"],
                "scope": "once",
                "reason": "Do not inspect this path",
            },
        )
        assert rejected.status_code == 200
        assert rejected.headers["cache-control"] == "no-store"
        assert rejected.json()["approval"]["status"] == "rejected"
        assert rejected.json()["task"]["status"] == "cancelled"
        assert rejected.json()["replayed"] is False
        _wait_for_status(client, task_id, "cancelled")
        time.sleep(0.05)

        opposite = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={"preview_hash": approval["preview_hash"], "scope": "once"},
        )
        events = _events(client, task_id)
        event_types = [event["type"] for event in events]

        assert opposite.status_code == 409
        assert opposite.json()["code"] == "APPROVAL_ALREADY_RESOLVED"
        assert opposite.json()["approval_status"] == "rejected"
        assert runner.calls == []
        assert "tool.started" not in event_types
        assert "tool.completed" not in event_types
        assert event_types[-3:] == [
            "approval.resolved",
            "tool.cancelled",
            "task.cancelled",
        ]


def test_approved_retry_recovers_after_first_continuation_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _approval_client(tmp_path, "approval-api-continuation-retry.db") as (
        client,
        runner,
    ):
        task_id, approval = _create_pending_task(
            client,
            "Recover an approved checkpoint on retry",
        )
        approval_id = str(approval["approval_id"])
        processor = client.app.state.processor
        original_continue = processor.continue_after_approval
        continuation_attempts = 0

        def fail_first_continuation(task: str, requested_approval: str) -> None:
            nonlocal continuation_attempts
            continuation_attempts += 1
            if continuation_attempts == 1:
                raise TaskRuntimeUnavailableError(task)
            original_continue(task, requested_approval)

        monkeypatch.setattr(
            processor,
            "continue_after_approval",
            fail_first_continuation,
        )
        command = {
            "preview_hash": approval["preview_hash"],
            "scope": "once",
        }

        first = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json=command,
        )
        persisted = client.get(f"/api/v1/approvals/{approval_id}")

        assert first.status_code == 409
        assert first.json()["code"] == "APPROVAL_RUNTIME_UNAVAILABLE"
        assert persisted.status_code == 200
        assert persisted.json()["status"] == "approved"
        assert persisted.json()["consumed_at"] is None
        assert _wait_for_status(client, task_id, "running")["status"] == "running"
        assert runner.calls == []

        second = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json=command,
        )
        assert second.status_code == 200
        assert second.json()["replayed"] is True
        _wait_for_status(client, task_id, "succeeded")

        assert continuation_attempts == 2
        assert len(runner.calls) == 1
        assert runner.calls[0].approval_id == approval_id


def test_approved_unconsumed_replay_without_runtime_returns_conflict(
    tmp_path: Path,
) -> None:
    with _approval_client(tmp_path, "approval-api-no-runtime-replay.db") as (
        client,
        runner,
    ):
        task_id, approval = _create_pending_task(
            client,
            "Do not fake success without an approval checkpoint",
        )
        approval_id = str(approval["approval_id"])
        service = client.app.state.task_service
        processor = client.app.state.processor

        async def approve_without_continuing() -> None:
            await service.resolve_approval(
                approval_id,
                decision=ApprovalStatus.APPROVED,
                preview_hash=str(approval["preview_hash"]),
            )

        async def forget_runtime() -> None:
            processor.forget(task_id)

        assert client.portal is not None
        client.portal.call(approve_without_continuing)
        client.portal.call(forget_runtime)

        replay = client.post(
            f"/api/v1/approvals/{approval_id}:approve",
            json={
                "preview_hash": approval["preview_hash"],
                "scope": "once",
            },
        )
        detail = client.get(f"/api/v1/approvals/{approval_id}")

        assert replay.status_code == 409
        assert replay.json()["code"] == "APPROVAL_RUNTIME_UNAVAILABLE"
        assert detail.status_code == 200
        assert detail.json()["status"] == "approved"
        assert detail.json()["consumed_at"] is None
        assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "running"
        assert runner.calls == []
