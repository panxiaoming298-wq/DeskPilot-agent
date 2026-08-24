import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.model_planner_composer import (
    ModelPlannerComposer,
    ModelPlannerComposition,
    ModelPlannerOfferRejectedError,
    RevalidatedOfferStep,
)
from deskpilot.application.multi_step_plan_runtime import MultiStepPlanRuntime
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningNotFoundError,
)
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.turn_planner_runtime import TurnPlannerRuntime
from deskpilot.application.turn_router import CLASSIFIER_VERSION
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.task_loop import TaskLoopWorkbenchRead
from deskpilot.domain.task_workbench import (
    TurnRouteDecision,
    TurnRouteRead,
    TurnRouteStatus,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    ConversationRecord,
    ModelPlannerDraftRecord,
    ModelPlannerStepBindingRecord,
    TaskExecutionRunRecord,
    TaskLoopEventRecord,
    TaskLoopRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    TurnRouteRecord,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, 12, 0, tzinfo=UTC)
DecisionFactory = Callable[[ModelRequest], dict[str, Any]]


class ScriptedTurnPlannerProvider(FakeModelProvider):
    """Return one selected Offer set and fail if the Planner is replayed."""

    def __init__(self, decisions: list[DecisionFactory]) -> None:
        super().__init__(provider_id="planner-local")
        self.decisions = decisions
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if not self.decisions:
            raise AssertionError("Scripted Turn Planner Provider was replayed")
        decision = self.decisions.pop(0)
        response = await super().complete(request)
        return response.model_copy(update={"structured_output": decision(request)})


class RejectingComposer(ModelPlannerComposer):
    """Count composition attempts while deterministically rejecting the first one."""

    def __init__(self) -> None:
        self.calls = 0

    def compose(
        self,
        task_id: str,
        steps: tuple[RevalidatedOfferStep, ...],
    ) -> ModelPlannerComposition:
        del task_id, steps
        self.calls += 1
        raise ModelPlannerOfferRejectedError("scripted server Offer rejection")


def _offer_key_for(request: ModelRequest, parameter_name: str) -> str:
    payload = json.loads(request.messages[1].content)
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if any(spec["parameter_name"] == parameter_name for spec in item["parameter_specs"])
    ]
    assert len(matches) == 1
    return str(matches[0])


def _select_two_offers(request: ModelRequest) -> dict[str, Any]:
    return {
        "schema_version": "deskpilot.turn-planner-decision.v1",
        "kind": "propose_steps",
        "steps": [
            {
                "offer_key": _offer_key_for(request, "query"),
                "parameters": [{"name": "query", "value": "cats"}],
            },
            {
                "offer_key": _offer_key_for(request, "text"),
                "parameters": [{"name": "text", "value": "cats stats"}],
            },
        ],
    }


def _unsupported_route(
    *,
    task_id: str,
    conversation_id: str,
    message_id: str,
    message_digest: str,
) -> TurnRouteRead:
    parameters: dict[str, str] = {}
    reason_code = "UNSUPPORTED"
    candidate_digest = sha256_digest(
        {
            "classifier_version": CLASSIFIER_VERSION,
            "message_digest": message_digest,
            "decision": TurnRouteDecision.UNSUPPORTED.value,
            "route_id": None,
            "parameters": parameters,
            "reason_code": reason_code,
        }
    )
    return TurnRouteRead(
        task_id=task_id,
        conversation_id=conversation_id,
        user_message_id=message_id,
        decision=TurnRouteDecision.UNSUPPORTED,
        route_id=None,
        route_version=None,
        route_manifest_digest=None,
        candidate_digest=candidate_digest,
        parameter_digest=sha256_digest(parameters),
        reason_code=reason_code,
        status=TurnRouteStatus.NOT_APPLICABLE,
        revision=1,
        created_at=NOW,
        updated_at=NOW,
    )


