import asyncio
import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.runner_supervisor import RunnerLease
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.core.config import Settings
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.main import create_app
from deskpilot.runner.ipc_protocol import ToolCallResult, ToolError

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_SESSION_TOKEN = "reconciliation-api-session-token-32-chars"


class UnknownRunnerSupervisor:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.receipt_queries: list[str] = []
        self.commit_receipts: dict[str, ToolCommitReceipt] = {}
        self.receipt_query_error: Exception | None = None
        self.emit_commit_receipt = False
        self.unknown_file_move_calls_remaining = 0
        self.started = False

    @property
    def runner_id(self) -> str:
        return "runner-reconciliation-test"

    @property
    def process_id(self) -> int:
        return 4343

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
        del (
            step_id,
            actor,
            progress_callback,
        )
        assert expected_runner_id == self.runner_id
        assert call_id is not None
        assert authorization.task_id == task_id
        assert authorization.call_id == call_id
        self.calls.append(call_id)
        timestamp = datetime.now(UTC)
        if self.emit_commit_receipt:
            assert tool_name == "file.move"
            assert tool_version == "1.0.0"
            assert idempotency_key is not None
            assert authorization.approval_id is not None
            assert authorization.preview_hash is not None
            assert expected_resource_versions is not None
            source_version = expected_resource_versions["source"]
            source = Path(str(arguments["source"]))
            destination = Path(str(arguments["destination"]))
            await asyncio.to_thread(source.rename, destination)
            receipt = ToolCommitReceipt(
                receipt_id=f"cmt_{sha256_digest({'call_id': call_id})}",
                call_id=call_id,
                tool_name=tool_name,
                tool_version=tool_version,
                authorization_id=authorization.authorization_id,
                approval_id=authorization.approval_id,
                preview_hash=authorization.preview_hash,
                prepare_digest="d" * 64,
                idempotency_key_digest=hashlib.sha256(
                    idempotency_key.encode("utf-8")
                ).hexdigest(),
                resource_versions_before={
                    "destination": "absent",
                    "source": source_version,
                },
                resource_versions_after={
                    "destination": source_version,
                    "source": "absent",
                },
                commit_started_at=timestamp,
                receipt_recorded_at=timestamp,
            )
            self.commit_receipts[call_id] = receipt
            if self.unknown_file_move_calls_remaining <= 0:
                return ToolCallResult(
                    runner_id=self.runner_id,
                    startup_nonce="reconciliation-test-startup",
                    call_id=call_id,
                    status="succeeded",
                    output={
                        "source": str(source),
                        "destination": str(destination),
                        "source_version_before": source_version,
                        "destination_version_after": source_version,
                        "reversible": True,
                        "commit_receipt": receipt.model_dump(mode="json"),
                    },
                    started_at=timestamp,
                    finished_at=timestamp,
                )
            self.unknown_file_move_calls_remaining -= 1
        return ToolCallResult(
            runner_id=self.runner_id,
            startup_nonce="reconciliation-test-startup",
            call_id=call_id,
            status="unknown",
            error=ToolError(
                code="TOOL_OUTCOME_UNCERTAIN",
                message="The test Runner deliberately lost the outcome.",
            ),
            started_at=timestamp,
            finished_at=timestamp,
        )

    async def get_commit_receipt(
        self,
        call_id: str,
        *,
        timeout_seconds: float = 5.0,
    ) -> ToolCommitReceipt | None:
        del timeout_seconds
        self.receipt_queries.append(call_id)
        if self.receipt_query_error is not None:
            raise self.receipt_query_error
        return self.commit_receipts.get(call_id)


class ReceiptQueryUnavailableError(RuntimeError):
    code = "RUNNER_RECEIPT_QUERY_UNAVAILABLE"


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {TEST_SESSION_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }


