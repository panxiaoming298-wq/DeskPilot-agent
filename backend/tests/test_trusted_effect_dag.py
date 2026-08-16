import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.runner_supervisor import RunnerLease
from deskpilot.core.config import Settings
from deskpilot.domain.policy import ToolAuthorizationGrant
from deskpilot.main import create_app
from deskpilot.runner.ipc_protocol import ToolCallResult


def _payload(root: Path) -> dict[str, Any]:
    return {
        "goal": "并行移动两个文件，完成后再移动第三个文件",
        "privacy_mode": "local_only",
        "constraints": ["trusted_dag", "no_overwrite"],
        "tool_request": {
            "kind": "file_move_dag",
            "operations": [
                {
                    "operation_id": "left",
                    "source": str(root / "a.txt"),
                    "destination": str(root / "b.txt"),
                    "depends_on": [],
                },
                {
                    "operation_id": "right",
                    "source": str(root / "c.txt"),
                    "destination": str(root / "d.txt"),
                    "depends_on": [],
                },
                {
                    "operation_id": "join",
                    "source": str(root / "e.txt"),
                    "destination": str(root / "f.txt"),
                    "depends_on": ["left", "right"],
                },
            ],
        },
    }


def _guarded_payload(root: Path, maximum_used_percent: float) -> dict[str, Any]:
    return {
        "goal": "目标磁盘压力允许时移动文件，否则安全推迟",
        "privacy_mode": "local_only",
        "constraints": ["trusted_conditional_graph", "no_overwrite"],
        "tool_request": {
            "kind": "disk_pressure_guarded_file_move",
            "source": str(root / "guarded-source.txt"),
            "destination": str(root / "guarded-destination.txt"),
            "maximum_used_percent": maximum_used_percent,
        },
    }


