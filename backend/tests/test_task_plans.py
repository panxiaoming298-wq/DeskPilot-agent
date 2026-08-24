from typing import Any, cast

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import ValidationError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import (
    CapabilityCatalog,
    create_builtin_capability_catalog,
)
from deskpilot.application.plan_compiler import (
    PlanAcceptanceUncoveredError,
    PlanBindingUnknownError,
    PlanBudgetExceededError,
    PlanCapabilityMismatchError,
    PlanCompiler,
    PlanPrivacyConflictError,
    knowledge_lookup_contract,
    knowledge_lookup_draft,
    mcp_text_metrics_contract,
    mcp_text_metrics_draft,
    research_to_html_contract,
    research_to_html_draft,
    workspace_agent_patch_test_contract,
    workspace_agent_patch_test_draft,
    workspace_directory_list_contract,
    workspace_directory_list_draft,
    workspace_dynamic_patch_test_contract,
    workspace_dynamic_patch_test_draft,
    workspace_file_read_contract,
    workspace_file_read_draft,
    workspace_node_test_contract,
    workspace_node_test_draft,
    workspace_python_test_contract,
    workspace_python_test_draft,
    workspace_snapshot_check_contract,
    workspace_snapshot_check_draft,
)
from deskpilot.domain.agent_replanning import classify_agent_replan_continuation
from deskpilot.domain.task_plans import DraftPlan, TurnInterpretation
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.infrastructure.models import TaskPlanGenerationRecord
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry


def _compiler() -> tuple[PlanCompiler, CapabilityCatalog]:
    tools = create_builtin_registry()
    agents = create_builtin_agent_registry(
        tools,
        (FakeModelProvider().descriptor,),
    )
    capabilities = create_builtin_capability_catalog()
    return PlanCompiler(agents, tools, capabilities), capabilities


@pytest.mark.parametrize(
    ("message", "accepted"),
    [
        ("继续修复", True),
        (" 重新规划并继续修复！ ", True),
        ("continue repair", True),
        ("继续", False),
        ("再试一次", False),
        ("修复", False),
    ],
)
def test_patch_replan_continuation_requires_explicit_intent(
    message: str,
    accepted: bool,
) -> None:
    assert (classify_agent_replan_continuation(message) is not None) is accepted


def _replace_node(draft: DraftPlan, key: str, **updates: Any) -> DraftPlan:
    nodes = tuple(
        node.model_copy(update=updates) if node.local_key == key else node for node in draft.nodes
    )
    return draft.model_copy(update={"nodes": nodes})


def test_research_to_html_fixture_is_sealed_deterministic_and_not_runnable() -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'1' * 32}"
    contract = research_to_html_contract(task_id, capabilities)
    draft = research_to_html_draft(task_id)

    first = compiler.compile(contract, draft, generation=1)
    repeated = compiler.compile(contract, draft, generation=1)

    assert first == repeated
    assert first.plan_manifest_digest == repeated.plan_manifest_digest
    assert first.runtime_enabled is False
    assert len(first.nodes) == 5
    assert {node.local_key for node in first.nodes} == {
        "research",
        "build_html",
        "browser_verify",
        "final_acceptance",
        "delivery",
    }
    enabled = {
        (pack.capability_id, pack.version)
        for pack in capabilities.list_public()
        if pack.runtime_enabled
    }
    assert enabled == {
        ("artifact.html.v1", "1.1.0"),
        ("artifact.html.v1", "1.2.0"),
        ("browser.verify.v1", "1.1.0"),
        ("knowledge.local.v1", "1.0.0"),
        ("mcp.text.metrics.v1", "1.0.0"),
        ("workspace.file.read.v1", "1.0.0"),
        ("workspace.file.replace.v1", "1.0.0"),
        ("workspace.file.create.v1", "1.0.0"),
        ("workspace.file.rename.v1", "1.0.0"),
        ("workspace.patch.bundle.v1", "1.0.0"),
        ("workspace.patch.propose.v1", "1.0.0"),
        ("workspace.directory.read.v1", "1.0.0"),
        ("workspace.snapshot.check.v1", "1.0.0"),
        ("workspace.python.test.v1", "1.0.0"),
        ("workspace.node.test.v1", "1.0.0"),
        ("workspace.project.search.v1", "1.0.0"),
        ("workspace.project.read_many.v1", "1.0.0"),
        ("workspace.git.inspect.v1", "1.0.0"),
    }
    assert len(first.acceptance_coverage) == len(contract.acceptance_criteria)
    compiler.validate_manifest(first)