@contextmanager
def _client(
    database_path: Path,
    *,
    default_headers: bool = True,
) -> Iterator[tuple[TestClient, UnknownRunnerSupervisor]]:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_SESSION_TOKEN),
        cors_origins=[TEST_ORIGIN],
        disk_usage_path=str(database_path.parent.resolve(strict=True)),
    )
    runner = UnknownRunnerSupervisor()
    with TestClient(
        create_app(settings, runner_supervisor=runner),  # type: ignore[arg-type]
        headers=_headers() if default_headers else None,
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


def _create_unknown(client: TestClient) -> tuple[str, dict[str, object]]:
    created = client.post(
        "/api/v1/tasks",
        json={"goal": "produce an uncertain tool outcome"},
    )
    assert created.status_code == 201
    task_id = str(created.json()["task_id"])
    _wait_for_status(client, task_id, "waiting_reconciliation")
    listed = client.get(
        "/api/v1/reconciliations",
        params={"status": "pending", "task_id": task_id},
    )
    assert listed.status_code == 200
    assert listed.headers["cache-control"] == "no-store"
    records = listed.json()
    assert len(records) == 1
    return task_id, records[0]


def _create_receipted_unknown_move(
    client: TestClient,
    runner: UnknownRunnerSupervisor,
    source: Path,
    destination: Path,
) -> tuple[str, dict[str, object], dict[str, object]]:
    source.write_text("receipt-bound compensation", encoding="utf-8")
    created = client.post(
        "/api/v1/tasks",
        json={
            "goal": "move a file and retain an uncertain committed result",
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
    approval = client.get(
        "/api/v1/approvals",
        params={"status": "pending", "task_id": task_id},
    ).json()[0]
    runner.emit_commit_receipt = True
    runner.unknown_file_move_calls_remaining = 1
    approved = client.post(
        f"/api/v1/approvals/{approval['approval_id']}:approve",
        json={"preview_hash": approval["preview_hash"], "scope": "once"},
    )
    assert approved.status_code == 200
    _wait_for_status(client, task_id, "waiting_reconciliation")
    assert not source.exists()
    assert destination.read_text(encoding="utf-8") == "receipt-bound compensation"
    reconciliation = client.get(
        "/api/v1/reconciliations",
        params={"task_id": task_id},
    ).json()[0]
    refreshed = client.post(
        f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}"
        ":refresh-evidence"
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["evidence"]["kind"] == "commit_receipt"
    return task_id, refreshed.json()["reconciliation"], approval


def test_reconciliation_api_requires_auth_origin_and_idempotency_key(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-security.db"
    with _client(database_path, default_headers=False) as (client, runner):
        missing_token = client.get(
            "/api/v1/reconciliations",
            headers={"Origin": TEST_ORIGIN},
        )
        missing_origin = client.post(
            "/api/v1/reconciliations/rec_missing:resolve",
            headers={"Authorization": f"Bearer {TEST_SESSION_TOKEN}"},
            json={
                "outcome": "accepted_unknown",
                "evidence_summary": "No safe conclusion is available.",
            },
        )
        missing_key = client.post(
            "/api/v1/reconciliations/rec_missing:resolve",
            headers=_headers(),
            json={
                "outcome": "accepted_unknown",
                "evidence_summary": "No safe conclusion is available.",
            },
        )

        assert missing_token.status_code == 401
        assert missing_token.json()["code"] == "SESSION_TOKEN_INVALID"
        assert missing_origin.status_code == 403
        assert missing_origin.json()["code"] == "ORIGIN_REQUIRED"
        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        assert runner.calls == []


def test_reconciliation_receipts_survive_restart_and_never_blindly_replay(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-restart.db"
    resolve_key = "resolve-after-human-check"
    attempt_key = "create-new-attempt-once"

    with _client(database_path) as (client, first_runner):
        original_task_id, reconciliation = _create_unknown(client)
        reconciliation_id = str(reconciliation["reconciliation_id"])
        assert reconciliation["status"] == "pending"
        assert reconciliation["can_create_attempt"] is False
        assert len(first_runner.calls) == 1

        blocked = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-attempt",
            headers={"Idempotency-Key": "blocked-before-resolution"},
        )
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "RECONCILIATION_ATTEMPT_NOT_ALLOWED"
        assert len(first_runner.calls) == 1

        resolved = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:resolve",
            headers={"Idempotency-Key": resolve_key},
            json={
                "outcome": "confirmed_no_effect",
                "evidence_summary": "Verified the target resource was unchanged.",
            },
        )
        assert resolved.status_code == 200
        assert resolved.json()["replayed"] is False
        assert resolved.json()["reconciliation"]["can_create_attempt"] is True

    with _client(database_path) as (client, second_runner):
        replayed_resolution = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:resolve",
            headers={"Idempotency-Key": resolve_key},
            json={
                "outcome": "confirmed_no_effect",
                "evidence_summary": "Verified the target resource was unchanged.",
            },
        )
        assert replayed_resolution.status_code == 200
        assert replayed_resolution.json()["replayed"] is True
        assert second_runner.calls == []

        immutable_resolution = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:resolve",
            headers={"Idempotency-Key": "try-to-rewrite-resolution"},
            json={
                "outcome": "accepted_unknown",
                "evidence_summary": "Attempt to replace an immutable verdict.",
            },
        )
        assert immutable_resolution.status_code == 409
        assert immutable_resolution.json()["code"] == (
            "RECONCILIATION_ALREADY_RESOLVED"
        )

        created_attempt = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-attempt",
            headers={"Idempotency-Key": attempt_key},
        )
        assert created_attempt.status_code == 201
        attempt_task_id = str(created_attempt.json()["task"]["task_id"])
        assert attempt_task_id != original_task_id
        assert created_attempt.json()["replayed"] is False
        _wait_for_status(client, attempt_task_id, "waiting_reconciliation")
        assert len(second_runner.calls) == 1

        replayed_attempt = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-attempt",
            headers={"Idempotency-Key": attempt_key},
        )
        assert replayed_attempt.status_code == 201
        assert replayed_attempt.json()["replayed"] is True
        assert replayed_attempt.json()["task"]["task_id"] == attempt_task_id
        assert replayed_attempt.json()["task"]["status"] == "waiting_reconciliation"
        assert len(second_runner.calls) == 1

        original_events = client.get(
            f"/api/v1/tasks/{original_task_id}/events"
        ).json()
        attempt_events = client.get(f"/api/v1/tasks/{attempt_task_id}/events").json()
        assert [event["type"] for event in original_events].count("tool.unknown") == 1
        assert [event["type"] for event in attempt_events].count("tool.unknown") == 1
        assert attempt_events[0]["payload"]["retry_of"] == {
            "reconciliation_id": reconciliation_id,
            "task_id": original_task_id,
            "call_id": reconciliation["call_id"],
            "source_attempt": 1,
        }

        conflicting_key = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:resolve",
            headers={"Idempotency-Key": attempt_key},
            json={
                "outcome": "confirmed_no_effect",
                "evidence_summary": "Verified the target resource was unchanged.",
            },
        )
        assert conflicting_key.status_code == 409
        assert conflicting_key.json()["code"] == "IDEMPOTENCY_KEY_REUSED"


