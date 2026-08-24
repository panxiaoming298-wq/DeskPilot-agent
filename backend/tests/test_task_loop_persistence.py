from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.plan_compiler import (
    PlanCompiler,
    knowledge_lookup_contract,
    knowledge_lookup_draft,
)
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.task_loop import (
    MODEL_PLANNER_COMPOSER_VERSION,
    ModelPlannerDraft,
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
    TaskLoop,
    TaskLoopEvent,
    TaskLoopSourceRef,
)
from deskpilot.domain.task_plans import (
    DraftPlan,
    ExecutablePlan,
    PlanNodeBudget,
    PlanProducer,
    TaskContract,
)
from deskpilot.domain.turn_planning import (
    TurnPlanBinding,
    TurnPlannerAdjudication,
    TurnPlannerRun,
    TurnPlanningOffer,
    TurnPlanningParameterBinding,
    TurnPlanningParameterSpec,
    TurnPlanningRecipeRef,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    ConversationRecord,
    ModelPlannerDraftRecord,
    ModelPlannerStepBindingRecord,
    TaskLoopEventRecord,
    TaskLoopRecord,
    TaskRecord,
    TurnPlanBindingRecord,
    TurnPlannerAdjudicationRecord,
    TurnPlannerRunRecord,
    TurnPlanningOfferRecord,
)
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, tzinfo=UTC)
TASK_ID = "tsk_" + "a" * 32
MESSAGE_ID = "msg_" + "b" * 32
CONVERSATION_ID = "conv_" + "c" * 32
MESSAGE = "cats dogs"
MESSAGE_DIGEST = "d" * 64


def _provider() -> ModelProviderDescriptor:
    return ModelProviderDescriptor(
        provider_id="local_task_loop",
        display_name="Local task-loop provider",
        model="test-model",
        protocol=ModelProtocol.FAKE,
        location=ModelLocation.LOCAL,
        capabilities=ModelCapabilities(
            structured_output=True,
            strict_json_schema=True,
        ),
    )


def _compiled(*, model_planner: bool) -> tuple[TaskContract, DraftPlan, ExecutablePlan]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (_provider(),))
    compiler = PlanCompiler(agents, tools, capabilities)
    contract = knowledge_lookup_contract(TASK_ID, capabilities)
    draft = knowledge_lookup_draft(TASK_ID)
    if model_planner:
        draft = DraftPlan.model_validate(
            {
                **draft.model_dump(mode="json"),
                "producer": PlanProducer(
                    kind="model_planner",
                    producer_ref=MODEL_PLANNER_COMPOSER_VERSION,
                ).model_dump(mode="json"),
            }
        )
    return contract, draft, compiler.compile(contract, draft, generation=1)


