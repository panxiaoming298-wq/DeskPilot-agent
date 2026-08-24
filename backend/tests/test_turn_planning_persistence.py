from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.plan_compiler import (
    PlanCompiler,
    workspace_agent_patch_test_contract,
    workspace_agent_patch_test_draft,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.task_plans import ExecutablePlan, PlanNodeBudget, TaskContract
from deskpilot.domain.turn_planning import (
    TurnPlanBinding,
    TurnPlannerAdjudication,
    TurnPlannerDecision,
    TurnPlannerFailureProof,
    TurnPlannerInput,
    TurnPlannerRun,
    TurnPlanningOffer,
    TurnPlanningParameterBinding,
    TurnPlanningParameterSpec,
    TurnPlanningPlanRef,
    TurnPlanningRead,
    TurnPlanningRecipeRef,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    ConversationRecord,
    TaskRecord,
    TurnPlanBindingRecord,
    TurnPlannerAdjudicationRecord,
    TurnPlannerRunRecord,
    TurnPlanningOfferRecord,
    TurnRouteRecord,
)
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, tzinfo=UTC)
TASK_ID = "tsk_" + "1" * 32
MESSAGE_ID = "msg_" + "2" * 32
CONVERSATION_ID = "conv_" + "3" * 32
MESSAGE_DIGEST = "4" * 64
OFFER_KEY = "ofk_" + "5" * 64


def _provider() -> ModelProviderDescriptor:
    return ModelProviderDescriptor(
        provider_id="local_test",
        display_name="Local test provider",
        model="test-model",
        protocol=ModelProtocol.FAKE,
        location=ModelLocation.LOCAL,
        capabilities=ModelCapabilities(
            structured_output=True,
            strict_json_schema=True,
        ),
    )


@cache
def _compiled_offer_plan(task_id: str) -> tuple[TaskContract, ExecutablePlan]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (_provider(),))
    compiler = PlanCompiler(agents, tools, capabilities)
    contract = workspace_agent_patch_test_contract(
        task_id,
        capabilities,
        test_kind="python",
    )
    return (
        contract,
        compiler.compile(
            contract,
            workspace_agent_patch_test_draft(task_id),
            generation=1,
        ),
    )


def _offer(
    *,
    offer_key: str = OFFER_KEY,
    task_id: str = TASK_ID,
    message_id: str = MESSAGE_ID,
    message_digest: str = MESSAGE_DIGEST,
    route_id: str = "research",
    expected_plan_override: ExecutablePlan | None = None,
) -> TurnPlanningOffer:
    contract, compiled_plan = _compiled_offer_plan(task_id)
    expected_plan = expected_plan_override or compiled_plan
    return TurnPlanningOffer.build(
        offer_key=offer_key,
        task_id=task_id,
        user_message_id=message_id,
        user_message_digest=message_digest,
        intent_description="Research the exact topic supplied by the user.",
        task_contract=expected_plan.task_contract,
        expected_plan=expected_plan,
        capabilities=contract.capabilities,
        provider=_provider(),
        trusted_recipe=TurnPlanningRecipeRef(
            route_id=route_id,
            route_version="2",
            route_manifest_digest="b" * 64,
        ),
        budget=PlanNodeBudget(
            model_calls=1,
            tool_calls=2,
            input_tokens=2_000,
            output_tokens=1_000,
            wall_seconds=60,
            retries=0,
            cost_micros=0,
            handoffs=0,
        ),
        parameter_specs=(
            TurnPlanningParameterSpec(
                parameter_name="query",
                required=True,
                min_length=1,
                max_length=100,
            ),
        ),
        policy_snapshot_digest="c" * 64,
        created_at=NOW,
    )


def _planner_agent() -> BoundAgentRef:
    return BoundAgentRef(
        agent_id="builtin.turn_planner",
        version="1.0.0",
        contract_digest="d" * 64,
        prompt_package_digest="e" * 64,
    )


