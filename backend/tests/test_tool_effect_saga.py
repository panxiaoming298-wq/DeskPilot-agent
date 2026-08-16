import time
from pathlib import Path
from typing import Any, cast

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.policy_engine import BuiltinPolicyEngine
from deskpilot.application.runner_client import RunnerExitedError
from deskpilot.application.runner_supervisor import RunnerLease
from deskpilot.core.config import Settings
from deskpilot.main import create_app


def _wait_for_terminal(
    client: TestClient,
    task_id: str,
    *,
    on_first_approval: Any | None = None,
    timeout: float = 12,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    deadline = time.monotonic() + timeout
    resolved: set[str] = set()
    approvals: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] == "waiting_approval":
            pending = client.get(
                "/api/v1/approvals",
                params={"task_id": task_id, "status": "pending"},
            ).json()
            for approval in pending:
                approval_id = str(approval["approval_id"])
                if approval_id in resolved:
                    continue
                if not approvals and on_first_approval is not None:
                    on_first_approval()
                resolved.add(approval_id)
                approvals.append(approval)
                response = client.post(
                    f"/api/v1/approvals/{approval_id}:approve",
                    json={
                        "preview_hash": approval["preview_hash"],
                        "scope": "once",
                    },
                )
                assert response.status_code == 200, response.json()
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return task, approvals
        time.sleep(0.01)
    raise AssertionError("Tool saga did not reach a terminal state")


def _saga_payload(root: Path) -> dict[str, Any]:
    return {
        "goal": "按顺序移动两个互不重叠的本地文件",
        "privacy_mode": "local_only",
        "constraints": ["no_overwrite", "saga"],
        "tool_request": {
            "kind": "file_move_saga",
            "operations": [
                {
                    "operation_id": "first",
                    "source": str(root / "a.txt"),
                    "destination": str(root / "b.txt"),
                },
                {
                    "operation_id": "second",
                    "source": str(root / "c.txt"),
                    "destination": str(root / "d.txt"),
                },
            ],
        },
    }


def _wait_for_pending_approval(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 8,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] == "waiting_approval":
            pending = client.get(
                "/api/v1/approvals",
                params={"task_id": task_id, "status": "pending"},
            ).json()
            if pending:
                return pending[0]
        time.sleep(0.01)
    raise AssertionError("Tool saga did not reach a pending approval")


def _wait_for_status(
    client: TestClient,
    task_id: str,
    expected: str,
    *,
    timeout: float = 8,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] == expected:
            return task
        time.sleep(0.01)
    raise AssertionError(f"Task did not reach {expected}")


def test_versioned_effect_graph_executes_distinct_forward_effects(
    client: TestClient,
    tmp_path: Path,
) -> None:
    (tmp_path / "a.txt").write_text("first", encoding="utf-8")
    (tmp_path / "c.txt").write_text("second", encoding="utf-8")
    created = client.post("/api/v1/tasks", json=_saga_payload(tmp_path))
    assert created.status_code == 201

    task, approvals = _wait_for_terminal(client, created.json()["task_id"])

    assert task["status"] == "succeeded"
    assert len(approvals) == 2
    assert not (tmp_path / "a.txt").exists()
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "c.txt").exists()
    assert (tmp_path / "d.txt").read_text(encoding="utf-8") == "second"

    response = client.get(f"/api/v1/tasks/{task['task_id']}/effect-graph")
    assert response.status_code == 200
    graph = response.json()
    assert graph["schema_version"] == "deskpilot.tool-effect-graph.v1"
    assert graph["status"] == "succeeded"
    assert graph["execution_mode"] == "forward"
    assert [(node["node_key"], node["status"]) for node in graph["nodes"]] == [
        ("first", "succeeded"),
        ("second", "succeeded"),
    ]
    assert {edge["kind"] for edge in graph["edges"]} == {
        "success",
        "compensation_order",
    }
    call_ids = {
        attempt["call_id"]
        for node in graph["nodes"]
        for attempt in node["attempts"]
    }
    effect_ids = {
        effect["effect_id"]
        for node in graph["nodes"]
        for effect in node["effects"]
    }
    receipt_ids = {
        effect["receipt_id"]
        for node in graph["nodes"]
        for effect in node["effects"]
    }
    assert len(call_ids) == len(effect_ids) == len(receipt_ids) == 2
    assert all(receipt_ids)
    graph_text = response.text
    assert str(tmp_path / "a.txt") not in graph_text
    assert str(tmp_path / "d.txt") not in graph_text

    events = client.get(f"/api/v1/tasks/{task['task_id']}/events").json()
    events_by_id = {event["event_id"]: event for event in events}
    for transition in graph["transitions"]:
        event = events_by_id[transition["event_id"]]
        assert event["seq"] == transition["event_seq"]


