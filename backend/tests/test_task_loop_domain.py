from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.plan_compiler import (
    PlanCompiler,
    knowledge_lookup_contract,
    knowledge_lookup_draft,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.task_loop import (
    MODEL_PLANNER_COMPOSER_VERSION,
    ModelPlannerDraft,
    ModelPlannerFailureProof,
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
    TaskLoop,
    TaskLoopEvent,
    TaskLoopSourceRef,
    TaskLoopWorkbenchRead,
)
from deskpilot.domain.task_plans import (
    DraftPlan,
    ExecutablePlan,
    PlanNodeBudget,
    PlanProducer,
    TaskContract,
)
from deskpilot.domain.turn_planning import (
    TurnPlanningOfferRef,
    TurnPlanningParameterBinding,
    TurnPlanningRecipeRef,
)
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, tzinfo=UTC)
TASK_ID = "tsk_" + "1" * 32
MESSAGE_ID = "msg_" + "2" * 32


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


def _source(*, binding_digit: str = "9") -> TaskLoopSourceRef:
    return TaskLoopSourceRef(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest="3" * 64,
        turn_planner_run_id="tpr_" + "4" * 64,
        turn_planner_run_digest="5" * 64,
        adjudication_id="tpa_" + "6" * 64,
        adjudication_digest="7" * 64,
        turn_plan_binding_id="tpb_" + "8" * 64,
        turn_plan_binding_digest=binding_digit * 64,
    )


def _model_plan() -> tuple[TaskContract, DraftPlan, ExecutablePlan]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (_provider(),))
    compiler = PlanCompiler(agents, tools, capabilities)
    contract = knowledge_lookup_contract(TASK_ID, capabilities)
    trusted = knowledge_lookup_draft(TASK_ID)
    draft = DraftPlan.model_validate(
        {
            **trusted.model_dump(mode="json"),
            "producer": PlanProducer(
                kind="model_planner",
                producer_ref=MODEL_PLANNER_COMPOSER_VERSION,
            ).model_dump(mode="json"),
        }
    )
    return contract, draft, compiler.compile(contract, draft, generation=1)


def _step(
    source: TaskLoopSourceRef,
    *,
    ordinal: int,
    offer_digit: str,
    value: str,
) -> ModelPlannerStepBinding:
    _, _, plan = _model_plan()
    node = plan.nodes[0]
    offer = TurnPlanningOfferRef(
        offer_id="tpo_" + offer_digit * 64,
        offer_key="ofk_" + offer_digit * 64,
        offer_digest=offer_digit * 64,
    )
    parameter = TurnPlanningParameterBinding.build(
        offer_key=offer.offer_key,
        parameter_name="query",
        value=value,
        source_start=ordinal - 1,
        source_end=ordinal - 1 + len(value),
    )
    mapping = ModelPlannerNodeMapping.build(
        source_node_id=node.node_id,
        source_local_key=node.local_key,
        source_node_spec_digest=node.node_spec_digest,
        composite_node_id="pnd_" + offer_digit * 64,
        composite_local_key=f"s0{ordinal}_n01",
        composite_node_spec_digest=("a" if ordinal == 1 else "b") * 64,
    )
    return ModelPlannerStepBinding.build(
        source=source,
        ordinal=ordinal,
        offer=offer,
        recipe=TurnPlanningRecipeRef(
            route_id="knowledge_lookup",
            route_version="2",
            route_manifest_digest="c" * 64,
        ),
        policy_snapshot_digest="d" * 64,
        source_plan_id=plan.plan_id,
        source_plan_manifest_digest=plan.plan_manifest_digest,
        source_plan_binding_snapshot_digest=plan.binding_snapshot_digest,
        budget=PlanNodeBudget(
            model_calls=0,
            tool_calls=1,
            input_tokens=0,
            output_tokens=0,
            wall_seconds=60,
            retries=0,
            cost_micros=0,
            handoffs=0,
        ),
        parameter_bindings=(parameter,),
        node_mappings=(mapping,),
        created_at=NOW,
    )