def _plan_with_registry_drift(plan: ExecutablePlan) -> ExecutablePlan:
    material = plan.model_dump(mode="json", exclude={"plan_manifest_digest"})
    nodes = list(material["nodes"])
    changed = False
    for index, node_value in enumerate(nodes):
        node = dict(node_value)
        if node["bound_agent"] is None:
            continue
        agent = dict(node["bound_agent"])
        agent["prompt_package_digest"] = "0" * 64
        node["bound_agent"] = agent
        node_material = {
            key: value for key, value in node.items() if key != "node_spec_digest"
        }
        node["node_spec_digest"] = sha256_digest(node_material)
        nodes[index] = node
        changed = True
        break
    assert changed
    material["nodes"] = nodes
    used_agents = sorted(
        (node["bound_agent"] for node in nodes if node["bound_agent"] is not None),
        key=lambda item: (str(item["agent_id"]), str(item["version"])),
    )
    used_capabilities = sorted(
        (node["capability"] for node in nodes if node["capability"] is not None),
        key=lambda item: (str(item["capability_id"]), str(item["version"])),
    )
    material["binding_snapshot_digest"] = sha256_digest(
        {"agents": used_agents, "capabilities": used_capabilities}
    )
    return ExecutablePlan.model_validate(
        {**material, "plan_manifest_digest": sha256_digest(material)}
    )


def _reserve(offers: tuple[TurnPlanningOffer, ...]) -> TurnPlannerRun:
    return TurnPlannerRun.reserve(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        planner_agent=_planner_agent(),
        provider=_provider(),
        offers=tuple(item.ref for item in offers),
        request_digest="f" * 64,
        fallback_candidate_digest="0" * 64,
        created_at=NOW,
    )


def _dispatch(run: TurnPlannerRun) -> TurnPlannerRun:
    return run.evolve(
        status="dispatching",
        revision=2,
        updated_at=NOW + timedelta(seconds=1),
        claim_owner_id="worker-stage-111",
        claim_fencing_token=1,
        claim_expires_at=NOW + timedelta(seconds=31),
        request_dispatched_at=NOW + timedelta(seconds=1),
    )


def _single_step_response(offer: TurnPlanningOffer) -> dict[str, object]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": offer.offer_key,
                "parameters": [{"name": "query", "value": "cats"}],
            }
        ],
    }


