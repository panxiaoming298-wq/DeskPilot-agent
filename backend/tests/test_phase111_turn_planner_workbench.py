import asyncio
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import JsonValue, SecretStr
from sqlalchemy import func, select

from deskpilot.application.model_gateway import ModelTimeoutError
from deskpilot.application.route_recipe_catalog import RouteId, RouteRecipeCatalog
from deskpilot.application.task_workbench_service import TaskWorkbenchService
from deskpilot.application.turn_planner_runtime import TurnPlannerRuntime
from deskpilot.application.workbench_runtime_coordinator import (
    WorkbenchRuntimeCoordinator,
)
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.research import SearchProviderResult, SearchRequest
from deskpilot.domain.task_workbench import ContinueConversationTurn, TaskWorkbenchRead
from deskpilot.domain.turn_planning import TurnPlanningRead
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceNodeTestRead,
    WorkspaceNodeTestSnapshot,
    WorkspacePythonTestRead,
    WorkspacePythonTestSnapshot,
)
from deskpilot.infrastructure.models import TurnPlannerRunRecord
from deskpilot.main import create_app
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools.workspace_checks import WorkspaceCheckInput

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "stage-111-session-token-with-at-least-32-chars"
TURN_PLANNER_SCHEMA = "turn_planner_decision"
PRIVATE_TURN_PLANNING_KEYS = frozenset(
    {
        "offers",
        "selected_offers",
        "response_manifest",
        "proposal_manifest",
        "parameter_bindings",
        "claim_owner_id",
        "claim_fencing_token",
        "claim_expires_at",
        "request_dispatched_at",
        "provider",
        "provider_snapshot_digest",
        "planner_agent",
        "execution_agents",
        "expected_plan",
        "trusted_recipe",
        "parameter_specs",
        "task_contract",
        "capabilities",
        "budget",
        "reservation_digest",
        "fallback_candidate_digest",
        "offer",
        "plan",
    }
)
PRIVATE_TASK_LOOP_KEYS = frozenset(
    {
        "source",
        "steps",
        "task_contract",
        "draft_plan",
        "expected_plan",
        "parameter_bindings",
        "node_mappings",
        "parameters",
        "offer_key",
        "offer_id",
        "offer_digest",
        "user_message_id",
        "user_message_digest",
        "provider",
        "planner_agent",
        "execution_agents",
    }
)

PlannerMode = Literal[
    "single",
    "multi",
    "needs_input",
    "unsupported",
    "timeout",
    "schema",
    "unknown_offer",
    "provider_failure",
    "blocking_single",
    "invalid_patch",
]


class PlannerScriptProvider(FakeModelProvider):
    """Return one explicit Turn Planner decision and count every model dispatch."""

    def __init__(self, mode: PlannerMode) -> None:
        super().__init__()
        self.mode = mode
        self.total_calls = 0
        self.planner_calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.total_calls += 1
        if request.output_schema is None or request.output_schema.name != TURN_PLANNER_SCHEMA:
            return await super().complete(request)
        self.planner_calls += 1
        if self.mode == "timeout":
            raise ModelTimeoutError(
                "scripted Turn Planner timeout",
                provider_id=self.descriptor.provider_id,
            )
        if self.mode == "provider_failure":
            raise RuntimeError("scripted unavailable local Provider")
        if self.mode == "blocking_single":
            self.started.set()
            await self.release.wait()

        response = await super().complete(request)
        output = self._planner_output(request)
        return response.model_copy(update={"structured_output": output})

    def _planner_output(self, request: ModelRequest) -> dict[str, JsonValue]:
        payload = cast(dict[str, object], json.loads(request.messages[-1].content))
        if self.mode == "schema":
            return cast(
                dict[str, JsonValue],
                {
                    "schema_version": "deskpilot.turn-planner-decision.v1",
                    "kind": "propose_steps",
                    "steps": [],
                },
            )
        if self.mode == "unknown_offer":
            return _steps_decision(
                (
                    {
                        "offer_key": f"ofk_{'0' * 64}",
                        "parameters": [{"name": "query", "value": "alpha"}],
                    },
                )
            )
        if self.mode == "unsupported":
            return cast(
                dict[str, JsonValue],
                {
                    "schema_version": "deskpilot.turn-planner-decision.v1",
                    "kind": "unsupported",
                },
            )
        if self.mode == "invalid_patch":
            return _steps_decision(
                (
                    {
                        "offer_key": _offer_key_for_parameter(payload, "changes_json"),
                        "parameters": [{"name": "changes_json", "value": "not-json"}],
                    },
                )
            )

        query_offer = _offer_key_for_parameter(payload, "query")
        if self.mode == "needs_input":
            return cast(
                dict[str, JsonValue],
                {
                    "schema_version": "deskpilot.turn-planner-decision.v1",
                    "kind": "needs_input",
                    "offer_key": query_offer,
                    "missing_parameters": ["query"],
                },
            )
        query_step: dict[str, JsonValue] = {
            "offer_key": query_offer,
            "parameters": [{"name": "query", "value": "alpha"}],
        }
        if self.mode == "multi":
            text_offer = _offer_key_for_parameter(payload, "text")
            return _steps_decision(
                (
                    query_step,
                    {
                        "offer_key": text_offer,
                        "parameters": [{"name": "text", "value": "alpha"}],
                    },
                )
            )
        return _steps_decision((query_step,))