def test_saga_rejects_overlapping_resources_before_task_creation(
    client: TestClient,
    tmp_path: Path,
) -> None:
    shared = tmp_path / "shared.txt"
    shared.write_text("shared", encoding="utf-8")
    payload = _saga_payload(tmp_path)
    operations = payload["tool_request"]["operations"]
    operations[0]["source"] = str(shared)
    operations[1]["source"] = str(shared)

    response = client.post("/api/v1/tasks", json=payload)

    assert response.status_code == 422
    assert response.json()["code"] == "FILE_MOVE_SAGA_REQUEST_INVALID"


def test_saga_compensates_applied_nodes_in_reverse_with_fresh_approval(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source_a = tmp_path / "a.txt"
    source_c = tmp_path / "c.txt"
    source_a.write_text("first", encoding="utf-8")
    source_c.write_text("second", encoding="utf-8")
    created = client.post("/api/v1/tasks", json=_saga_payload(tmp_path))
    assert created.status_code == 201

    task, approvals = _wait_for_terminal(
        client,
        created.json()["task_id"],
        on_first_approval=source_c.unlink,
    )

    assert task["status"] == "failed"
    assert [approval["title"] for approval in approvals] == [
        "移动单个文件",
        "撤销先前的单文件移动",
    ]
    assert source_a.read_text(encoding="utf-8") == "first"
    assert not (tmp_path / "b.txt").exists()
    assert not source_c.exists()
    assert not (tmp_path / "d.txt").exists()

    graph = client.get(
        f"/api/v1/tasks/{task['task_id']}/effect-graph"
    ).json()
    assert graph["status"] == "compensated"
    assert graph["execution_mode"] == "compensating"
    first, second = graph["nodes"]
    assert first["status"] == "compensated"
    assert second["status"] == "failed"
    assert [attempt["kind"] for attempt in first["attempts"]] == [
        "compensation",
        "forward",
    ]
    assert len({attempt["call_id"] for attempt in first["attempts"]}) == 2
    forward, compensation = sorted(
        first["effects"], key=lambda effect: effect["kind"], reverse=True
    )
    assert forward["kind"] == "forward"
    assert forward["state"] == "compensated"
    assert compensation["kind"] == "compensation"
    assert compensation["state"] == "compensation_applied"
    assert compensation["compensates_effect_id"] == forward["effect_id"]
    events = client.get(f"/api/v1/tasks/{task['task_id']}/events").json()
    terminal = events[-1]
    assert terminal["type"] == "task.failed"
    assert terminal["payload"]["code"] == "SAGA_COMPENSATED"


class _UnknownRunner:
    runner_id = "runner-effect-unknown"
    process_id = 12_345
    is_running = False

    async def start(self) -> None:
        self.is_running = True

    async def stop(self) -> None:
        self.is_running = False

    def ensure_ready(self, *, expected_runner_id: str | None = None) -> RunnerLease:
        if expected_runner_id is not None and expected_runner_id != self.runner_id:
            raise RunnerExitedError("Runner generation changed")
        return RunnerLease(
            runner_id=self.runner_id,
            generation=1,
            client=cast(Any, self),
        )

    async def call_tool(self, **_: object) -> None:
        raise RunnerExitedError("Runner exited after dispatch")


def test_unknown_attempt_blocks_graph_and_never_starts_compensation(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    (tmp_path / "a.txt").write_text("first", encoding="utf-8")
    (tmp_path / "c.txt").write_text("second", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unknown.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(
        create_app(settings, runner_supervisor=cast(Any, _UnknownRunner())),
        headers=headers,
    ) as client:
        created = client.post("/api/v1/tasks", json=_saga_payload(tmp_path))
        task_id = created.json()["task_id"]
        approval = _wait_for_pending_approval(client, task_id)
        approved = client.post(
            f"/api/v1/approvals/{approval['approval_id']}:approve",
            json={"preview_hash": approval["preview_hash"], "scope": "once"},
        )
        assert approved.status_code == 200
        task = _wait_for_status(client, task_id, "waiting_reconciliation")
        graph = client.get(
            f"/api/v1/tasks/{task['task_id']}/effect-graph"
        ).json()
        reconciliation = client.get(
            "/api/v1/reconciliations",
            params={"task_id": task_id, "status": "pending"},
        ).json()[0]
        resolved = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}:resolve",
            headers={"Idempotency-Key": "resolve-accepted-unknown"},
            json={
                "outcome": "accepted_unknown",
                "evidence_summary": "外部效果仍无法证明，显式终止原图。",
            },
        )
        assert resolved.status_code == 200, resolved.json()
        unsafe_continue = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}:recover-graph",
            headers={"Idempotency-Key": "reject-accepted-unknown-continue"},
            json={"action": "continue"},
        )
        assert unsafe_continue.status_code == 409
        assert (
            unsafe_continue.json()["reason_code"]
            == "ACCEPTED_UNKNOWN_CANNOT_CONTINUE"
        )
        recovered = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}:recover-graph",
            headers={"Idempotency-Key": "terminate-blocked-graph"},
            json={"action": "terminate"},
        )
        assert recovered.status_code == 200, recovered.json()
        task = recovered.json()["task"]
        terminated_graph = recovered.json()["graph"]

    assert task["status"] == "failed"
    assert reconciliation["graph_recovery_status"] == "pending"
    assert recovered.json()["reconciliation"]["graph_recovery_status"] == "applied"
    assert terminated_graph["status"] == "failed"
    assert terminated_graph["nodes"][0]["status"] == "unknown"
    assert graph["status"] == "blocked_unknown"
    assert graph["nodes"][0]["status"] == "unknown"
    assert graph["nodes"][1]["status"] == "pending"
    assert [
        attempt["kind"]
        for node in graph["nodes"]
        for attempt in node["attempts"]
    ] == ["forward"]