def _offer(offer_key_digit: str, *, created_at: datetime) -> TurnPlanningOffer:
    contract, _, expected_plan = _compiled(model_planner=False)
    return TurnPlanningOffer.build(
        offer_key="ofk_" + offer_key_digit * 64,
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        intent_description="Look up one exact persisted query.",
        task_contract=expected_plan.task_contract,
        expected_plan=expected_plan,
        capabilities=contract.capabilities,
        provider=_provider(),
        trusted_recipe=TurnPlanningRecipeRef(
            route_id="knowledge_lookup",
            route_version="2",
            route_manifest_digest="e" * 64,
        ),
        budget=PlanNodeBudget(
            model_calls=0,
            tool_calls=1,
            input_tokens=0,
            output_tokens=0,
            wall_seconds=90,
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
        policy_snapshot_digest="f" * 64,
        created_at=created_at,
    )


def _v1_lineage() -> tuple[
    tuple[TurnPlanningOffer, TurnPlanningOffer],
    TurnPlannerRun,
    TurnPlannerAdjudication,
    TurnPlanBinding,
]:
    offers = (_offer("1", created_at=NOW), _offer("2", created_at=NOW))
    reserved = TurnPlannerRun.reserve(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        planner_agent=BoundAgentRef(
            agent_id="builtin.turn_planner",
            version="1.0.0",
            contract_digest="1" * 64,
            prompt_package_digest="2" * 64,
        ),
        provider=_provider(),
        offers=tuple(item.ref for item in offers),
        request_digest="3" * 64,
        fallback_candidate_digest="4" * 64,
        created_at=NOW,
    )
    dispatching = reserved.evolve(
        status="dispatching",
        revision=2,
        updated_at=NOW + timedelta(seconds=1),
        claim_owner_id="task-loop-worker",
        claim_fencing_token=1,
        claim_expires_at=NOW + timedelta(seconds=31),
        request_dispatched_at=NOW + timedelta(seconds=1),
    )
    response = {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": offers[0].offer_key,
                "parameters": [{"name": "query", "value": "cats"}],
            },
            {
                "offer_key": offers[1].offer_key,
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
    parameters = (
        TurnPlanningParameterBinding.build(
            offer_key=offers[0].offer_key,
            parameter_name="query",
            value="cats",
            source_start=0,
            source_end=4,
        ),
        TurnPlanningParameterBinding.build(
            offer_key=offers[1].offer_key,
            parameter_name="query",
            value="dogs",
            source_start=5,
            source_end=9,
        ),
    )
    adjudication = TurnPlannerAdjudication.build(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        run_id=succeeded.run_id,
        run_digest=succeeded.run_digest,
        outcome="multi_step_deferred",
        selected_offers=tuple(item.ref for item in offers),
        parameter_bindings=parameters,
        proposal_manifest={
            "schema_version": "deskpilot.turn-planner-validated-proposal.v1",
            "decision": response,
            "steps": [],
            "grants_authority": False,
        },
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
    return offers, succeeded, adjudication, binding


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
        expected_plan_binding_snapshot_digest=offer.expected_plan.binding_snapshot_digest,
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
        failure_code=None,
        failure_digest=None,
        manifest=run.model_dump(mode="json"),
        run_digest=run.run_digest,
        completed_at=run.completed_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def _task_loop_proofs(
    offers: tuple[TurnPlanningOffer, TurnPlanningOffer],
    run: TurnPlannerRun,
    adjudication: TurnPlannerAdjudication,
    binding: TurnPlanBinding,
) -> tuple[
    TaskLoopEvent,
    TaskLoopEvent,
    TaskLoop,
    ModelPlannerDraft,
    tuple[ModelPlannerStepBinding, ModelPlannerStepBinding],
]:
    source = TaskLoopSourceRef(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        turn_planner_run_id=run.run_id,
        turn_planner_run_digest=run.run_digest,
        adjudication_id=adjudication.adjudication_id,
        adjudication_digest=adjudication.adjudication_digest,
        turn_plan_binding_id=binding.binding_id,
        turn_plan_binding_digest=binding.binding_digest,
    )
    contract, draft_plan, expected_plan = _compiled(model_planner=True)
    source_node = offers[0].expected_plan.nodes[0]
    composite_node = expected_plan.nodes[0]
    step_bindings: list[ModelPlannerStepBinding] = []
    for ordinal, offer in enumerate(offers, start=1):
        value = "cats" if ordinal == 1 else "dogs"
        start = 0 if ordinal == 1 else 5
        mapping = ModelPlannerNodeMapping.build(
            source_node_id=source_node.node_id,
            source_local_key=source_node.local_key,
            source_node_spec_digest=source_node.node_spec_digest,
            composite_node_id=(
                composite_node.node_id if ordinal == 1 else "pnd_" + "9" * 64
            ),
            composite_local_key=f"s0{ordinal}_n01",
            composite_node_spec_digest=(
                composite_node.node_spec_digest if ordinal == 1 else "8" * 64
            ),
        )
        step_bindings.append(
            ModelPlannerStepBinding.build(
                source=source,
                ordinal=ordinal,
                offer=offer.ref,
                recipe=offer.trusted_recipe,
                policy_snapshot_digest=offer.policy_snapshot_digest,
                source_plan_id=offer.expected_plan.plan_id,
                source_plan_manifest_digest=offer.expected_plan.plan_manifest_digest,
                source_plan_binding_snapshot_digest=(
                    offer.expected_plan.binding_snapshot_digest
                ),
                budget=offer.budget,
                parameter_bindings=(
                    TurnPlanningParameterBinding.build(
                        offer_key=offer.offer_key,
                        parameter_name="query",
                        value=value,
                        source_start=start,
                        source_end=start + len(value),
                    ),
                ),
                node_mappings=(mapping,),
                created_at=NOW + timedelta(seconds=3),
            )
        )
    steps = (step_bindings[0], step_bindings[1])
    draft = ModelPlannerDraft.build(
        source=source,
        steps=tuple(item.ref for item in steps),
        task_contract=contract,
        draft_plan=draft_plan,
        expected_plan=expected_plan,
        created_at=NOW + timedelta(seconds=3),
    )
    observed_event = TaskLoopEvent.observe(
        source=source,
        created_at=NOW + timedelta(seconds=2),
    )
    observed = TaskLoop.observed(observed_event)
    plan_event = TaskLoopEvent.plan(
        observed=observed_event,
        draft=draft.ref,
        created_at=NOW + timedelta(seconds=4),
    )
    return observed_event, plan_event, observed.settle_plan(plan_event), draft, steps


def _loop_record(loop: TaskLoop) -> TaskLoopRecord:
    source = loop.source
    return TaskLoopRecord(
        loop_id=loop.loop_id,
        task_id=source.task_id,
        user_message_id=source.user_message_id,
        user_message_digest=source.user_message_digest,
        source_run_id=source.turn_planner_run_id,
        source_run_digest=source.turn_planner_run_digest,
        source_adjudication_id=source.adjudication_id,
        source_adjudication_digest=source.adjudication_digest,
        source_turn_plan_binding_id=source.turn_plan_binding_id,
        source_turn_plan_binding_digest=source.turn_plan_binding_digest,
        phase=loop.phase,
        status=loop.status,
        revision=loop.revision,
        event_count=loop.event_count,
        latest_event_id=loop.latest_event_id,
        latest_event_digest=loop.latest_event_digest,
        progress_digest=loop.progress_digest,
        active_draft_id=(loop.active_draft.draft_id if loop.active_draft else None),
        active_draft_record_digest=(
            loop.active_draft.draft_record_digest if loop.active_draft else None
        ),
        failure_manifest=(loop.failure.model_dump(mode="json") if loop.failure else None),
        failure_digest=(loop.failure.failure_digest if loop.failure else None),
        manifest=loop.model_dump(mode="json"),
        loop_digest=loop.loop_digest,
        created_at=loop.created_at,
        updated_at=loop.updated_at,
    )


def _event_record(event: TaskLoopEvent) -> TaskLoopEventRecord:
    return TaskLoopEventRecord(
        event_id=event.event_id,
        loop_id=event.loop_id,
        task_id=event.source.task_id,
        user_message_id=event.source.user_message_id,
        user_message_digest=event.source.user_message_digest,
        sequence=event.sequence,
        previous_event_digest=event.previous_event_digest,
        phase=event.phase,
        kind=event.kind,
        draft_id=event.draft.draft_id if event.draft else None,
        draft_record_digest=(event.draft.draft_record_digest if event.draft else None),
        failure_manifest=(event.failure.model_dump(mode="json") if event.failure else None),
        failure_digest=(event.failure.failure_digest if event.failure else None),
        progress_digest=event.progress_digest,
        manifest=event.model_dump(mode="json"),
        event_digest=event.event_digest,
        created_at=event.created_at,
    )


def _draft_record(draft: ModelPlannerDraft, loop_id: str) -> ModelPlannerDraftRecord:
    source = draft.source
    return ModelPlannerDraftRecord(
        draft_id=draft.draft_id,
        loop_id=loop_id,
        task_id=source.task_id,
        user_message_id=source.user_message_id,
        user_message_digest=source.user_message_digest,
        source_run_id=source.turn_planner_run_id,
        source_run_digest=source.turn_planner_run_digest,
        source_adjudication_id=source.adjudication_id,
        source_adjudication_digest=source.adjudication_digest,
        source_turn_plan_binding_id=source.turn_plan_binding_id,
        source_turn_plan_binding_digest=source.turn_plan_binding_digest,
        composer_version=draft.composer_version,
        step_count=draft.step_count,
        ordered_steps_manifest=[item.model_dump(mode="json") for item in draft.steps],
        step_set_digest=draft.step_set_digest,
        task_contract_manifest=draft.task_contract.model_dump(mode="json"),
        task_contract_digest=draft.task_contract_digest,
        draft_plan_manifest=draft.draft_plan.model_dump(mode="json"),
        draft_plan_digest=draft.draft_plan_digest,
        expected_plan_manifest=draft.expected_plan.model_dump(mode="json"),
        expected_plan_id=draft.expected_plan.plan_id,
        expected_plan_generation=draft.expected_plan.plan_generation,
        expected_plan_manifest_digest=draft.expected_plan_manifest_digest,
        expected_plan_binding_snapshot_digest=(
            draft.expected_plan.binding_snapshot_digest
        ),
        manifest=draft.model_dump(mode="json"),
        draft_record_digest=draft.draft_record_digest,
        created_at=draft.created_at,
    )


def _step_record(
    step: ModelPlannerStepBinding,
    *,
    draft_id: str,
    loop_id: str,
) -> ModelPlannerStepBindingRecord:
    return ModelPlannerStepBindingRecord(
        step_binding_id=step.step_binding_id,
        draft_id=draft_id,
        loop_id=loop_id,
        task_id=step.source.task_id,
        user_message_id=step.source.user_message_id,
        user_message_digest=step.source.user_message_digest,
        ordinal=step.ordinal,
        offer_id=step.offer.offer_id,
        offer_key=step.offer.offer_key,
        offer_digest=step.offer.offer_digest,
        recipe_id=step.recipe.route_id,
        recipe_version=step.recipe.route_version,
        recipe_digest=step.recipe.route_manifest_digest,
        policy_snapshot_digest=step.policy_snapshot_digest,
        source_plan_id=step.source_plan_id,
        source_plan_manifest_digest=step.source_plan_manifest_digest,
        source_plan_binding_snapshot_digest=step.source_plan_binding_snapshot_digest,
        budget_manifest=step.budget.model_dump(mode="json"),
        budget_digest=step.budget_digest,
        parameter_bindings_manifest=[
            item.model_dump(mode="json") for item in step.parameter_bindings
        ],
        parameter_bindings_digest=step.parameter_bindings_digest,
        node_mappings_manifest=[item.model_dump(mode="json") for item in step.node_mappings],
        node_mappings_digest=step.node_mappings_digest,
        manifest=step.model_dump(mode="json"),
        step_binding_digest=step.step_binding_digest,
        created_at=step.created_at,
    )


@pytest.mark.asyncio
async def test_model_planner_task_loop_records_round_trip_exact_v1_lineage(
    tmp_path: Path,
) -> None:
    path = tmp_path / "task-loop.db"
    database = Database(f"sqlite+aiosqlite:///{path.as_posix()}")
    await database.migrate()
    offers, run, adjudication, binding = _v1_lineage()
    observed_event, plan_event, loop, draft, steps = _task_loop_proofs(
        offers, run, adjudication, binding
    )
    try:
        async with database.session_factory() as session:
            session.add_all(
                [
                    ConversationRecord(
                        conversation_id=CONVERSATION_ID,
                        title="Stage 112 task loop",
                        created_at=NOW,
                    ),
                    TaskRecord(
                        task_id=TASK_ID,
                        conversation_id=CONVERSATION_ID,
                        goal="Look up cats and dogs",
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
                    content=MESSAGE,
                    content_ref=None,
                    classification="internal",
                    status="active",
                    message_digest=MESSAGE_DIGEST,
                    created_at=NOW,
                    deleted_at=None,
                )
            )
            await session.commit()
            session.add_all([*(_offer_record(item) for item in offers), _run_record(run)])
            await session.commit()
            session.add(
                TurnPlannerAdjudicationRecord(
                    adjudication_id=adjudication.adjudication_id,
                    task_id=adjudication.task_id,
                    user_message_id=adjudication.user_message_id,
                    user_message_digest=adjudication.user_message_digest,
                    run_id=adjudication.run_id,
                    run_digest=adjudication.run_digest,
                    outcome=adjudication.outcome,
                    selected_offer_count=len(adjudication.selected_offers),
                    parameter_bindings_manifest=[
                        item.model_dump(mode="json")
                        for item in adjudication.parameter_bindings
                    ],
                    parameter_bindings_digest=adjudication.parameter_bindings_digest,
                    proposal_digest=adjudication.proposal_digest,
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

            observed_loop = TaskLoop.observed(observed_event)
            stored_loop = _loop_record(observed_loop)
            session.add(stored_loop)
            await session.flush()
            session.add(_event_record(observed_event))
            await session.flush()
            session.add(_draft_record(draft, loop.loop_id))
            await session.flush()
            session.add_all(
                [
                    _step_record(
                        item,
                        draft_id=draft.draft_id,
                        loop_id=loop.loop_id,
                    )
                    for item in steps
                ]
            )
            await session.flush()
            session.add(_event_record(plan_event))
            await session.flush()
            stored_loop.phase = loop.phase
            stored_loop.status = loop.status
            stored_loop.revision = loop.revision
            stored_loop.event_count = loop.event_count
            stored_loop.latest_event_id = loop.latest_event_id
            stored_loop.latest_event_digest = loop.latest_event_digest
            stored_loop.progress_digest = loop.progress_digest
            stored_loop.active_draft_id = draft.draft_id
            stored_loop.active_draft_record_digest = draft.draft_record_digest
            stored_loop.manifest = loop.model_dump(mode="json")
            stored_loop.loop_digest = loop.loop_digest
            stored_loop.updated_at = loop.updated_at
            await session.commit()

            persisted_loop = await session.get(TaskLoopRecord, loop.loop_id)
            persisted_draft = await session.get(ModelPlannerDraftRecord, draft.draft_id)
            persisted_events = tuple(
                (
                    await session.scalars(
                        select(TaskLoopEventRecord)
                        .where(TaskLoopEventRecord.loop_id == loop.loop_id)
                        .order_by(TaskLoopEventRecord.sequence)
                    )
                ).all()
            )
            persisted_steps = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerStepBindingRecord)
                        .where(ModelPlannerStepBindingRecord.draft_id == draft.draft_id)
                        .order_by(ModelPlannerStepBindingRecord.ordinal)
                    )
                ).all()
            )
            assert persisted_loop is not None
            assert persisted_draft is not None
            assert TaskLoop.model_validate(persisted_loop.manifest) == loop
            assert ModelPlannerDraft.model_validate(persisted_draft.manifest) == draft
            assert tuple(
                TaskLoopEvent.model_validate(item.manifest)
                for item in persisted_events
            ) == (observed_event, plan_event)
            assert tuple(
                ModelPlannerStepBinding.model_validate(item.manifest)
                for item in persisted_steps
            ) == steps

            persisted_steps[0].offer_digest = "0" * 64
            with pytest.raises(IntegrityError):
                await session.commit()
            await session.rollback()
    finally:
        await database.dispose()