def _steps_decision(
    steps: tuple[dict[str, JsonValue], ...],
) -> dict[str, JsonValue]:
    return cast(
        dict[str, JsonValue],
        {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": list(steps),
        },
    )


def _offer_key_for_parameter(payload: dict[str, object], parameter_name: str) -> str:
    raw_offers = cast(list[object], payload["offers"])
    for raw_offer in raw_offers:
        offer = cast(dict[str, object], raw_offer)
        specs = cast(list[object], offer["parameter_specs"])
        names = {
            cast(str, cast(dict[str, object], raw_spec)["parameter_name"]) for raw_spec in specs
        }
        if names == {parameter_name}:
            reference = cast(dict[str, object], offer["offer"])
            return cast(str, reference["offer_key"])
    raise AssertionError(f"No precompiled Offer exposes only {parameter_name!r}")


class NoopSearchProvider:
    provider_id = "phase-111-noop-search"

    async def search(self, request: SearchRequest) -> SearchProviderResult:
        raise AssertionError(f"Search was not expected for {request.query!r}")


class EnabledWorkspaceChecks:
    @property
    def enabled(self) -> bool:
        return True

    def run(self, snapshot: WorkspaceCheckInput) -> WorkspaceCheckRead:
        raise AssertionError(f"Workspace check was not expected for {snapshot.relative_path!r}")


class EnabledPythonTests:
    @property
    def enabled(self) -> bool:
        return True

    def run(self, snapshot: WorkspacePythonTestSnapshot) -> WorkspacePythonTestRead:
        raise AssertionError(f"Python test was not expected for {snapshot.test_path!r}")


class EnabledNodeTests:
    @property
    def enabled(self) -> bool:
        return True

    def run(self, snapshot: WorkspaceNodeTestSnapshot) -> WorkspaceNodeTestRead:
        raise AssertionError(f"Node test was not expected for {snapshot.test_path!r}")


@contextmanager
def _open_client(
    tmp_path: Path,
    provider: PlannerScriptProvider,
    *,
    name: str,
    automatic: bool = False,
) -> Iterator[tuple[TestClient, Path]]:
    workspace = tmp_path / f"workspace-{name}"
    workspace.mkdir(exist_ok=True)
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / f'{name}.db').as_posix()}",
        artifact_workspace_root=str(tmp_path / f"artifacts-{name}"),
        conversation_workspace_root=str(workspace),
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        runner_commit_receipt_database_path=str(tmp_path / f"receipts-{name}.db"),
        research_runtime_enabled=True,
        workbench_runtime_enabled=automatic,
        workbench_runtime_poll_interval_seconds=0.01,
        workbench_runtime_claim_ttl_seconds=5,
        model_gateway_policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id="fake-local"),),
        ),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(
        settings,
        model_provider=provider,
        search_provider=NoopSearchProvider(),
        workspace_check_runtime=EnabledWorkspaceChecks(),
        workspace_python_test_runtime=EnabledPythonTests(),
        workspace_node_test_runtime=EnabledNodeTests(),
    )
    with TestClient(app, headers=headers) as client:
        yield client, workspace