@pytest.mark.parametrize(
    ("contract_factory", "draft_factory", "node_key"),
    (
        (knowledge_lookup_contract, knowledge_lookup_draft, "knowledge_lookup"),
        (mcp_text_metrics_contract, mcp_text_metrics_draft, "mcp_text_metrics"),
        (
            workspace_directory_list_contract,
            workspace_directory_list_draft,
            "workspace_directory_list",
        ),
        (
            workspace_snapshot_check_contract,
            workspace_snapshot_check_draft,
            "workspace_snapshot_check",
        ),
        (
            workspace_python_test_contract,
            workspace_python_test_draft,
            "workspace_python_test",
        ),
        (
            workspace_node_test_contract,
            workspace_node_test_draft,
            "workspace_node_test",
        ),
    ),
)
def test_phase_78_direct_route_plans_are_sealed_and_runtime_enabled(
    contract_factory: Any,
    draft_factory: Any,
    node_key: str,
) -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'8' * 32}"
    contract = contract_factory(task_id, capabilities)
    plan = compiler.compile(contract, draft_factory(task_id), generation=1)

    assert plan.runtime_enabled is True
    expected_nodes = {
        node_key,
        "final_acceptance",
        "delivery",
    }
    assert {node.local_key for node in plan.nodes} == expected_nodes
    compiler.validate_manifest(plan)


@pytest.mark.parametrize(
    ("contract_factory", "draft_factory", "node_key", "capability_id"),
    (
        (
            workspace_file_read_contract,
            workspace_file_read_draft,
            "workspace_file_read",
            "workspace.file.read.v1",
        ),
        (
            workspace_directory_list_contract,
            workspace_directory_list_draft,
            "workspace_directory_list",
            "workspace.directory.read.v1",
        ),
    ),
)
def test_workspace_read_plans_bind_versioned_agent_loop(
    contract_factory: Any,
    draft_factory: Any,
    node_key: str,
    capability_id: str,
) -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'9' * 32}"
    plan = compiler.compile(
        contract_factory(task_id, capabilities), draft_factory(task_id), generation=1
    )
    node = next(item for item in plan.nodes if item.local_key == node_key)

    assert node.bound_agent is not None
    expected_agent = (
        "builtin.workspace_coordinator"
        if node_key == "workspace_directory_list"
        else "builtin.workspace_reader"
    )
    assert node.bound_agent.agent_id == expected_agent
    assert node.bound_agent.version == (
        "1.1.0" if node_key == "workspace_directory_list" else "1.2.0"
    )
    if node_key == "workspace_directory_list":
        assert all(item.handoff_parent_node_id is None for item in plan.nodes)
        assert node.capability is None
    else:
        assert node.capability is not None
        assert node.capability.capability_id == capability_id
    assert node.budget.model_calls == 2
    assert node.budget.tool_calls == (
        0 if node_key == "workspace_directory_list" else 1
    )
    assert node.budget.handoffs == (
        4 if node_key == "workspace_directory_list" else 0
    )


def test_workspace_agent_patch_plan_has_proposal_only_agent_and_fixed_test_contract() -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'a' * 32}"
    contract = workspace_agent_patch_test_contract(
        task_id,
        capabilities,
        test_kind="python",
    )
    plan = compiler.compile(
        contract,
        workspace_agent_patch_test_draft(task_id),
        generation=1,
    )
    node = next(item for item in plan.nodes if item.local_key == "workspace_agent_patch_test")

    assert plan.runtime_enabled is True
    assert node.bound_agent is not None
    assert node.bound_agent.agent_id == "builtin.workspace_patch_planner"
    assert node.bound_agent.version == "1.0.0"
    assert node.capability is not None
    assert node.capability.capability_id == "workspace.patch.propose.v1"
    assert {item.capability_id for item in contract.capabilities} == {
        "workspace.file.read.v1",
        "workspace.patch.propose.v1",
        "workspace.patch.bundle.v1",
        "workspace.python.test.v1",
    }
    assert "explicit_user_patch_confirmation_v1" in contract.constraints
    compiler.validate_manifest(plan)