class BlockingDagRunner:
    def __init__(
        self,
        expected_calls: int,
        *,
        cancel_observer: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self.expected_calls = expected_calls
        self.cancel_observer = cancel_observer
        self.started = threading.Event()
        self._release: asyncio.Event | None = None
        self._started_count = 0
        self._running = False
        self.cancelled: list[tuple[str, str, str | None]] = []
        self.intent_preceded_cancel: list[bool] = []

    @property
    def runner_id(self) -> str:
        return "runner-dag-cancel"

    @property
    def process_id(self) -> int:
        return 4243

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self._running = True
        self._release = asyncio.Event()

    async def stop(self) -> None:
        self._running = False
        if self._release is not None:
            self._release.set()

    def ensure_ready(self, *, expected_runner_id: str | None = None) -> RunnerLease:
        if expected_runner_id is not None:
            assert expected_runner_id == self.runner_id
        assert self._running
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
        assert expected_runner_id == self.runner_id
        assert call_id is not None
        self._started_count += 1
        if self._started_count == self.expected_calls:
            self.started.set()
        release = self._release
        assert release is not None
        await release.wait()
        timestamp = datetime.now(UTC)
        return ToolCallResult(
            runner_id=self.runner_id,
            startup_nonce="dag-cancel-startup-nonce",
            call_id=call_id,
            status="cancelled",
            error={
                "code": "TOOL_CANCELLED",
                "message": "The DAG call was cancelled before commit.",
                "retryable": False,
            },
            started_at=timestamp,
            finished_at=timestamp,
        )

    async def cancel_call(
        self,
        call_id: str,
        reason: str,
        *,
        expected_runner_id: str | None = None,
    ) -> None:
        if self.cancel_observer is not None:
            self.intent_preceded_cancel.append(await self.cancel_observer())
        self.cancelled.append((call_id, reason, expected_runner_id))
        release = self._release
        assert release is not None
        if len(self.cancelled) == self.expected_calls:
            release.set()


def _wait_for_pending(
    client: TestClient,
    task_id: str,
    *,
    minimum: int = 1,
    timeout: float = 12,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = client.get(
            "/api/v1/approvals",
            params={"task_id": task_id, "status": "pending"},
        ).json()
        if len(pending) >= minimum:
            return pending
        time.sleep(0.01)
    task = client.get(f"/api/v1/tasks/{task_id}").json()
    graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()
    raise AssertionError(
        f"Trusted DAG did not expose approvals: task={task}, graph={graph}, "
        f"last_events={events[-6:]}"
    )


def _approve(client: TestClient, approval: dict[str, Any]) -> None:
    response = client.post(
        f"/api/v1/approvals/{approval['approval_id']}:approve",
        json={"preview_hash": approval["preview_hash"], "scope": "once"},
    )
    assert response.status_code == 200, response.json()


def _reject(client: TestClient, approval: dict[str, Any]) -> dict[str, Any]:
    response = client.post(
        f"/api/v1/approvals/{approval['approval_id']}:reject",
        json={
            "preview_hash": approval["preview_hash"],
            "scope": "once",
            "reason": "batch rejected by user",
        },
    )
    assert response.status_code == 200, response.json()
    return response.json()


def _wait_for_terminal(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 20,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        task = client.get(f"/api/v1/tasks/{task_id}").json()
        if task["status"] in {"succeeded", "failed", "cancelled"}:
            return task
        time.sleep(0.01)
    raise AssertionError("Trusted DAG did not reach a terminal state")


def _wait_for_bound_checkpoint(
    client: TestClient,
    task_id: str,
    *,
    timeout: float = 5,
) -> None:
    async def probe() -> bool:
        loaded = await client.app.state.task_service.load_task_checkpoints()
        task = await client.app.state.task_service.get_task(task_id)
        return (
            loaded.invalid_task_ids == ()
            and len(loaded.checkpoints) == 1
            and loaded.checkpoints[0].event_seq == task.last_event_seq
            and bool(loaded.checkpoints[0].payload.dag_approval_ids)
        )

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if client.portal.call(probe):
            return
        time.sleep(0.01)
    raise AssertionError("Trusted DAG checkpoint was not rebound to its approval events")


def _assert_dag_expiry_workers(
    client: TestClient,
    task_id: str,
    approval_ids: set[str],
) -> None:
    async def probe() -> tuple[set[str], bool]:
        runtime = client.app.state.processor._runtimes[task_id]
        workers = runtime.dag_approval_expiry_workers
        return set(workers), all(not worker.done() for worker in workers.values())

    scheduled_ids, all_active = client.portal.call(probe)
    assert scheduled_ids == approval_ids
    assert all_active


def test_trusted_v2_dag_binds_parallel_nodes_to_ledger_approval_and_receipt(
    client: TestClient,
    tmp_path: Path,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    created = client.post("/api/v1/tasks", json=_payload(tmp_path))
    assert created.status_code == 201, created.json()
    task_id = created.json()["task_id"]

    root_approvals = _wait_for_pending(client, task_id, minimum=2)
    assert len(root_approvals) == 2
    _approve(client, root_approvals[0])
    assert client.get(f"/api/v1/tasks/{task_id}").json()["status"] == "waiting_approval"
    _approve(client, root_approvals[1])
    join_approval = _wait_for_pending(client, task_id)
    assert len(join_approval) == 1
    _approve(client, join_approval[0])
    task = _wait_for_terminal(client, task_id)

    graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
    assert task["status"] == "succeeded"
    assert graph["schema_version"] == "deskpilot.tool-effect-graph.v2"
    assert graph["status"] == "succeeded"
    assert [node["status"] for node in graph["nodes"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert all(len(node["attempts"]) == 1 for node in graph["nodes"])
    assert all(node["effects"][0]["receipt_id"] for node in graph["nodes"])
    assert (tmp_path / "b.txt").read_text(encoding="utf-8") == "left"
    assert (tmp_path / "d.txt").read_text(encoding="utf-8") == "right"
    assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "join"


def test_disk_pressure_business_graph_selects_move_and_binds_approval(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "guarded-source.txt"
    destination = tmp_path / "guarded-destination.txt"
    source.write_text("move when trusted threshold permits", encoding="utf-8")

    created = client.post("/api/v1/tasks", json=_guarded_payload(tmp_path, 100))
    assert created.status_code == 201, created.json()
    task_id = created.json()["task_id"]
    approvals = _wait_for_pending(client, task_id)
    assert len(approvals) == 1
    assert approvals[0]["tool_name"] == "file.move"
    _approve(client, approvals[0])

    task = _wait_for_terminal(client, task_id)
    graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()

    assert task["status"] == "succeeded"
    assert graph["status"] == "succeeded"
    assert [node["node_key"] for node in graph["nodes"]] == [
        "inspect_capacity",
        "move_file",
        "confirm_deferred",
    ]
    assert [node["status"] for node in graph["nodes"]] == [
        "succeeded",
        "succeeded",
        "skipped",
    ]
    assert graph["branch_decisions"][0]["decision_key"] == "disk_pressure_route"
    assert graph["branch_decisions"][0]["outcome"] == "move"
    branch_event = next(event for event in events if event["type"] == "effect.branch.decided")
    assert branch_event["payload"]["outcome"] == "move"
    assert source.exists() is False
    assert destination.read_text(encoding="utf-8") == "move when trusted threshold permits"


def test_disk_pressure_business_graph_selects_defer_without_write_approval(
    client: TestClient,
    tmp_path: Path,
) -> None:
    source = tmp_path / "guarded-source.txt"
    destination = tmp_path / "guarded-destination.txt"
    source.write_text("leave in place under disk pressure", encoding="utf-8")

    created = client.post("/api/v1/tasks", json=_guarded_payload(tmp_path, 0))
    assert created.status_code == 201, created.json()
    task_id = created.json()["task_id"]
    task = _wait_for_terminal(client, task_id)
    graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
    approvals = client.get("/api/v1/approvals", params={"task_id": task_id}).json()

    assert task["status"] == "succeeded"
    assert graph["status"] == "succeeded"
    assert [node["status"] for node in graph["nodes"]] == [
        "succeeded",
        "skipped",
        "succeeded",
    ]
    assert graph["branch_decisions"][0]["outcome"] == "defer"
    assert approvals == []
    assert source.read_text(encoding="utf-8") == "leave in place under disk pressure"
    assert destination.exists() is False


def test_task_cancel_broadcasts_to_in_flight_dag_runner_calls(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    runner = BlockingDagRunner(expected_calls=2)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'dag-cancel.db').as_posix()}",
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
        create_app(settings, runner_supervisor=runner),  # type: ignore[arg-type]
        headers=headers,
    ) as test_client:
        task_id = test_client.post("/api/v1/tasks", json=_payload(tmp_path)).json()[
            "task_id"
        ]
        approvals = _wait_for_pending(test_client, task_id, minimum=2)
        for approval in approvals:
            _approve(test_client, approval)
        assert runner.started.wait(timeout=5)

        cancelled = test_client.post(
            f"/api/v1/tasks/{task_id}:cancel",
            json={"reason": "stop the active graph"},
        )
        graph = test_client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
        events = test_client.get(f"/api/v1/tasks/{task_id}/events").json()

    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["status"] == "cancelled"
    assert graph["status"] == "cancelled"
    assert graph["cancel_requested_at"] is not None
    assert [node["status"] for node in graph["nodes"]] == [
        "cancelled",
        "cancelled",
        "skipped",
    ]
    assert len(runner.cancelled) == 2
    assert {expected for _, _, expected in runner.cancelled} == {runner.runner_id}
    assert {reason for _, reason, _ in runner.cancelled} == {"stop the active graph"}
    assert events[-1]["type"] == "task.cancelled"
    assert events[-1]["payload"]["reason"] == "stop the active graph"
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "c.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "d.txt").exists()


def test_remote_api_routes_cancel_to_live_dag_owner_runner_calls(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    database_path = tmp_path / "remote-dag-cancel.db"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[allowed_origin],
        effect_graph_control_poll_interval_seconds=0.01,
        effect_graph_control_request_timeout_seconds=5,
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": allowed_origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    owner_runner = BlockingDagRunner(expected_calls=2)
    requester_runner = BlockingDagRunner(expected_calls=99)
    owner_app = create_app(settings, runner_supervisor=owner_runner)  # type: ignore[arg-type]
    requester_app = create_app(  # type: ignore[arg-type]
        settings,
        runner_supervisor=requester_runner,
    )

    with (
        TestClient(owner_app, headers=headers) as owner_client,
        TestClient(requester_app, headers=headers) as requester_client,
    ):
        task_id = owner_client.post("/api/v1/tasks", json=_payload(tmp_path)).json()[
            "task_id"
        ]
        approvals = _wait_for_pending(owner_client, task_id, minimum=2)
        for approval in approvals:
            _approve(owner_client, approval)
        assert owner_runner.started.wait(timeout=5)

        active_graph = owner_client.get(
            f"/api/v1/tasks/{task_id}/effect-graph"
        ).json()
        owner_id = owner_app.state.processor.dag_owner_id
        requester_id = requester_app.state.processor.dag_owner_id
        assert owner_id != requester_id
        assert active_graph["lease_owner_id"] == owner_id
        assert owner_app.state.processor.has_runtime(task_id)
        assert not requester_app.state.processor.has_runtime(task_id)

        async def observe_cancel_intent() -> bool:
            graph = await owner_app.state.task_service.get_effect_graph(task_id)
            return (
                graph.cancel_requested_at is not None
                and graph.fencing_token == active_graph["fencing_token"]
            )

        owner_runner.cancel_observer = observe_cancel_intent
        cancelled = requester_client.post(
            f"/api/v1/tasks/{task_id}:cancel",
            json={"reason": "cancel through the remote API"},
        )
        graph = owner_client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()

    assert cancelled.status_code == 200, cancelled.json()
    assert cancelled.json()["status"] == "cancelled"
    assert graph["status"] == "cancelled"
    assert graph["cancel_requested_at"] is not None
    assert len(owner_runner.cancelled) == 2
    assert requester_runner.cancelled == []
    assert owner_runner.intent_preceded_cancel == [True, True]
    assert {reason for _, reason, _ in owner_runner.cancelled} == {
        "cancel through the remote API"
    }
    assert {expected for _, _, expected in owner_runner.cancelled} == {
        owner_runner.runner_id
    }
    assert [node["status"] for node in graph["nodes"]] == [
        "cancelled",
        "cancelled",
        "skipped",
    ]
    assert (tmp_path / "a.txt").exists()
    assert (tmp_path / "c.txt").exists()
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "d.txt").exists()


def test_trusted_v2_dag_consumes_parallel_compensation_wave_with_fresh_approvals(
    client: TestClient,
    tmp_path: Path,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    task_id = client.post("/api/v1/tasks", json=_payload(tmp_path)).json()["task_id"]
    root_approvals = _wait_for_pending(client, task_id, minimum=2)
    for approval in root_approvals:
        _approve(client, approval)

    join_approval = _wait_for_pending(client, task_id)
    (tmp_path / "e.txt").unlink()
    _approve(client, join_approval[0])
    compensation_approvals = _wait_for_pending(client, task_id, minimum=2)
    assert {approval["title"] for approval in compensation_approvals} == {
        "撤销 DAG 节点的文件移动"
    }
    for approval in compensation_approvals:
        _approve(client, approval)
    task = _wait_for_terminal(client, task_id)

    graph = client.get(f"/api/v1/tasks/{task_id}/effect-graph").json()
    assert task["status"] == "failed"
    terminal_events = client.get(f"/api/v1/tasks/{task_id}/events").json()[-10:]
    assert graph["status"] == "compensated", "nodes=" + ",".join(
        node["status"] for node in graph["nodes"]
    ) + ";events=" + "|".join(
        f"{event['type']}:{event['payload'].get('code')}" for event in terminal_events
    )
    assert [node["status"] for node in graph["nodes"]] == [
        "compensated",
        "compensated",
        "failed",
    ]
    assert [attempt["kind"] for attempt in graph["nodes"][0]["attempts"]] == [
        "compensation",
        "forward",
    ]
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "left"
    assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "right"
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "d.txt").exists()


def test_trusted_v2_dag_rejection_cancels_the_entire_unconsumed_approval_batch(
    client: TestClient,
    tmp_path: Path,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    task_id = client.post("/api/v1/tasks", json=_payload(tmp_path)).json()["task_id"]
    root_approvals = _wait_for_pending(client, task_id, minimum=2)

    result = _reject(client, root_approvals[0])
    approvals = client.get(
        "/api/v1/approvals",
        params={"task_id": task_id},
    ).json()
    events = client.get(f"/api/v1/tasks/{task_id}/events").json()

    assert result["task"]["status"] == "cancelled"
    assert {approval["status"] for approval in approvals} == {
        "rejected",
        "cancelled",
    }
    assert sum(event["type"] == "tool.cancelled" for event in events) == 2
    assert (tmp_path / "a.txt").read_text(encoding="utf-8") == "left"
    assert (tmp_path / "c.txt").read_text(encoding="utf-8") == "right"
    assert not (tmp_path / "b.txt").exists()
    assert not (tmp_path / "d.txt").exists()


def test_trusted_v2_dag_recovers_batch_approvals_across_api_restarts(
    tmp_path: Path,
    allowed_origin: str,
    session_token: str,
) -> None:
    for name, content in (("a.txt", "left"), ("c.txt", "right"), ("e.txt", "join")):
        (tmp_path / name).write_text(content, encoding="utf-8")
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'dag-restart.db').as_posix()}",
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
    with TestClient(create_app(settings), headers=headers) as first:
        task_id = first.post("/api/v1/tasks", json=_payload(tmp_path)).json()["task_id"]
        root_approvals = _wait_for_pending(first, task_id, minimum=2)
        _wait_for_bound_checkpoint(first, task_id)
        _assert_dag_expiry_workers(
            first,
            task_id,
            {approval["approval_id"] for approval in root_approvals},
        )

    with TestClient(create_app(settings), headers=headers) as second:
        _assert_dag_expiry_workers(
            second,
            task_id,
            {approval["approval_id"] for approval in root_approvals},
        )
        for approval in root_approvals:
            _approve(second, approval)
        join_approval = _wait_for_pending(second, task_id)
        _wait_for_bound_checkpoint(second, task_id)

    with TestClient(create_app(settings), headers=headers) as third:
        _approve(third, join_approval[0])
        task = _wait_for_terminal(third, task_id)
        graph = third.get(f"/api/v1/tasks/{task_id}/effect-graph").json()

    assert task["status"] == "succeeded"
    assert graph["status"] == "succeeded"
    assert all(node["status"] == "succeeded" for node in graph["nodes"])