def test_turn_planning_domain_rejects_tampering_and_exposes_least_authority_input() -> None:
    offer = _offer()
    planner_input = TurnPlannerInput.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        user_message="please research cats",
        offers=(offer,),
    )

    visible_offer = planner_input.offers[0].model_dump(mode="json")
    assert set(visible_offer) == {"offer", "intent_description", "parameter_specs"}
    assert "agent" not in visible_offer
    assert "capabilities" not in visible_offer
    assert "provider" not in visible_offer
    assert planner_input.offer_set_digest == sha256_digest(
        {"offers": [offer.ref.model_dump(mode="json")]}
    )

    decision = TurnPlannerDecision.model_validate(_single_step_response(offer))
    assert decision.root.kind == "propose_steps"
    assert decision.root.steps[0].offer_key == offer.offer_key
    needs_input = TurnPlannerDecision.model_validate(
        {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "needs_input",
            "offer_key": offer.offer_key,
            "missing_parameters": ["query"],
        }
    )
    unsupported = TurnPlannerDecision.model_validate(
        {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "unsupported",
        }
    )
    assert needs_input.root.kind == "needs_input"
    assert unsupported.root.kind == "unsupported"

    with pytest.raises(ValidationError, match="extra_forbidden"):
        TurnPlannerDecision.model_validate(
            {
                **_single_step_response(offer),
                "command": "powershell.exe",
            }
        )
    with pytest.raises(ValidationError, match="duplicate parameters"):
        TurnPlannerDecision.model_validate(
            {
                "schema_version": "deskpilot.turn-planner-decision.v1",
                "kind": "propose_steps",
                "steps": [
                    {
                        "offer_key": offer.offer_key,
                        "parameters": [
                            {"name": "query", "value": "cats"},
                            {"name": "query", "value": "dogs"},
                        ],
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="offer digest"):
        TurnPlanningOffer.model_validate(
            {**offer.model_dump(mode="json"), "offer_digest": "0" * 64}
        )
    with pytest.raises(ValidationError, match="parameter span"):
        TurnPlanningParameterBinding(
            offer_key=offer.offer_key,
            parameter_name="query",
            value="cats",
            source_start=16,
            source_end=19,
            value_digest=sha256_digest({"value": "cats"}),
        )


def test_offer_seals_precompiled_plan_and_rejects_registry_binding_drift() -> None:
    offer = _offer()

    assert offer.expected_plan.plan_generation == 1
    assert offer.expected_plan.runtime_enabled is True
    assert offer.execution_agents
    assert offer.execution_agents == tuple(
        sorted(
            {
                node.bound_agent
                for node in offer.expected_plan.nodes
                if node.bound_agent is not None
            },
            key=lambda item: (
                item.agent_id,
                item.version,
                item.contract_digest,
                item.prompt_package_digest,
            ),
        )
    )
    assert all(item.agent_id != "builtin.turn_planner" for item in offer.execution_agents)

    drifted = _plan_with_registry_drift(offer.expected_plan)
    with pytest.raises(ValueError, match="expected Plan binding drifted"):
        offer.validate_recompiled_plan(drifted)

    tampered = offer.model_dump(mode="json")
    tampered["expected_plan"] = drifted.model_dump(mode="json")
    with pytest.raises(ValidationError, match="execution Agent binding changed"):
        TurnPlanningOffer.model_validate(tampered)

    drifted_offer = _offer(expected_plan_override=drifted)
    assert drifted_offer.offer_id != offer.offer_id
    assert drifted_offer.offer_digest != offer.offer_digest


def test_turn_planner_run_fencing_and_terminal_projection_are_fail_closed() -> None:
    offer = _offer()
    reserved = _reserve((offer,))
    waiting = TurnPlanningRead.build(offers=(offer,), run=reserved)
    assert waiting.revision == 1
    assert waiting.adjudication is None
    with pytest.raises(ValidationError, match="reservation digest"):
        TurnPlannerRun.model_validate(
            {
                **reserved.model_dump(mode="json"),
                "fallback_candidate_digest": "1" * 64,
            }
        )

    with pytest.raises(ValueError, match="stale or invalid"):
        reserved.evolve(
            status="dispatching",
            revision=1,
            updated_at=NOW + timedelta(seconds=1),
            claim_owner_id="stale-worker",
            claim_fencing_token=1,
            claim_expires_at=NOW + timedelta(seconds=31),
            request_dispatched_at=NOW + timedelta(seconds=1),
        )

    dispatching = _dispatch(reserved)
    assert dispatching.run_id == reserved.run_id
    assert dispatching.reservation_digest == reserved.reservation_digest
    response = _single_step_response(offer)
    succeeded = dispatching.evolve(
        status="succeeded",
        revision=3,
        updated_at=NOW + timedelta(seconds=2),
        claim_fencing_token=1,
        request_dispatched_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
        response_manifest=response,
    )
    parameter = TurnPlanningParameterBinding.build(
        offer_key=offer.offer_key,
        parameter_name="query",
        value="cats",
        source_start=16,
        source_end=20,
    )
    adjudication = TurnPlannerAdjudication.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        run_id=succeeded.run_id,
        run_digest=succeeded.run_digest,
        outcome="single_step",
        selected_offers=(offer.ref,),
        parameter_bindings=(parameter,),
        proposal_manifest=response,
        reason_code="SINGLE_STEP_ACCEPTED",
        created_at=NOW + timedelta(seconds=2),
    )
    binding = TurnPlanBinding.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        adjudication_id=adjudication.adjudication_id,
        adjudication_digest=adjudication.adjudication_digest,
        status="bound",
        offer=offer.ref,
        plan=TurnPlanningPlanRef(
            plan_id=offer.expected_plan.plan_id,
            plan_generation=offer.expected_plan.plan_generation,
            plan_manifest_digest=offer.expected_plan.plan_manifest_digest,
            task_contract=offer.task_contract,
        ),
        reason_code="SINGLE_STEP_BOUND",
        created_at=NOW + timedelta(seconds=2),
    )
    completed = TurnPlanningRead.build(
        offers=(offer,),
        run=succeeded,
        adjudication=adjudication,
        binding=binding,
    )
    assert completed.binding is not None
    assert completed.binding.binding_digest == binding.binding_digest

    wrong_plan_binding = TurnPlanBinding.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        adjudication_id=adjudication.adjudication_id,
        adjudication_digest=adjudication.adjudication_digest,
        status="bound",
        offer=offer.ref,
        plan=TurnPlanningPlanRef(
            plan_id="epl_" + "1" * 64,
            plan_generation=1,
            plan_manifest_digest="2" * 64,
            task_contract=offer.task_contract,
        ),
        reason_code="SINGLE_STEP_BOUND",
        created_at=NOW + timedelta(seconds=2),
    )
    with pytest.raises(ValidationError, match="offered executable Plan"):
        TurnPlanningRead.build(
            offers=(offer,),
            run=succeeded,
            adjudication=adjudication,
            binding=wrong_plan_binding,
        )

    with pytest.raises(ValidationError, match="lacks an adjudication"):
        TurnPlanningRead.build(offers=(offer,), run=succeeded)
    with pytest.raises(ValueError, match="stale or invalid"):
        succeeded.evolve(
            status="failed",
            revision=4,
            updated_at=NOW + timedelta(seconds=3),
            claim_fencing_token=1,
            request_dispatched_at=NOW + timedelta(seconds=1),
            completed_at=NOW + timedelta(seconds=3),
            failure=TurnPlannerFailureProof.build(
                error_code="PLANNER_SCHEMA_REJECTED",
                detail_digest="3" * 64,
            ),
        )

    failure = TurnPlannerFailureProof.build(
        error_code="PLANNER_OUTCOME_UNKNOWN",
        detail_digest="4" * 64,
    )
    unknown = dispatching.evolve(
        status="outcome_unknown",
        revision=3,
        updated_at=NOW + timedelta(seconds=32),
        claim_fencing_token=1,
        request_dispatched_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=32),
        failure=failure,
    )
    fallback = TurnPlannerAdjudication.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        run_id=unknown.run_id,
        run_digest=unknown.run_digest,
        outcome="deterministic_fallback",
        reason_code="PLANNER_OUTCOME_UNKNOWN",
        created_at=NOW + timedelta(seconds=32),
    )
    no_plan = TurnPlanBinding.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        adjudication_id=fallback.adjudication_id,
        adjudication_digest=fallback.adjudication_digest,
        status="not_applicable",
        reason_code="PLANNER_OUTCOME_UNKNOWN",
        created_at=NOW + timedelta(seconds=32),
    )
    recovered = TurnPlanningRead.build(
        offers=(offer,),
        run=unknown,
        adjudication=fallback,
        binding=no_plan,
    )
    assert recovered.run.failure is not None
    assert recovered.run.failure.retry_policy == "never_automatic"