def _create_unmatched_turn(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/v1/conversation-turns",
        json={"message": "请为 alpha 做一次受限资料匹配"},
    )
    assert response.status_code == 201, response.text
    body = cast(dict[str, object], response.json())
    assert body["stage"] == "interpreting"
    planning = cast(dict[str, object], body["turn_planning"])
    _assert_minimized_turn_planning(planning)
    run = cast(dict[str, object], planning["run"])
    assert run["status"] == "prepared"
    return body


def _wait_for_terminal_planner(
    client: TestClient,
    task_id: str,
    *,
    timeout_seconds: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}/workbench")
        assert response.status_code == 200, response.text
        latest = cast(dict[str, object], response.json())
        planning = cast(dict[str, object] | None, latest.get("turn_planning"))
        if planning is not None:
            _assert_minimized_turn_planning(planning)
            run = cast(dict[str, object], planning["run"])
            if run["status"] in {"succeeded", "failed", "outcome_unknown", "cancelled"}:
                return latest
        time.sleep(0.02)
    pytest.fail(f"Planner did not terminalize; latest={latest}")


def _wait_for_planned_task_loop(
    client: TestClient,
    task_id: str,
    *,
    timeout_seconds: float = 5,
) -> dict[str, object]:
    deadline = time.monotonic() + timeout_seconds
    latest: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/v1/tasks/{task_id}/workbench")
        assert response.status_code == 200, response.text
        latest = cast(dict[str, object], response.json())
        raw_loop = latest.get("task_loop")
        if isinstance(raw_loop, dict):
            _assert_minimized_task_loop(raw_loop)
            if raw_loop["status"] in {"planned", "failed"}:
                return latest
        time.sleep(0.02)
    pytest.fail(f"Task Loop did not terminalize; latest={latest}")


def _task_id(body: dict[str, object]) -> str:
    return cast(str, cast(dict[str, object], body["task"])["task_id"])


def _assert_minimized_turn_planning(planning: dict[str, object]) -> None:
    """Prove recursively that the Workbench response contains no authority inputs."""

    def visit(value: object) -> None:
        if isinstance(value, dict):
            leaked = PRIVATE_TURN_PLANNING_KEYS.intersection(value)
            assert not leaked, f"private Turn Planner keys leaked: {sorted(leaked)}"
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(planning)
    assert planning["schema_version"] == "deskpilot.turn-planning-workbench-read.v1"
    run = cast(dict[str, object], planning["run"])
    assert run["schema_version"] == ("deskpilot.turn-planner-run-workbench-summary.v1")
    assert cast(int, run["offer_count"]) > 0


def _assert_minimized_task_loop(task_loop: dict[str, object]) -> None:
    """Prove the public Task Loop contains no user input or authority body."""

    def visit(value: object) -> None:
        if isinstance(value, dict):
            leaked = PRIVATE_TASK_LOOP_KEYS.intersection(value)
            assert not leaked, f"private Task Loop keys leaked: {sorted(leaked)}"
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(task_loop)
    assert task_loop["schema_version"] == "deskpilot.task-loop-workbench.v1"