def test_receipt_evidence_refresh_is_content_addressed_and_inconclusive_without_receipt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-no-receipt.db"
    with _client(database_path) as (client, runner):
        task_id, reconciliation = _create_unknown(client)
        reconciliation_id = str(reconciliation["reconciliation_id"])

        first = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:refresh-evidence"
        )
        second = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:refresh-evidence"
        )

        assert first.status_code == 200
        assert first.headers["cache-control"] == "no-store"
        assert first.json()["replayed"] is False
        assert first.json()["evidence"]["kind"] == "no_receipt"
        assert first.json()["evidence"]["commit_receipt"] is None
        assert first.json()["reconciliation"]["can_create_attempt"] is False
        assert second.status_code == 200
        assert second.json()["replayed"] is True
        assert len(second.json()["reconciliation"]["receipt_evidence"]) == 1
        assert len(runner.receipt_queries) == 2

        events = client.get(f"/api/v1/tasks/{task_id}/events").json()
        assert [event["type"] for event in events].count("tool.unknown") == 1


def test_receipt_query_failure_is_sanitized_and_later_receipt_is_positive_evidence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reconciliation-receipt-found.db"
    source = tmp_path / "receipt-source.txt"
    destination = tmp_path / "receipt-destination.txt"
    source.write_text("receipt evidence", encoding="utf-8")

    with _client(database_path) as (client, runner):
        created = client.post(
            "/api/v1/tasks",
            json={
                "goal": "move a file with an uncertain response",
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
        approvals = client.get(
            "/api/v1/approvals",
            params={"status": "pending", "task_id": task_id},
        ).json()
        assert len(approvals) == 1
        runner.emit_commit_receipt = True
        runner.unknown_file_move_calls_remaining = 1
        approved = client.post(
            f"/api/v1/approvals/{approvals[0]['approval_id']}:approve",
            json={"preview_hash": approvals[0]["preview_hash"], "scope": "once"},
        )
        assert approved.status_code == 200
        _wait_for_status(client, task_id, "waiting_reconciliation")
        reconciliation = client.get(
            "/api/v1/reconciliations",
            params={"task_id": task_id},
        ).json()[0]
        reconciliation_id = str(reconciliation["reconciliation_id"])

        runner.receipt_query_error = ReceiptQueryUnavailableError(
            "sensitive local detail must not cross the API"
        )
        failed = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:refresh-evidence"
        )
        assert failed.status_code == 200
        assert failed.json()["evidence"] == {
            "evidence_id": failed.json()["evidence"]["evidence_id"],
            "kind": "query_failed",
            "queried_runner_id": runner.runner_id,
            "commit_receipt": None,
            "error_code": "RUNNER_RECEIPT_QUERY_UNAVAILABLE",
            "observed_at": failed.json()["evidence"]["observed_at"],
        }
        assert "sensitive local detail" not in failed.text

        runner.receipt_query_error = None
        found = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:refresh-evidence"
        )
        body = found.json()
        receipt = body["evidence"]["commit_receipt"]
        assert found.status_code == 200
        assert body["evidence"]["kind"] == "commit_receipt"
        assert receipt["status"] == "committed"
        assert receipt["call_id"] == reconciliation["call_id"]
        assert body["reconciliation"]["status"] == "pending"
        assert body["reconciliation"]["outcome"] is None
        assert body["reconciliation"]["can_create_attempt"] is False
        assert [item["kind"] for item in body["reconciliation"]["receipt_evidence"]] == [
            "commit_receipt",
            "query_failed",
        ]

        detail = client.get(f"/api/v1/reconciliations/{reconciliation_id}").json()
        assert detail["call_error_code"] == "TOOL_OUTCOME_UNCERTAIN"
        assert len(runner.calls) == 1


