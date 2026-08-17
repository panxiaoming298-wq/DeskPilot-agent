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
    research_to_html_contract,
    research_to_html_draft,
)
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
        ("browser.verify.v1", "1.1.0"),
    }
    assert len(first.acceptance_coverage) == len(contract.acceptance_criteria)
    compiler.validate_manifest(first)


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


def test_capability_api_is_read_only_and_only_local_phase_71_runtimes_are_enabled(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/capabilities")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert {item["capability_id"] for item in response.json()["capabilities"]} == {
        "research.read.v1",
        "artifact.html.v1",
        "browser.verify.v1",
    }
    assert {
        (item["capability_id"], item["version"])
        for item in response.json()["capabilities"]
        if item["runtime_enabled"]
    } == {
        ("artifact.html.v1", "1.1.0"),
        ("browser.verify.v1", "1.1.0"),
    }