def test_all_legacy_routes_bypass_model_and_keep_v1_recipe(
    tmp_path: Path,
) -> None:
    provider = PlannerScriptProvider("unsupported")
    with _open_client(tmp_path, provider, name="legacy-routes") as (client, workspace):
        (workspace / "src").mkdir()
        (workspace / "src" / "alpha.py").write_text("answer = 42\n", encoding="utf-8")
        (workspace / "README.md").write_text("legacy read\n", encoding="utf-8")
        (workspace / "replace.md").write_text("before\n", encoding="utf-8")
        (workspace / "first.md").write_text("first old\n", encoding="utf-8")
        (workspace / "second.py").write_text("value = 'before'\n", encoding="utf-8")
        (workspace / "rename-source.md").write_text("rename me\n", encoding="utf-8")
        backend_tests = workspace / "backend" / "tests"
        backend_tests.mkdir(parents=True)
        (workspace / "backend" / "sample.py").write_text("VALUE = 1\n", encoding="utf-8")
        (backend_tests / "test_sample.py").write_text(
            "def test_value():\n    assert True\n",
            encoding="utf-8",
        )
        frontend_tests = workspace / "frontend" / "tests"
        frontend_tests.mkdir(parents=True)
        (frontend_tests / "sample.test.js").write_text(
            "const test = require('node:test')\n",
            encoding="utf-8",
        )

        samples: tuple[tuple[str, RouteId], ...] = (
            ("研究一个公开主题并生成 HTML 报告", "research_to_html"),
            ("查询知识库：immutable proof", "knowledge_lookup"),
            ("统计字符数：DeskPilot", "mcp_text_metrics"),
            ("读取工作区文件：README.md", "workspace_file_read"),
            (
                '在工作区文件 replace.md 中把 "before" 替换为 "after"',
                "workspace_file_replace",
            ),
            (
                '批量修改工作区文件：在工作区文件 first.md 中把 "old" 替换为 "new"；'
                '在工作区文件 second.py 中把 "before" 替换为 "after"',
                "workspace_patch_bundle",
            ),
            (
                '修复并测试工作区：文件："backend/sample.py" Python项目："backend" '
                'Python测试："tests/test_sample.py" 目标：生成最小补丁',
                "workspace_agent_patch_test",
            ),
            (
                '多 Agent 修复并测试工作区：目录："." 文件："backend/sample.py" '
                'Python项目："backend" Python测试："tests/test_sample.py" '
                "目标：生成动态最小补丁",
                "workspace_dynamic_patch_test",
            ),
            ('创建工作区文件 new-file.md 内容："new content"', "workspace_file_create"),
            (
                "将工作区文件 rename-source.md 重命名为 rename-target.md",
                "workspace_file_rename",
            ),
            ("列出工作区目录：src", "workspace_directory_list"),
            ("分析工作区目录：src 文件：src/alpha.py", "workspace_directory_analyze"),
            ("运行工作区检查：python-syntax src", "workspace_snapshot_check"),
            (
                "运行项目测试：backend tests/test_sample.py",
                "workspace_python_test",
            ),
            (
                "运行 Node 测试：frontend tests/sample.test.js",
                "workspace_node_test",
            ),
        )

        for prompt, route_id in samples:
            response = client.post("/api/v1/conversation-turns", json={"message": prompt})
            assert response.status_code == 201, (route_id, response.text)
            body = cast(dict[str, object], response.json())
            route = cast(dict[str, object], body["route"])
            assert route["decision"] == "routed"
            assert route["route_id"] == route_id
            assert route["route_version"] == "1"
            assert route["route_manifest_digest"] == RouteRecipeCatalog.digest(route_id, "1")
            assert route["turn_planning_adjudication_id"] is None
            assert route["turn_plan_binding_id"] is None
            assert route["turn_planning_provenance_digest"] is None
            assert body["turn_planning"] is None
            assert body["task_loop"] is None

    assert provider.total_calls == 0
    assert provider.planner_calls == 0


def test_unmatched_single_step_uses_explicit_bodyless_endpoint_and_binds_v2_route(
    tmp_path: Path,
) -> None:
    provider = PlannerScriptProvider("single")
    source = tmp_path / "knowledge.txt"
    source.write_text("alpha is backed by an immutable local proof.\n", encoding="utf-8")
    with _open_client(tmp_path, provider, name="single") as (client, _workspace):
        imported = client.post("/api/v1/knowledge/sources:import", json={"path": str(source)})
        assert imported.status_code == 200, imported.text
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)

        openapi = client.get("/openapi.json").json()
        operation = openapi["paths"]["/api/v1/tasks/{task_id}/workbench:interpret-turn"]["post"]
        assert "requestBody" not in operation
        interpreted = client.post(f"/api/v1/tasks/{task_id}/workbench:interpret-turn")
        assert interpreted.status_code == 200, interpreted.text
        body = cast(dict[str, object], interpreted.json())

        route = cast(dict[str, object], body["route"])
        planning = cast(dict[str, object], body["turn_planning"])
        _assert_minimized_turn_planning(planning)
        adjudication = cast(dict[str, object], planning["adjudication"])
        binding = cast(dict[str, object], planning["binding"])
        assert body["stage"] == "executing"
        assert route["decision"] == "routed"
        assert route["route_id"] == "knowledge_lookup"
        assert route["route_version"] == "2"
        app = cast(FastAPI, client.app)
        assert client.portal is not None
        internal = cast(
            TurnPlanningRead,
            client.portal.call(app.state.turn_planner_runtime.get, task_id),
        )
        assert internal.adjudication is not None
        selected = internal.adjudication.selected_offers[0]
        selected_offer = next(
            offer for offer in internal.offers if offer.offer_key == selected.offer_key
        )
        assert route["route_manifest_digest"] == selected_offer.trusted_recipe.route_manifest_digest
        assert route["turn_planning_adjudication_id"]
        assert route["turn_plan_binding_id"]
        assert route["turn_planning_provenance_digest"]
        assert adjudication["outcome"] == "single_step"
        assert adjudication["selected_offer_count"] == 1
        assert binding["status"] == "bound"
        assert body["task_loop"] is None
        assert body["planning"] is not None
        assert len(cast(dict[str, list[object]], body["plans"])["plans"]) == 1
        assert len(cast(dict[str, list[object]], body["executions"])["runs"]) == 1

        completed = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        assert completed.status_code == 200, completed.text
        completed_body = cast(dict[str, object], completed.json())
        assert completed_body["stage"] == "delivered"
        assert cast(dict[str, object], completed_body["route"])["status"] == "succeeded"
        assert cast(dict[str, object], completed_body["knowledge"])["citations"]

    assert provider.planner_calls == 1