def test_dynamic_patch_plan_delegates_only_inside_server_adjudicated_graph() -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'b' * 32}"
    contract = workspace_dynamic_patch_test_contract(
        task_id,
        capabilities,
        test_kind="python",
    )
    plan = compiler.compile(
        contract,
        workspace_dynamic_patch_test_draft(task_id),
        generation=1,
    )
    parent = next(item for item in plan.nodes if item.local_key == "workspace_dynamic_patch_test")

    assert parent.bound_agent is not None
    assert parent.bound_agent.agent_id == "builtin.workspace_coordinator"
    assert parent.bound_agent.version == "1.1.0"
    assert parent.capability is None
    assert contract.max_risk_level is ToolRiskLevel.R1
    assert {item.capability_id for item in contract.capabilities} == {
        "workspace.directory.read.v1",
        "workspace.file.read.v1",
        "workspace.patch.propose.v1",
        "workspace.patch.bundle.v1",
        "workspace.python.test.v1",
    }
    assert "dynamic_patch_approval_node_v1" in contract.constraints
    assert "fresh_confirmation_per_patch_node_v1" in contract.constraints
    assert "composable_patch_approval_nodes_v1" in contract.constraints
    assert "distinct_server_bound_patch_input_per_node_v1" in contract.constraints
    assert "server_adjudicated_test_conditions_v1" in contract.constraints
    assert "no_automatic_replan_after_workspace_write_v1" in contract.constraints
    assert "maximum_three_patch_plan_generations_v1" in contract.constraints
    assert "cross_generation_task_budget_v1" in contract.constraints
    assert "fresh_confirmation_after_replan_v1" in contract.constraints
    assert contract.budget.max_model_calls == 30
    assert contract.budget.max_tool_calls == 12
    assert contract.budget.max_cost_micros == 1_500_000
    assert contract.budget.max_handoffs == 12
    compiler.validate_manifest(plan)


def test_draft_is_untrusted_and_compiler_rejects_invalid_authority_and_proofs() -> None:
    compiler, capabilities = _compiler()
    task_id = f"tsk_{'2' * 32}"
    contract = research_to_html_contract(task_id, capabilities)
    draft = research_to_html_draft(task_id)

    forged = draft.model_dump(mode="json")
    forged["approved"] = True
    with pytest.raises(ValidationError):
        DraftPlan.model_validate(forged)

    without_citations = _replace_node(draft, "research", acceptance_refs=())
    with pytest.raises(PlanAcceptanceUncoveredError):
        compiler.compile(contract, without_citations, generation=1)

    oversized = _replace_node(
        draft,
        "research",
        budget=next(node for node in draft.nodes if node.local_key == "research").budget.model_copy(
            update={"wall_seconds": 901}
        ),
    )
    with pytest.raises(PlanBudgetExceededError):
        compiler.compile(contract, oversized, generation=1)

    private = contract.model_copy(
        update={
            "privacy_policy": contract.privacy_policy.model_copy(
                update={"external_egress_allowed": False}
            )
        }
    )
    with pytest.raises(PlanPrivacyConflictError):
        compiler.compile(private, draft, generation=1)

    low_risk = contract.model_copy(update={"max_risk_level": ToolRiskLevel.R0})
    with pytest.raises(PlanCapabilityMismatchError):
        compiler.compile(low_risk, draft, generation=1)

    unknown_reference = contract.capabilities[0].model_copy(
        update={"capability_id": "unknown.read.v1"}
    )
    unknown_contract = contract.model_copy(
        update={"capabilities": (unknown_reference, *contract.capabilities[1:])}
    )
    with pytest.raises(PlanBindingUnknownError):
        compiler.compile(unknown_contract, draft, generation=1)


def test_turn_interpretation_is_typed_and_cannot_carry_approval() -> None:
    value: dict[str, object] = {
        "turn_id": f"trn_{'1' * 32}",
        "conversation_id": f"cnv_{'2' * 32}",
        "message_id": f"msg_{'3' * 32}",
        "kind": "typed_command",
        "summary": "继续当前任务",
    }
    with pytest.raises(ValidationError):
        TurnInterpretation.model_validate(value)
    value["task_id"] = f"tsk_{'4' * 32}"
    value["approved"] = True
    with pytest.raises(ValidationError):
        TurnInterpretation.model_validate(value)