def test_confirmed_success_resumes_graph_without_rewriting_unknown_call(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'recover-success.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(
        create_app(settings, runner_supervisor=cast(Any, _UnknownRunner())),
        headers=headers,
    ) as client:
        created = client.post("/api/v1/tasks", json={"goal": "读取磁盘容量"})
        task_id = created.json()["task_id"]
        _wait_for_status(client, task_id, "waiting_reconciliation")
        reconciliation = client.get(
            "/api/v1/reconciliations",
            params={"task_id": task_id, "status": "pending"},
        ).json()[0]
        resolved = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}:resolve",
            headers={"Idempotency-Key": "resolve-confirmed-success"},
            json={
                "outcome": "confirmed_succeeded",
                "evidence_summary": "只读调用已由人工证据确认完成。",
            },
        )
        assert resolved.status_code == 200, resolved.json()
        recovered = client.post(
            f"/api/v1/reconciliations/{reconciliation['reconciliation_id']}:recover-graph",
            headers={"Idempotency-Key": "continue-confirmed-success"},
            json={"action": "continue"},
        )
        assert recovered.status_code == 200, recovered.json()
        task, _ = _wait_for_terminal(client, task_id)
        graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
        events = client.get(f"/api/v1/tasks/{task_id}/events").json()

    assert task["status"] == "succeeded"
    assert graph["status"] == "succeeded"
    assert graph["nodes"][0]["attempts"][0]["status"] == "succeeded"
    assert sum(event["type"] == "tool.unknown" for event in events) == 1
    assert not any(event["type"] == "tool.completed" for event in events)