def test_multi_step_adjudication_is_persisted_but_cannot_bind_a_plan() -> None:
    first = _offer()
    second = _offer(
        offer_key="ofk_" + "6" * 64,
        route_id="summarize",
    )
    reserved = _reserve((first, second))
    dispatching = _dispatch(reserved)
    response: dict[str, object] = {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": first.offer_key,
                "parameters": [{"name": "query", "value": "cats"}],
            },
            {
                "offer_key": second.offer_key,
                "parameters": [{"name": "query", "value": "dogs"}],
            },
        ],
    }
    succeeded = dispatching.evolve(
        status="succeeded",
        revision=3,
        updated_at=NOW + timedelta(seconds=2),
        claim_fencing_token=1,
        request_dispatched_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
        response_manifest=response,
    )
    adjudication = TurnPlannerAdjudication.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        run_id=succeeded.run_id,
        run_digest=succeeded.run_digest,
        outcome="multi_step_deferred",
        selected_offers=(first.ref, second.ref),
        parameter_bindings=(
            TurnPlanningParameterBinding.build(
                offer_key=first.offer_key,
                parameter_name="query",
                value="cats",
                source_start=0,
                source_end=4,
            ),
            TurnPlanningParameterBinding.build(
                offer_key=second.offer_key,
                parameter_name="query",
                value="dogs",
                source_start=9,
                source_end=13,
            ),
        ),
        proposal_manifest=response,
        reason_code="MULTI_STEP_PLAN_DEFERRED",
        created_at=NOW + timedelta(seconds=2),
    )
    binding = TurnPlanBinding.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        adjudication_id=adjudication.adjudication_id,
        adjudication_digest=adjudication.adjudication_digest,
        status="multi_step_deferred",
        reason_code="MULTI_STEP_PLAN_DEFERRED",
        created_at=NOW + timedelta(seconds=2),
    )
    projection = TurnPlanningRead.build(
        offers=(first, second),
        run=succeeded,
        adjudication=adjudication,
        binding=binding,
    )

    assert projection.binding is not None
    assert projection.binding.plan is None
    with pytest.raises(ValidationError, match="Deferred turn plan binding reason"):
        TurnPlanBinding.model_validate(
            {
                **binding.model_dump(mode="json"),
                "reason_code": "MODEL_APPROVED",
            }
        )