def test_planning_persistence_version_chain_read_api_and_tamper_rejection(
    client: TestClient,
) -> None:
    created = client.post(
        "/api/v1/tasks",
        json={
            "goal": "研究公开主题并生成 HTML",
            "privacy_mode": "balanced",
            "constraints": ["read_only_research"],
        },
    )
    assert created.status_code == 201
    task_id = created.json()["task_id"]
    app = cast(FastAPI, client.app)
    assert client.portal is not None
    service = app.state.plan_compilation_service
    capabilities = app.state.capability_catalog
    contract_v1 = research_to_html_contract(task_id, capabilities)
    draft_v1 = research_to_html_draft(task_id)

    first = client.portal.call(service.activate, contract_v1, draft_v1)
    second = client.portal.call(service.activate, contract_v1, draft_v1)
    contract_v2 = contract_v1.model_copy(
        update={
            "version": 2,
            "previous_contract_digest": contract_v1.digest,
            "constraints": (*contract_v1.constraints, "retain_source_titles"),
        }
    )
    draft_v2 = draft_v1.model_copy(update={"contract_version": 2})
    third = client.portal.call(service.activate, contract_v2, draft_v2)

    assert first.plan.plan_generation == 1
    assert second.plan.plan_generation == 2
    assert third.plan.plan_generation == 3
    state = client.get(f"/api/v1/tasks/{task_id}/planning")
    contracts = client.get(f"/api/v1/tasks/{task_id}/contracts")
    plans = client.get(f"/api/v1/tasks/{task_id}/plans")
    assert state.status_code == contracts.status_code == plans.status_code == 200
    assert state.headers["cache-control"] == "no-store"
    assert state.json()["active_contract_version"] == 2
    assert state.json()["active_plan_generation"] == 3
    assert [item["active"] for item in contracts.json()["contracts"]] == [False, True]
    assert [item["status"] for item in plans.json()["plans"]] == [
        "superseded",
        "superseded",
        "active",
    ]
    assert client.post("/api/v1/capabilities").status_code == 405

    async def tamper() -> None:
        database = app.state.database
        async with database.session() as session, session.begin():
            record = await session.get(TaskPlanGenerationRecord, (task_id, 3))
            assert record is not None
            manifest = dict(record.manifest)
            manifest["runtime_enabled"] = True
            record.manifest = manifest

    client.portal.call(tamper)
    rejected = client.get(f"/api/v1/tasks/{task_id}/plans/3")
    assert rejected.status_code == 409
    assert rejected.json()["code"] == "PLANNING_PROOF_REJECTED"


def test_capability_api_is_read_only_and_only_registered_runtimes_are_enabled(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {item["capability_id"] for item in response.json()["capabilities"]} == {
        "research.read.v1",
        "artifact.html.v1",
        "browser.verify.v1",
        "knowledge.local.v1",
        "mcp.text.metrics.v1",
        "workspace.file.read.v1",
        "workspace.file.replace.v1",
        "workspace.file.create.v1",
        "workspace.file.rename.v1",
        "workspace.patch.bundle.v1",
        "workspace.patch.propose.v1",
        "workspace.directory.read.v1",
        "workspace.snapshot.check.v1",
        "workspace.python.test.v1",
        "workspace.node.test.v1",
        "workspace.project.search.v1",
        "workspace.project.read_many.v1",
        "workspace.git.inspect.v1",
    }
    assert {
        (item["capability_id"], item["version"])
        for item in response.json()["capabilities"]
        if item["runtime_enabled"]
        } == {
            ("artifact.html.v1", "1.1.0"),
            ("artifact.html.v1", "1.2.0"),
            ("browser.verify.v1", "1.1.0"),
        ("knowledge.local.v1", "1.0.0"),
        ("mcp.text.metrics.v1", "1.0.0"),
        ("workspace.file.read.v1", "1.0.0"),
        ("workspace.file.replace.v1", "1.0.0"),
        ("workspace.file.create.v1", "1.0.0"),
        ("workspace.file.rename.v1", "1.0.0"),
        ("workspace.patch.bundle.v1", "1.0.0"),
        ("workspace.patch.propose.v1", "1.0.0"),
        ("workspace.directory.read.v1", "1.0.0"),
        ("workspace.snapshot.check.v1", "1.0.0"),
        ("workspace.python.test.v1", "1.0.0"),
        ("workspace.node.test.v1", "1.0.0"),
        ("workspace.project.search.v1", "1.0.0"),
        ("workspace.project.read_many.v1", "1.0.0"),
        ("workspace.git.inspect.v1", "1.0.0"),
    }