def test_compensation_requires_positive_commit_receipt_evidence(tmp_path: Path) -> None:
    database_path = tmp_path / "compensation-no-receipt.db"
    with _client(database_path) as (client, runner):
        _, reconciliation = _create_unknown(client)
        reconciliation_id = str(reconciliation["reconciliation_id"])

        missing_key = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-compensation"
        )
        blocked = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-compensation",
            headers={"Idempotency-Key": "compensation-without-receipt"},
        )

        assert missing_key.status_code == 400
        assert missing_key.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
        assert blocked.status_code == 409
        assert blocked.json()["code"] == "RECONCILIATION_COMPENSATION_NOT_ALLOWED"
        assert blocked.json()["reason_code"] == (
            "COMPENSATION_REQUIRES_UNKNOWN_FILE_MOVE"
        )
        assert len(runner.calls) == 1


def test_receipt_bound_compensation_creates_fresh_approved_reverse_move(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "compensation-success.db"
    source = tmp_path / "original-source.txt"
    destination = tmp_path / "original-destination.txt"
    compensation_key = "create-receipt-compensation-once"

    with _client(database_path) as (client, runner):
        original_task_id, reconciliation, original_approval = (
            _create_receipted_unknown_move(client, runner, source, destination)
        )
        reconciliation_id = str(reconciliation["reconciliation_id"])
        original_call_id = str(reconciliation["call_id"])
        receipt_id = str(
            reconciliation["receipt_evidence"][0]["commit_receipt"]["receipt_id"]
        )
        assert reconciliation["can_create_compensation"] is True

        created = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-compensation",
            headers={"Idempotency-Key": compensation_key},
        )
        assert created.status_code == 201
        assert created.headers["cache-control"] == "no-store"
        body = created.json()
        compensation_task_id = str(body["task"]["task_id"])
        assert compensation_task_id != original_task_id
        assert body["replayed"] is False
        assert body["reconciliation"]["can_create_compensation"] is False
        assert body["reconciliation"]["compensation_task_id"] == compensation_task_id
        assert body["reconciliation"]["compensation_receipt_id"] == receipt_id
        assert body["reconciliation"]["compensation_created_at"] is not None
        assert not source.exists()
        assert destination.exists()

        _wait_for_status(client, compensation_task_id, "waiting_approval")
        approvals = client.get(
            "/api/v1/approvals",
            params={"status": "pending", "task_id": compensation_task_id},
        ).json()
        assert len(approvals) == 1
        compensation_approval = approvals[0]
        assert compensation_approval["approval_id"] != original_approval["approval_id"]
        assert compensation_approval["title"] == "撤销先前的单文件移动"
        reverse_source = next(
            resource
            for resource in compensation_approval["resource_scope"]
            if resource["operations"] == ["filesystem.file.move_source"]
        )
        reverse_destination = next(
            resource
            for resource in compensation_approval["resource_scope"]
            if resource["operations"] == ["filesystem.file.move_destination"]
        )
        assert Path(reverse_source["label"]) == destination.resolve()
        assert reverse_source["version"] is not None
        assert Path(reverse_destination["label"]) == source.resolve()
        assert reverse_destination["version"] is None

        replay = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-compensation",
            headers={"Idempotency-Key": compensation_key},
        )
        duplicate = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:create-compensation",
            headers={"Idempotency-Key": "create-second-compensation-denied"},
        )
        assert replay.status_code == 201
        assert replay.json()["replayed"] is True
        assert replay.json()["task"]["task_id"] == compensation_task_id
        assert duplicate.status_code == 409
        assert duplicate.json()["code"] == (
            "RECONCILIATION_COMPENSATION_ALREADY_CREATED"
        )
        assert duplicate.json()["task_id"] == compensation_task_id

        approved = client.post(
            f"/api/v1/approvals/{compensation_approval['approval_id']}:approve",
            json={
                "preview_hash": compensation_approval["preview_hash"],
                "scope": "once",
            },
        )
        assert approved.status_code == 200
        _wait_for_status(client, compensation_task_id, "succeeded")
        assert source.read_text(encoding="utf-8") == "receipt-bound compensation"
        assert not destination.exists()
        assert len(runner.calls) == 2
        assert runner.calls[0] == original_call_id
        assert runner.calls[1] != original_call_id

        compensation_events = client.get(
            f"/api/v1/tasks/{compensation_task_id}/events"
        ).json()
        assert compensation_events[0]["payload"]["compensation_of"] == {
            "reconciliation_id": reconciliation_id,
            "task_id": original_task_id,
            "call_id": original_call_id,
            "receipt_id": receipt_id,
        }
        requested = next(
            event for event in compensation_events if event["type"] == "tool.requested"
        )
        original_requested = next(
            event
            for event in client.get(
                f"/api/v1/tasks/{original_task_id}/events"
            ).json()
            if event["type"] == "tool.requested"
        )
        assert requested["payload"]["call_id"] != original_requested["payload"]["call_id"]
        assert requested["payload"]["idempotency_key_digest"] != (
            original_requested["payload"]["idempotency_key_digest"]
        )


def test_compensation_rejects_a_reverse_source_changed_after_receipt(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "compensation-stale.db"
    source = tmp_path / "stale-original-source.txt"
    destination = tmp_path / "stale-original-destination.txt"

    with _client(database_path) as (client, runner):
        _, reconciliation, _ = _create_receipted_unknown_move(
            client,
            runner,
            source,
            destination,
        )
        destination.write_text("changed after receipt", encoding="utf-8")
        before_calls = len(runner.calls)
        response = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}"
            ":create-compensation",
            headers={"Idempotency-Key": "stale-compensation-is-rejected"},
        )

        assert response.status_code == 409
        assert response.json()["code"] == (
            "RECONCILIATION_COMPENSATION_RESOURCE_CONFLICT"
        )
        assert len(runner.calls) == before_calls
        detail = client.get(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}"
        ).json()
        assert detail["compensation_task_id"] is None
        assert detail["compensation_created_at"] is None