@pytest.mark.parametrize(
    ("kind", "outcome", "selected", "reason_code"),
    [
        ("needs_input", "needs_user_input", True, "PLANNER_NEEDS_INPUT"),
        ("unsupported", "unsupported", False, "PLANNER_UNSUPPORTED"),
    ],
)
def test_non_plan_model_decisions_keep_proposal_proof_without_a_plan(
    kind: str,
    outcome: str,
    selected: bool,
    reason_code: str,
) -> None:
    offer = _offer()
    response: dict[str, object] = (
        {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "needs_input",
            "offer_key": offer.offer_key,
            "missing_parameters": ["query"],
        }
        if kind == "needs_input"
        else {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "unsupported",
        }
    )
    succeeded = _dispatch(_reserve((offer,))).evolve(
        status="succeeded",
        revision=3,
        updated_at=NOW + timedelta(seconds=2),
        claim_fencing_token=1,
        request_dispatched_at=NOW + timedelta(seconds=1),
        completed_at=NOW + timedelta(seconds=2),
        response_manifest=response,
    )
    adjudication = TurnPlannerAdjudication.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        run_id=succeeded.run_id,
        run_digest=succeeded.run_digest,
        outcome=outcome,  # type: ignore[arg-type]
        selected_offers=(offer.ref,) if selected else (),
        proposal_manifest=response,
        reason_code=reason_code,
        created_at=NOW + timedelta(seconds=2),
    )
    binding = TurnPlanBinding.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        adjudication_id=adjudication.adjudication_id,
        adjudication_digest=adjudication.adjudication_digest,
        status="not_applicable",
        reason_code=reason_code,
        created_at=NOW + timedelta(seconds=2),
    )
    projection = TurnPlanningRead.build(
        offers=(offer,),
        run=succeeded,
        adjudication=adjudication,
        binding=binding,
    )

    assert projection.adjudication is not None
    assert projection.adjudication.proposal_digest == sha256_digest(response)
    assert projection.binding is not None
    assert projection.binding.plan is None


def _offer_record(offer: TurnPlanningOffer) -> TurnPlanningOfferRecord:
    return TurnPlanningOfferRecord(
        offer_id=offer.offer_id,
        offer_key=offer.offer_key,
        task_id=offer.task_id,
        user_message_id=offer.user_message_id,
        user_message_digest=offer.user_message_digest,
        contract_id=offer.task_contract.contract_id,
        contract_version=offer.task_contract.version,
        contract_digest=offer.task_contract.digest,
        execution_agents_manifest=[
            item.model_dump(mode="json") for item in offer.execution_agents
        ],
        execution_agents_digest=offer.execution_agents_digest,
        expected_plan_manifest=offer.expected_plan.model_dump(mode="json"),
        expected_plan_id=offer.expected_plan.plan_id,
        expected_plan_generation=offer.expected_plan.plan_generation,
        expected_plan_manifest_digest=offer.expected_plan.plan_manifest_digest,
        expected_plan_binding_snapshot_digest=(
            offer.expected_plan.binding_snapshot_digest
        ),
        capabilities_manifest=[
            item.model_dump(mode="json") for item in offer.capabilities
        ],
        capabilities_digest=offer.capabilities_digest,
        provider_id=offer.provider.provider_id,
        provider_model=offer.provider.model,
        provider_snapshot_digest=offer.provider_snapshot_digest,
        recipe_id=offer.trusted_recipe.route_id,
        recipe_version=offer.trusted_recipe.route_version,
        recipe_digest=offer.trusted_recipe.route_manifest_digest,
        budget_manifest=offer.budget.model_dump(mode="json"),
        budget_digest=offer.budget_digest,
        parameter_schema_manifest=[
            item.model_dump(mode="json") for item in offer.parameter_specs
        ],
        parameter_schema_digest=offer.parameter_schema_digest,
        policy_snapshot_digest=offer.policy_snapshot_digest,
        manifest=offer.model_dump(mode="json"),
        offer_digest=offer.offer_digest,
        created_at=offer.created_at,
    )