@pytest.mark.parametrize(
    ("mode", "outcome", "binding_status", "stage"),
    (
        ("needs_input", "needs_user_input", "not_applicable", "needs_clarification"),
        ("unsupported", "unsupported", "not_applicable", "unsupported"),
    ),
)
def test_non_executable_planner_outcomes_remain_proof_only(
    tmp_path: Path,
    mode: PlannerMode,
    outcome: str,
    binding_status: str,
    stage: str,
) -> None:
    provider = PlannerScriptProvider(mode)
    with _open_client(tmp_path, provider, name=f"outcome-{mode}") as (client, _workspace):
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)
        interpreted = client.post(f"/api/v1/tasks/{task_id}/workbench:interpret-turn")
        assert interpreted.status_code == 200, interpreted.text
        body = cast(dict[str, object], interpreted.json())
        planning = cast(dict[str, object], body["turn_planning"])
        _assert_minimized_turn_planning(planning)
        adjudication = cast(dict[str, object], planning["adjudication"])
        binding = cast(dict[str, object], planning["binding"])
        route = cast(dict[str, object], body["route"])

        assert body["stage"] == stage
        assert cast(dict[str, object], planning["run"])["status"] == "succeeded"
        assert adjudication["outcome"] == outcome
        assert binding["status"] == binding_status
        assert route["route_version"] is None
        assert route["turn_planning_adjudication_id"] is None
        assert route["turn_plan_binding_id"] is None
        assert route["turn_planning_provenance_digest"] is None
        assert body["planning"] is None
        assert cast(dict[str, list[object]], body["plans"])["plans"] == []
        assert cast(dict[str, list[object]], body["executions"])["runs"] == []

    assert provider.planner_calls == 1


def test_multi_step_deferred_becomes_sanitized_planned_task_loop_without_model_replay(
    tmp_path: Path,
) -> None:
    provider = PlannerScriptProvider("multi")
    with _open_client(tmp_path, provider, name="multi-task-loop") as (client, _workspace):
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)
        interpreted = client.post(f"/api/v1/tasks/{task_id}/workbench:interpret-turn")
        assert interpreted.status_code == 200, interpreted.text
        deferred = cast(dict[str, object], interpreted.json())
        planning = cast(dict[str, object], deferred["turn_planning"])
        adjudication = cast(dict[str, object], planning["adjudication"])
        actions = cast(list[dict[str, object]], deferred["actions"])
        assert adjudication["outcome"] == "multi_step_deferred"
        assert deferred["task_loop"] is None
        assert any(
            item["action"] == "plan_task_loop" and item["enabled"] is True for item in actions
        )

        advanced = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        assert advanced.status_code == 200, advanced.text
        body = cast(dict[str, object], advanced.json())
        task_loop = cast(dict[str, object], body["task_loop"])
        _assert_minimized_task_loop(task_loop)
        assert body["stage"] == "planned"
        assert task_loop["phase"] == "plan"
        assert task_loop["status"] == "planned"
        assert task_loop["revision"] == 2
        assert task_loop["event_count"] == 2
        assert task_loop["step_count"] == 2
        assert task_loop["recoverable"] is False
        assert body["planning"] is None
        assert cast(dict[str, list[object]], body["plans"])["plans"] == []
        assert cast(dict[str, list[object]], body["executions"])["runs"] == []
        route = cast(dict[str, object], body["route"])
        assert route["route_version"] is None
        assert route["turn_planning_adjudication_id"] is None
        assert route["turn_plan_binding_id"] is None
        assert route["turn_planning_provenance_digest"] is None

        stable = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
        assert stable.status_code == 200, stable.text
        stable_body = cast(dict[str, object], stable.json())
        assert stable_body["projection_digest"] == body["projection_digest"]
        assert stable_body["task_loop"] == task_loop

    assert provider.planner_calls == 1
    assert provider.total_calls == 1