def _draft(
    source: TaskLoopSourceRef,
) -> tuple[ModelPlannerDraft, tuple[ModelPlannerStepBinding, ...]]:
    contract, draft_plan, expected_plan = _model_plan()
    steps = (
        _step(source, ordinal=1, offer_digit="1", value="cats"),
        _step(source, ordinal=2, offer_digit="2", value="dogs"),
    )
    return (
        ModelPlannerDraft.build(
            source=source,
            steps=tuple(item.ref for item in steps),
            task_contract=contract,
            draft_plan=draft_plan,
            expected_plan=expected_plan,
            created_at=NOW + timedelta(seconds=1),
        ),
        steps,
    )


def test_task_loop_observe_plan_chain_is_content_addressed_and_publicly_sanitized() -> None:
    source = _source()
    draft, steps = _draft(source)
    observed_event = TaskLoopEvent.observe(source=source, created_at=NOW)
    observed = TaskLoop.observed(observed_event)
    plan_event = TaskLoopEvent.plan(
        observed=observed_event,
        draft=draft.ref,
        created_at=NOW + timedelta(seconds=2),
    )
    planned = observed.settle_plan(plan_event)
    public = TaskLoopWorkbenchRead.from_internal(planned)

    assert planned.status == "planned"
    assert planned.active_draft == draft.ref
    assert plan_event.previous_event_digest == observed_event.event_digest
    assert public.status == "planned"
    assert public.step_count == 2
    assert public.recoverable is False
    serialized = public.model_dump_json()
    assert "cats" not in serialized
    assert "dogs" not in serialized
    assert steps[0].offer.offer_key not in serialized
    assert "parameter_bindings" not in serialized
    assert "task_contract" not in serialized
    assert '"expected_plan":' not in serialized
    assert '"draft_plan":' not in serialized


def test_task_loop_failure_is_terminal_and_never_automatically_recoverable() -> None:
    source = _source()
    observed_event = TaskLoopEvent.observe(source=source, created_at=NOW)
    observed = TaskLoop.observed(observed_event)
    failure = ModelPlannerFailureProof.build(
        error_code="MULTI_STEP_BUDGET_EXCEEDED",
        reason_code="MULTI_STEP_BUDGET_EXCEEDED",
        detail_digest="a" * 64,
    )
    failed_event = TaskLoopEvent.plan(
        observed=observed_event,
        failure=failure,
        created_at=NOW + timedelta(seconds=1),
    )
    failed = observed.settle_plan(failed_event)
    public = TaskLoopWorkbenchRead.from_internal(failed)

    assert failed.status == "failed"
    assert public.failure is not None
    assert public.failure.retry_policy == "never_automatic"
    assert public.recoverable is False
    with pytest.raises(ValueError, match="stale or crosses lineage"):
        failed.settle_plan(failed_event)


def test_task_loop_domain_rejects_digest_time_and_lineage_tampering() -> None:
    source = _source()
    draft, steps = _draft(source)
    observed = TaskLoopEvent.observe(source=source, created_at=NOW)

    with pytest.raises(ValidationError, match="step binding digest"):
        ModelPlannerStepBinding.model_validate(
            {
                **steps[0].model_dump(mode="json"),
                "step_binding_digest": "0" * 64,
            }
        )
    with pytest.raises(ValidationError, match="step ordinals"):
        ModelPlannerDraft.model_validate(
            {
                **draft.model_dump(mode="json"),
                "steps": [
                    {**draft.steps[0].model_dump(mode="json"), "ordinal": 2},
                    draft.steps[1].model_dump(mode="json"),
                ],
            }
        )
    with pytest.raises(ValidationError, match="timezone-aware"):
        TaskLoopEvent.model_validate(
            {**observed.model_dump(mode="json"), "created_at": NOW.replace(tzinfo=None)}
        )
    other_source = _source(binding_digit="0")
    other_observed = TaskLoopEvent.observe(source=other_source, created_at=NOW)
    foreign_event = TaskLoopEvent.plan(
        observed=other_observed,
        draft=draft.ref,
        created_at=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="crosses lineage"):
        TaskLoop.observed(observed).settle_plan(foreign_event)

    public = TaskLoopWorkbenchRead.from_internal(TaskLoop.observed(observed))
    with pytest.raises(ValidationError, match="projection digest"):
        TaskLoopWorkbenchRead.model_validate(
            {**public.model_dump(mode="json"), "projection_digest": sha256_digest({})}
        )