def _run_record(run: TurnPlannerRun) -> TurnPlannerRunRecord:
    failure = run.failure
    return TurnPlannerRunRecord(
        run_id=run.run_id,
        task_id=run.task_id,
        user_message_id=run.user_message_id,
        user_message_digest=run.user_message_digest,
        planner_agent_id=run.planner_agent.agent_id,
        planner_agent_version=run.planner_agent.version,
        planner_contract_digest=run.planner_agent.contract_digest,
        planner_prompt_package_digest=run.planner_agent.prompt_package_digest,
        provider_id=run.provider.provider_id,
        provider_model=run.provider.model,
        provider_snapshot_digest=run.provider_snapshot_digest,
        offer_set_digest=run.offer_set_digest,
        request_digest=run.request_digest,
        fallback_candidate_digest=run.fallback_candidate_digest,
        reservation_digest=run.reservation_digest,
        status=run.status,
        revision=run.revision,
        claim_owner_id=run.claim_owner_id,
        claim_fencing_token=run.claim_fencing_token,
        claim_expires_at=run.claim_expires_at,
        request_dispatched_at=run.request_dispatched_at,
        response_digest=run.response_digest,
        failure_code=failure.error_code if failure is not None else None,
        failure_digest=failure.failure_digest if failure is not None else None,
        manifest=run.model_dump(mode="json"),
        run_digest=run.run_digest,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _apply_run(record: TurnPlannerRunRecord, run: TurnPlannerRun) -> None:
    record.status = run.status
    record.revision = run.revision
    record.claim_owner_id = run.claim_owner_id
    record.claim_fencing_token = run.claim_fencing_token
    record.claim_expires_at = run.claim_expires_at
    record.request_dispatched_at = run.request_dispatched_at
    record.response_digest = run.response_digest
    record.failure_code = run.failure.error_code if run.failure is not None else None
    record.failure_digest = (
        run.failure.failure_digest if run.failure is not None else None
    )
    record.manifest = run.model_dump(mode="json")
    record.run_digest = run.run_digest
    record.completed_at = run.completed_at
    record.updated_at = run.updated_at


@pytest.mark.asyncio
async def test_turn_planning_records_persist_reservation_and_exact_failure_lineage(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'turn-planning.db').as_posix()}"
    )
    await database.migrate()
    offer = _offer()
    reserved = _reserve((offer,))

    try:
        async with database.session_factory() as session:
            session.add_all(
                [
                    ConversationRecord(
                        conversation_id=CONVERSATION_ID,
                        title="Stage 111",
                        created_at=NOW,
                    ),
                    TaskRecord(
                        task_id=TASK_ID,
                        conversation_id=CONVERSATION_ID,
                        goal="Research cats",
                        status="ready",
                        mode="fake",
                        privacy_mode="local_only",
                        constraints=[],
                        last_event_seq=0,
                        created_at=NOW,
                        updated_at=NOW,
                    ),
                ]
            )
            await session.commit()
            session.add(
                ConversationMessageRecord(
                    message_id=MESSAGE_ID,
                    conversation_id=CONVERSATION_ID,
                    task_id=TASK_ID,
                    role="user",
                    content="please research cats",
                    content_ref=None,
                    classification="internal",
                    status="active",
                    message_digest=MESSAGE_DIGEST,
                    created_at=NOW,
                    deleted_at=None,
                )
            )
            await session.commit()
            session.add_all([_offer_record(offer), _run_record(reserved)])
            await session.commit()

            stored_offer = await session.get(TurnPlanningOfferRecord, offer.offer_id)
            stored_run = await session.get(TurnPlannerRunRecord, reserved.run_id)
            assert stored_offer is not None
            assert stored_run is not None
            assert TurnPlanningOffer.model_validate(stored_offer.manifest) == offer
            assert TurnPlannerRun.model_validate(stored_run.manifest) == reserved

            competing = TurnPlannerRun.reserve(
                task_id=TASK_ID,
                user_message_id=MESSAGE_ID,
                user_message_digest=MESSAGE_DIGEST,
                planner_agent=_planner_agent(),
                provider=_provider(),
                offers=(offer.ref,),
                request_digest="0" * 64,
                fallback_candidate_digest="1" * 64,
                created_at=NOW + timedelta(milliseconds=1),
            )
            session.add(_run_record(competing))
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            stored_run = await session.get(TurnPlannerRunRecord, reserved.run_id)
            assert stored_run is not None
            dispatching = _dispatch(reserved)
            _apply_run(stored_run, dispatching)
            await session.commit()

            failure = TurnPlannerFailureProof.build(
                error_code="PLANNER_TIMEOUT",
                detail_digest="1" * 64,
            )
            failed = dispatching.evolve(
                status="failed",
                revision=3,
                updated_at=NOW + timedelta(seconds=2),
                claim_fencing_token=1,
                request_dispatched_at=NOW + timedelta(seconds=1),
                completed_at=NOW + timedelta(seconds=2),
                failure=failure,
            )
            adjudication = TurnPlannerAdjudication.build(
                task_id=TASK_ID,
                user_message_id=MESSAGE_ID,
                user_message_digest=MESSAGE_DIGEST,
                run_id=failed.run_id,
                run_digest=failed.run_digest,
                outcome="deterministic_fallback",
                reason_code="PLANNER_TIMEOUT",
                created_at=NOW + timedelta(seconds=2),
            )
            binding = TurnPlanBinding.build(
                task_id=TASK_ID,
                user_message_id=MESSAGE_ID,
                user_message_digest=MESSAGE_DIGEST,
                adjudication_id=adjudication.adjudication_id,
                adjudication_digest=adjudication.adjudication_digest,
                status="not_applicable",
                reason_code="PLANNER_TIMEOUT",
                created_at=NOW + timedelta(seconds=2),
            )
            _apply_run(stored_run, failed)
            await session.flush()
            session.add(
                TurnPlannerAdjudicationRecord(
                    adjudication_id=adjudication.adjudication_id,
                    task_id=adjudication.task_id,
                    user_message_id=adjudication.user_message_id,
                    user_message_digest=adjudication.user_message_digest,
                    run_id=adjudication.run_id,
                    run_digest=adjudication.run_digest,
                    outcome=adjudication.outcome,
                    selected_offer_count=0,
                    parameter_bindings_manifest=None,
                    parameter_bindings_digest=None,
                    proposal_digest=None,
                    reason_code=adjudication.reason_code,
                    manifest=adjudication.model_dump(mode="json"),
                    adjudication_digest=adjudication.adjudication_digest,
                    created_at=adjudication.created_at,
                )
            )
            await session.flush()
            session.add(
                TurnPlanBindingRecord(
                    binding_id=binding.binding_id,
                    task_id=binding.task_id,
                    user_message_id=binding.user_message_id,
                    user_message_digest=binding.user_message_digest,
                    adjudication_id=binding.adjudication_id,
                    adjudication_digest=binding.adjudication_digest,
                    status=binding.status,
                    offer_id=None,
                    offer_digest=None,
                    plan_id=None,
                    plan_generation=None,
                    plan_manifest_digest=None,
                    contract_id=None,
                    contract_version=None,
                    contract_digest=None,
                    reason_code=binding.reason_code,
                    manifest=binding.model_dump(mode="json"),
                    binding_digest=binding.binding_digest,
                    created_at=binding.created_at,
                )
            )
            await session.commit()

            stored_adjudication = await session.scalar(
                select(TurnPlannerAdjudicationRecord).where(
                    TurnPlannerAdjudicationRecord.run_id == failed.run_id
                )
            )
            stored_binding = await session.scalar(
                select(TurnPlanBindingRecord).where(
                    TurnPlanBindingRecord.adjudication_id
                    == adjudication.adjudication_id
                )
            )
            assert stored_adjudication is not None
            assert stored_binding is not None
            assert (
                TurnPlannerAdjudication.model_validate(stored_adjudication.manifest)
                == adjudication
            )
            assert TurnPlanBinding.model_validate(stored_binding.manifest) == binding

            session.add(
                TurnRouteRecord(
                    task_id=TASK_ID,
                    conversation_id=CONVERSATION_ID,
                    user_message_id=MESSAGE_ID,
                    decision="unsupported",
                    route_id=None,
                    route_version=None,
                    route_manifest_digest=None,
                    candidate_digest="2" * 64,
                    parameters={},
                    parameter_digest="3" * 64,
                    resolved_from_task_id=None,
                    resolution_rule=None,
                    resolution_digest=None,
                    turn_planner_run_id=failed.run_id,
                    turn_planning_reservation_digest=failed.reservation_digest,
                    turn_planning_adjudication_id=adjudication.adjudication_id,
                    turn_plan_binding_id=binding.binding_id,
                    turn_plan_binding_digest="0" * 64,
                    turn_planning_provenance_digest="5" * 64,
                    reason_code="PLANNER_TIMEOUT",
                    status="not_applicable",
                    result_manifest=None,
                    result_digest=None,
                    error_code=None,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add(
                TurnRouteRecord(
                    task_id=TASK_ID,
                    conversation_id=CONVERSATION_ID,
                    user_message_id=MESSAGE_ID,
                    decision="unsupported",
                    route_id=None,
                    route_version=None,
                    route_manifest_digest=None,
                    candidate_digest="2" * 64,
                    parameters={},
                    parameter_digest="3" * 64,
                    resolved_from_task_id=None,
                    resolution_rule=None,
                    resolution_digest=None,
                    turn_planner_run_id=failed.run_id,
                    turn_planning_reservation_digest="0" * 64,
                    turn_planning_adjudication_id=None,
                    turn_plan_binding_id=None,
                    turn_plan_binding_digest=None,
                    turn_planning_provenance_digest=None,
                    reason_code="PLANNER_TIMEOUT",
                    status="not_applicable",
                    result_manifest=None,
                    result_digest=None,
                    error_code=None,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()

            session.add(
                TurnRouteRecord(
                    task_id=TASK_ID,
                    conversation_id=CONVERSATION_ID,
                    user_message_id=MESSAGE_ID,
                    decision="unsupported",
                    route_id=None,
                    route_version=None,
                    route_manifest_digest=None,
                    candidate_digest="2" * 64,
                    parameters={},
                    parameter_digest="3" * 64,
                    resolved_from_task_id=None,
                    resolution_rule=None,
                    resolution_digest=None,
                    turn_planner_run_id=failed.run_id,
                    turn_planning_reservation_digest=failed.reservation_digest,
                    turn_planning_adjudication_id=None,
                    turn_plan_binding_id=None,
                    turn_plan_binding_digest=None,
                    turn_planning_provenance_digest=None,
                    reason_code="PLANNER_TIMEOUT",
                    status="not_applicable",
                    result_manifest=None,
                    result_digest=None,
                    error_code=None,
                    revision=1,
                    created_at=NOW,
                    updated_at=NOW,
                )
            )
            await session.commit()

            with pytest.raises(IntegrityError):
                await session.execute(
                    delete(TurnPlannerRunRecord).where(
                        TurnPlannerRunRecord.run_id == failed.run_id
                    )
                )
                await session.commit()
            await session.rollback()

            await session.execute(
                delete(TaskRecord).where(TaskRecord.task_id == TASK_ID)
            )
            await session.commit()
            assert await session.get(TurnRouteRecord, TASK_ID) is None
            assert await session.get(TurnPlannerRunRecord, failed.run_id) is None
    finally:
        await database.dispose()