def test_restart_recovers_deferred_task_loop_without_replaying_planner(
    tmp_path: Path,
) -> None:
    first_provider = PlannerScriptProvider("multi")
    name = "multi-task-loop-restart"
    with _open_client(tmp_path, first_provider, name=name) as (client, _workspace):
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)
        interpreted = client.post(f"/api/v1/tasks/{task_id}/workbench:interpret-turn")
        assert interpreted.status_code == 200, interpreted.text
        assert interpreted.json()["task_loop"] is None

    assert first_provider.planner_calls == 1
    second_provider = PlannerScriptProvider("unsupported")
    with _open_client(
        tmp_path,
        second_provider,
        name=name,
        automatic=True,
    ) as (client, _workspace):
        recovered = _wait_for_planned_task_loop(client, task_id)
        task_loop = cast(dict[str, object], recovered["task_loop"])
        assert task_loop["status"] == "planned"
        assert task_loop["step_count"] == 2
        assert cast(dict[str, list[object]], recovered["executions"])["runs"] == []

    assert second_provider.planner_calls == 0
    # Startup may resume unrelated legacy TaskProcessor model work for the
    # persisted Task.  The stage-112 guarantee is narrower and explicit: the
    # terminal Turn Planner reservation is never dispatched again.


@pytest.mark.parametrize(
    ("mode", "failure_code"),
    (
        ("timeout", "PLANNER_TIMEOUT"),
        ("schema", "PLANNER_SCHEMA_REJECTED"),
        ("unknown_offer", "PLANNER_UNKNOWN_OFFER"),
        ("provider_failure", "PLANNER_PROVIDER_UNAVAILABLE"),
    ),
)
def test_automatic_planner_failure_is_persisted_and_never_replayed(
    tmp_path: Path,
    mode: PlannerMode,
    failure_code: str,
) -> None:
    provider = PlannerScriptProvider(mode)
    with _open_client(
        tmp_path,
        provider,
        name=f"failure-{mode}",
        automatic=True,
    ) as (client, _workspace):
        created_response = client.post(
            "/api/v1/conversation-turns",
            json={"message": "请为 alpha 做一次受限资料匹配"},
        )
        assert created_response.status_code == 201, created_response.text
        created = cast(dict[str, object], created_response.json())
        task_id = _task_id(created)
        terminal = _wait_for_terminal_planner(client, task_id)
        planning = cast(dict[str, object], terminal["turn_planning"])
        _assert_minimized_turn_planning(planning)
        run = cast(dict[str, object], planning["run"])
        failure = cast(dict[str, object], run["failure"])
        adjudication = cast(dict[str, object], planning["adjudication"])
        binding = cast(dict[str, object], planning["binding"])

        assert run["status"] == "failed"
        assert failure["error_code"] == failure_code
        assert failure["retry_policy"] == "never_automatic"
        assert adjudication["outcome"] == "deterministic_fallback"
        assert binding["status"] == "not_applicable"
        assert cast(dict[str, object], terminal["route"])["turn_planning_provenance_digest"] is None
        run_digest = run["run_digest"]
        planning_digest = planning["planning_digest"]
        app = cast(FastAPI, client.app)

        time.sleep(0.15)
        for _ in range(2):
            replay = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            assert replay.status_code == 200, replay.text
            replay_planning = cast(dict[str, object], replay.json()["turn_planning"])
            assert cast(dict[str, object], replay_planning["run"])["run_digest"] == run_digest
            assert replay_planning["planning_digest"] == planning_digest

        async def count_runs() -> int:
            async with app.state.database.session() as session:
                return int(
                    await session.scalar(
                        select(func.count())
                        .select_from(TurnPlannerRunRecord)
                        .where(TurnPlannerRunRecord.task_id == task_id)
                    )
                    or 0
                )

        assert client.portal is not None
        assert client.portal.call(count_runs) == 1
        assert provider.planner_calls == 1