def test_confirmed_failure_cannot_continue_without_no_effect_proof(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'recover-failed.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(
        create_app(settings, runner_supervisor=cast(Any, _UnknownRunner())),
        headers=headers,
    ) as client:
        task_id = client.post(
            "/api/v1/tasks",
            json={"goal": "确认失败但不能证明无副作用"},
        ).json()["task_id"]
        _wait_for_status(client, task_id, "waiting_reconciliation")
        reconciliation = client.get(
            "/api/v1/reconciliations",
            params={"task_id": task_id, "status": "pending"},
        ).json()[0]
        reconciliation_id = reconciliation["reconciliation_id"]
        resolved = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:resolve",
            headers={"Idempotency-Key": "resolve-confirmed-failed"},
            json={
                "outcome": "confirmed_failed",
                "evidence_summary": "已确认调用失败，但没有无副作用证明。",
            },
        )
        assert resolved.status_code == 200, resolved.json()

        unsafe_continue = client.post(
            f"/api/v1/reconciliations/{reconciliation_id}:recover-graph",
            headers={"Idempotency-Key": "reject-confirmed-failed-continue"},
            json={"action": "continue"},
        )

        assert unsafe_continue.status_code == 409
        assert (
            unsafe_continue.json()["reason_code"]
            == "CONFIRMED_FAILED_CANNOT_PROVE_NO_EFFECT"
        )


def test_policy_denial_terminates_the_bound_effect_graph(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    (tmp_path / "a.txt").write_text("first", encoding="utf-8")
    (tmp_path / "c.txt").write_text("second", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'denied.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(
        create_app(settings, policy_engine=BuiltinPolicyEngine()),
        headers=headers,
    ) as client:
        created = client.post("/api/v1/tasks", json=_saga_payload(tmp_path))
        task, approvals = _wait_for_terminal(client, created.json()["task_id"])
        graph = client.get(
            f"/api/v1/tasks/{task['task_id']}/effect-graph"
        ).json()
        events = client.get(f"/api/v1/tasks/{task['task_id']}/events").json()

    assert task["status"] == "failed"
    assert approvals == []
    assert graph["status"] == "failed"
    assert graph["failure_node_id"] == graph["nodes"][0]["node_id"]
    assert [node["status"] for node in graph["nodes"]] == ["failed", "pending"]
    assert graph["nodes"][0]["attempts"][0]["status"] == "failed"
    assert events[-1]["payload"]["code"] == "POLICY_DENIED"
    assert events[-1]["payload"]["error_type"] == "PolicyDeniedError"


def test_multistep_graph_resumes_exact_node_across_two_api_restarts(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    (tmp_path / "a.txt").write_text("first", encoding="utf-8")
    (tmp_path / "c.txt").write_text("second", encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'restart.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
        runner_commit_receipt_database_path=str(tmp_path / "runner-receipts.db"),
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(create_app(settings), headers=headers) as first_client:
        created = first_client.post("/api/v1/tasks", json=_saga_payload(tmp_path))
        task_id = str(created.json()["task_id"])
        first_approval = _wait_for_pending_approval(first_client, task_id)

    with TestClient(create_app(settings), headers=headers) as second_client:
        approved = second_client.post(
            f"/api/v1/approvals/{first_approval['approval_id']}:approve",
            json={
                "preview_hash": first_approval["preview_hash"],
                "scope": "once",
            },
        )
        assert approved.status_code == 200, approved.json()
        second_approval = _wait_for_pending_approval(second_client, task_id)
        assert second_approval["call_id"] != first_approval["call_id"]

    with TestClient(create_app(settings), headers=headers) as third_client:
        approved = third_client.post(
            f"/api/v1/approvals/{second_approval['approval_id']}:approve",
            json={
                "preview_hash": second_approval["preview_hash"],
                "scope": "once",
            },
        )
        assert approved.status_code == 200, approved.json()
        task, approvals = _wait_for_terminal(third_client, task_id)
        graph = third_client.get(
            f"/api/v1/tasks/{task_id}/effect-graph"
        ).json()

    assert task["status"] == "succeeded"
    assert approvals == []
    assert graph["status"] == "succeeded"
    assert [node["status"] for node in graph["nodes"]] == [
        "succeeded",
        "succeeded",
    ]