async def _seed_turn(
    database: Database,
    *,
    suffix: str,
) -> tuple[str, TurnRouteRead]:
    task_id = f"tsk_{suffix * 32}"
    conversation_id = f"cnv_{suffix * 32}"
    message_id = f"msg_{suffix * 32}"
    message = "cats stats"
    message_material = {
        "message_id": message_id,
        "conversation_id": conversation_id,
        "task_id": task_id,
        "role": "user",
        "content": message,
        "content_ref": None,
        "classification": "internal",
        "created_at": NOW,
    }
    message_digest = sha256_digest(message_material)
    fallback = _unsupported_route(
        task_id=task_id,
        conversation_id=conversation_id,
        message_id=message_id,
        message_digest=message_digest,
    )
    async with database.session() as session, session.begin():
        session.add_all(
            [
                ConversationRecord(
                    conversation_id=conversation_id,
                    title="Multi-step Plan runtime test",
                    created_at=NOW,
                ),
                TaskRecord(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    goal=message,
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
        await session.flush()
        session.add(
            ConversationMessageRecord(
                message_id=message_id,
                conversation_id=conversation_id,
                task_id=task_id,
                role="user",
                content=message,
                content_ref=None,
                classification="internal",
                status="active",
                message_digest=message_digest,
                created_at=NOW,
                deleted_at=None,
            )
        )
        await session.flush()
        session.add(
            TurnRouteRecord(
                task_id=task_id,
                conversation_id=conversation_id,
                user_message_id=message_id,
                decision=fallback.decision.value,
                route_id=None,
                route_version=None,
                route_manifest_digest=None,
                candidate_digest=fallback.candidate_digest,
                parameters={},
                parameter_digest=fallback.parameter_digest,
                resolved_from_task_id=None,
                resolution_rule=None,
                resolution_digest=None,
                turn_planning_adjudication_id=None,
                turn_plan_binding_id=None,
                turn_plan_binding_digest=None,
                turn_planning_provenance_digest=None,
                reason_code=fallback.reason_code,
                status=fallback.status.value,
                result_manifest=None,
                result_digest=None,
                error_code=None,
                revision=1,
                created_at=NOW,
                updated_at=NOW,
            )
        )
    return task_id, fallback


def _runtimes(
    database: Database,
    provider: ScriptedTurnPlannerProvider,
    *,
    composer: ModelPlannerComposer | None = None,
) -> tuple[
    TurnPlannerRuntime,
    PlanCompilationService,
    MultiStepPlanRuntime,
    ModelPlannerComposer,
]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (provider.descriptor,))
    gateway = ModelGateway(
        default_provider_id=provider.descriptor.provider_id,
        policy=ModelGatewayPolicy(
            provider_pricing=(ModelProviderPricing(provider_id=provider.descriptor.provider_id),),
            circuit_failure_threshold=1,
        ),
    )
    gateway.register(provider)
    compiler = PlanCompiler(agents, tools, capabilities)
    planning = PlanCompilationService(database, compiler)
    planner = TurnPlannerRuntime(
        database,
        agents,
        gateway,
        capabilities,
        planning,
        provider_hint=provider.descriptor.provider_id,
        clock=lambda: NOW,
    )
    resolved_composer = composer or ModelPlannerComposer(compiler, capabilities)
    task_loop = MultiStepPlanRuntime(
        database,
        planner,
        resolved_composer,
        clock=lambda: NOW,
    )
    return planner, planning, task_loop, resolved_composer


async def _defer_two_offers(
    database: Database,
    planner: TurnPlannerRuntime,
    provider: ScriptedTurnPlannerProvider,
    *,
    suffix: str,
) -> tuple[str, TurnRouteRead]:
    task_id, fallback = await _seed_turn(database, suffix=suffix)
    await planner.prepare(
        task_id,
        fallback.user_message_id,
        fallback,
        frozenset({"knowledge_lookup", "mcp_text_metrics"}),
    )
    completed = await planner.interpret(task_id)
    assert completed.run.status == "succeeded"
    assert completed.adjudication is not None
    assert completed.adjudication.outcome == "multi_step_deferred"
    assert completed.binding is not None
    assert completed.binding.status == "multi_step_deferred"
    assert provider.calls == 1
    return task_id, fallback


async def _task_loop_record_counts(
    database: Database,
    task_id: str,
) -> tuple[int, int, int, int]:
    async with database.session() as session:
        counts = []
        for record_type in (
            TaskLoopRecord,
            TaskLoopEventRecord,
            ModelPlannerDraftRecord,
            ModelPlannerStepBindingRecord,
        ):
            counts.append(
                int(
                    await session.scalar(
                        select(func.count())
                        .select_from(record_type)
                        .where(record_type.task_id == task_id)
                    )
                    or 0
                )
            )
    return counts[0], counts[1], counts[2], counts[3]


@pytest.mark.asyncio
async def test_plan_is_idempotent_without_replaying_provider_or_activating_execution(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'idempotent.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, planning, task_loop, _composer = _runtimes(database, provider)
    try:
        task_id, fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="1",
        )

        first = await task_loop.plan(task_id)
        repeated = await task_loop.plan(task_id)
        bundle = await task_loop.get_bundle(task_id)

        assert first == repeated
        assert first.status == "planned"
        assert first.phase == "plan"
        assert first.revision == 2
        assert first.event_count == 2
        assert bundle is not None
        assert bundle.loop == first
        assert tuple(event.kind for event in bundle.events) == ("observed", "plan_bound")
        assert bundle.draft is not None
        assert bundle.draft.draft_plan.producer.kind == "model_planner"
        assert bundle.draft.draft_plan.producer.producer_ref == ("deskpilot.offer-composer.v1")
        assert len(bundle.steps) == 2
        assert tuple(step.ordinal for step in bundle.steps) == (1, 2)
        assert await _task_loop_record_counts(database, task_id) == (1, 2, 1, 2)
        assert provider.calls == 1
        with pytest.raises(PlanningNotFoundError):
            await planning.get_state(task_id)

        async with database.session() as session:
            route = await session.get(TurnRouteRecord, task_id)
            planning_state_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskPlanningStateRecord)
                    .where(TaskPlanningStateRecord.task_id == task_id)
                )
                or 0
            )
            plan_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskPlanGenerationRecord)
                    .where(TaskPlanGenerationRecord.task_id == task_id)
                )
                or 0
            )
            run_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionRunRecord)
                    .where(TaskExecutionRunRecord.task_id == task_id)
                )
                or 0
            )
        assert route is not None
        assert route.candidate_digest == fallback.candidate_digest
        assert route.decision == TurnRouteDecision.UNSUPPORTED.value
        assert route.route_id is None
        assert route.route_version is None
        assert route.turn_planning_adjudication_id is None
        assert route.turn_plan_binding_id is None
        assert route.turn_plan_binding_digest is None
        assert route.turn_planning_provenance_digest is None
        assert route.revision == 1
        assert planning_state_count == 0
        assert plan_count == 0
        assert run_count == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_plan_calls_converge_on_one_persistent_bundle(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'concurrent.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, _planning, first_runtime, composer = _runtimes(database, provider)
    second_runtime = MultiStepPlanRuntime(
        database,
        planner,
        composer,
        clock=lambda: NOW,
    )
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="2",
        )

        first, second = await asyncio.gather(
            first_runtime.plan(task_id),
            second_runtime.plan(task_id),
        )

        assert first == second
        assert first.status == "planned"
        assert await _task_loop_record_counts(database, task_id) == (1, 2, 1, 2)
        assert provider.calls == 1
        bundle = await second_runtime.get_bundle(task_id)
        assert bundle is not None
        assert bundle.loop == first
        assert bundle.draft is not None
        assert len(bundle.events) == 2
        assert len(bundle.steps) == 2
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_composer_rejection_is_terminal_and_never_replayed_or_recomposed(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rejected.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    rejecting = RejectingComposer()
    planner, _planning, task_loop, _composer = _runtimes(
        database,
        provider,
        composer=rejecting,
    )
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="3",
        )

        failed = await task_loop.plan(task_id)
        repeated = await task_loop.plan(task_id)
        repeated_again = await task_loop.plan(task_id)
        bundle = await task_loop.get_bundle(task_id)

        assert failed == repeated == repeated_again
        assert failed.status == "failed"
        assert failed.phase == "plan"
        assert failed.failure is not None
        assert failed.failure.error_code == "MULTI_STEP_OFFER_REJECTED"
        assert failed.failure.reason_code == "MULTI_STEP_OFFER_REJECTED"
        assert failed.failure.retry_policy == "never_automatic"
        assert failed.active_draft is None
        assert bundle is not None
        assert bundle.loop == failed
        assert tuple(event.kind for event in bundle.events) == ("observed", "plan_failed")
        assert bundle.draft is None
        assert bundle.steps == ()
        assert provider.calls == 1
        assert rejecting.calls == 1
        assert task_id not in await task_loop.recoverable_task_ids()
        assert await _task_loop_record_counts(database, task_id) == (1, 2, 0, 0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_public_task_loop_projection_contains_no_inputs_offers_contracts_or_plans(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'public-projection.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, _planning, task_loop, _composer = _runtimes(database, provider)
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="4",
        )
        planned = await task_loop.plan(task_id)
        payload = TaskLoopWorkbenchRead.from_internal(planned).model_dump(mode="json")

        assert set(payload) == {
            "schema_version",
            "loop_id",
            "phase",
            "status",
            "revision",
            "event_count",
            "step_count",
            "source_turn_plan_binding_digest",
            "draft_record_digest",
            "expected_plan_manifest_digest",
            "progress_digest",
            "failure",
            "recoverable",
            "updated_at",
            "projection_digest",
        }
        assert payload["status"] == "planned"
        assert payload["step_count"] == 2
        assert payload["failure"] is None

        private_keys = {
            "source",
            "steps",
            "task_contract",
            "draft_plan",
            "expected_plan",
            "parameter_bindings",
            "node_mappings",
            "parameters",
            "parameter_summary",
            "offer_key",
            "offer_id",
            "offer_digest",
            "user_message_id",
            "user_message_digest",
            "provider",
            "planner_agent",
            "execution_agents",
        }

        def assert_sanitized(value: object) -> None:
            if isinstance(value, dict):
                assert private_keys.isdisjoint(value)
                for child in value.values():
                    assert_sanitized(child)
            elif isinstance(value, list):
                for child in value:
                    assert_sanitized(child)

        assert_sanitized(payload)
        encoded = json.dumps(payload, sort_keys=True)
        assert "cats" not in encoded
        assert "ofk_" not in encoded
        assert "deskpilot.task-contract" not in encoded
        assert "deskpilot.draft-plan" not in encoded
        assert "deskpilot.executable-plan" not in encoded
        assert provider.calls == 1
    finally:
        await database.dispose()