def test_periodic_recovery_terminalizes_a_lease_that_expires_after_startup(
    tmp_path: Path,
) -> None:
    provider = PlannerScriptProvider("single")
    with _open_client(
        tmp_path,
        provider,
        name="expired-after-startup",
        automatic=False,
    ) as (client, _workspace):
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)
        app = cast(FastAPI, client.app)
        assert client.portal is not None

        async def exercise_recovery() -> TurnPlanningRead:
            runtime = cast(TurnPlannerRuntime, app.state.turn_planner_runtime)
            clock = {"now": datetime.now(UTC) + timedelta(seconds=1)}
            runtime._clock = lambda: clock["now"]
            await runtime._claim_for_dispatch(task_id)

            coordinator = WorkbenchRuntimeCoordinator(
                app.state.database,
                app.state.task_workbench_service,
                instance_id="phase-111-expired-planner-recovery",
                poll_interval_seconds=0.005,
                claim_ttl_seconds=5,
                concurrency=1,
                max_failures=2,
                retry_base_seconds=0.01,
                retry_max_seconds=0.02,
                planner_recovery_scan_interval_seconds=0.02,
                planner_recovery_scan_limit=3,
            )
            coordinator.start()
            try:
                # Startup observes an unexpired dispatch lease, so this state
                # must remain untouched until a later periodic recovery scan.
                await asyncio.sleep(0.06)
                before_expiry = await runtime.get(task_id)
                assert before_expiry is not None
                assert before_expiry.run.status == "dispatching"
                assert provider.planner_calls == 0

                clock["now"] += timedelta(seconds=91)
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    recovered = await runtime.get(task_id)
                    if recovered is not None and recovered.run.status == "outcome_unknown":
                        break
                    await asyncio.sleep(0.01)
                else:
                    pytest.fail("Expired Planner lease was not recovered periodically")

                assert recovered is not None
                assert recovered.run.failure is not None
                assert recovered.run.failure.error_code == "PLANNER_OUTCOME_UNKNOWN"
                assert recovered.run.failure.retry_policy == "never_automatic"
                assert recovered.adjudication is not None
                assert recovered.adjudication.outcome == "deterministic_fallback"
                assert recovered.binding is not None
                assert recovered.binding.status == "not_applicable"
                assert task_id not in await runtime.recoverable_task_ids()

                stable_digest = recovered.planning_digest
                await asyncio.sleep(0.06)
                stable = await runtime.get(task_id)
                assert stable is not None
                assert stable.planning_digest == stable_digest
                assert provider.planner_calls == 0
                return stable
            finally:
                await coordinator.shutdown()

        recovered = client.portal.call(exercise_recovery)
        assert recovered.run.status == "outcome_unknown"

    assert provider.total_calls == 0
    assert provider.planner_calls == 0


def test_invalid_model_patch_preview_terminalizes_once_and_disables_start(
    tmp_path: Path,
) -> None:
    provider = PlannerScriptProvider("invalid_patch")
    with _open_client(
        tmp_path,
        provider,
        name="invalid-model-patch",
        automatic=True,
    ) as (client, _workspace):
        created_response = client.post(
            "/api/v1/conversation-turns",
            json={"message": "请把 not-json 作为受限补丁参数处理"},
        )
        assert created_response.status_code == 201, created_response.text
        created = cast(dict[str, object], created_response.json())
        task_id = _task_id(created)

        deadline = time.monotonic() + 5
        terminal: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/api/v1/tasks/{task_id}/workbench")
            assert response.status_code == 200, response.text
            terminal = cast(dict[str, object], response.json())
            route = cast(dict[str, object], terminal["route"])
            conversation = cast(list[dict[str, object]], terminal["conversation"])
            if route["status"] == "failed" and any(
                message["role"] == "assistant"
                and "本地提案选择的工作区预览被拒绝" in cast(str, message["content"])
                for message in conversation
            ):
                break
            time.sleep(0.02)
        else:
            pytest.fail(f"Invalid Patch preview did not terminalize; latest={terminal}")

        planning = cast(dict[str, object], terminal["turn_planning"])
        _assert_minimized_turn_planning(planning)
        run = cast(dict[str, object], planning["run"])
        route = cast(dict[str, object], terminal["route"])
        actions = cast(list[dict[str, object]], terminal["actions"])
        messages = cast(list[object], terminal["conversation"])
        executions = cast(dict[str, list[object]], terminal["executions"])

        assert run["status"] == "succeeded"
        assert route["status"] == "failed"
        assert route["error_code"] == "TURN_ROUTE_PROOF_REJECTED"
        assert executions["runs"] == []
        assert not any(
            action["action"] == "start_execution" and action["enabled"] for action in actions
        )
        stable_route_revision = route["revision"]
        stable_message_count = len(messages)
        stable_projection_digest = terminal["projection_digest"]

        time.sleep(0.15)
        for _ in range(2):
            replay = client.post(f"/api/v1/tasks/{task_id}/workbench:advance")
            assert replay.status_code == 200, replay.text
            replay_body = cast(dict[str, object], replay.json())
            replay_route = cast(dict[str, object], replay_body["route"])
            replay_messages = cast(list[object], replay_body["conversation"])
            replay_executions = cast(dict[str, list[object]], replay_body["executions"])
            replay_actions = cast(list[dict[str, object]], replay_body["actions"])

            assert replay_route["status"] == "failed"
            assert replay_route["error_code"] == "TURN_ROUTE_PROOF_REJECTED"
            assert replay_route["revision"] == stable_route_revision
            assert len(replay_messages) == stable_message_count
            assert replay_executions["runs"] == []
            assert replay_body["projection_digest"] == stable_projection_digest
            assert not any(
                action["action"] == "start_execution" and action["enabled"]
                for action in replay_actions
            )

        assert provider.planner_calls == 1


@pytest.mark.parametrize("replacement", [False, True], ids=["stop", "continue"])
def test_stop_or_continue_fences_late_planner_result(
    tmp_path: Path,
    replacement: bool,
) -> None:
    provider = PlannerScriptProvider("blocking_single")
    with _open_client(tmp_path, provider, name=f"fence-{replacement}") as (
        client,
        _workspace,
    ):
        created = _create_unmatched_turn(client)
        task_id = _task_id(created)
        app = cast(FastAPI, client.app)
        service = cast(TaskWorkbenchService, app.state.task_workbench_service)

        async def race() -> tuple[TaskWorkbenchRead, TaskWorkbenchRead, TaskWorkbenchRead]:
            interpreting = asyncio.create_task(service.interpret_turn(task_id))
            await asyncio.wait_for(provider.started.wait(), timeout=2)
            if replacement:
                action_result = await service.continue_turn(
                    task_id,
                    ContinueConversationTurn(message="查询知识库：alpha"),
                )
            else:
                action_result = await service.stop(task_id)
            provider.release.set()
            late_result = await asyncio.wait_for(interpreting, timeout=5)
            source_after = await service.get(task_id)
            return action_result, late_result, source_after

        assert client.portal is not None
        action_result, late_result, source_after = client.portal.call(race)
        source_planning = source_after.turn_planning
        assert source_planning is not None
        assert source_planning.run.status == "cancelled"
        assert source_planning.run.failure is not None
        assert source_planning.run.failure.error_code == "PLANNER_CANCELLED"
        assert source_planning.run.revision >= 3
        assert source_planning.adjudication is not None
        assert source_planning.adjudication.outcome == "deterministic_fallback"
        assert source_planning.binding is not None
        assert source_planning.binding.status == "not_applicable"
        assert source_after.route is not None
        assert source_after.route.turn_planning_provenance_digest is None
        assert not source_after.plans.plans
        assert not source_after.executions.runs
        assert late_result.route is not None
        assert late_result.route.turn_planning_provenance_digest is None

        if replacement:
            assert action_result.task.task_id != task_id
            assert action_result.route is not None
            assert action_result.route.route_id == "knowledge_lookup"
            assert action_result.route.route_version == "1"
            assert action_result.turn_planning is None
        else:
            assert action_result.task.task_id == task_id
            assert action_result.turn_planning is not None
            assert action_result.turn_planning.run.status == "cancelled"

    assert provider.planner_calls == 1
