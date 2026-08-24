import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event, func, select

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.model_gateway import ModelGateway, ModelTimeoutError
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningNotFoundError,
    PlanningVersionConflictError,
)
from deskpilot.application.plan_compiler import (
    PlanCompiler,
    knowledge_lookup_contract,
    knowledge_lookup_draft,
    mcp_text_metrics_contract,
    mcp_text_metrics_draft,
)
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog
from deskpilot.application.turn_planner_runtime import (
    TurnPlannerNotEligibleError,
    TurnPlannerProofRejectedError,
    TurnPlannerRuntime,
)
from deskpilot.application.turn_router import (
    CLASSIFIER_VERSION,
    TurnRouteProofRejectedError,
    TurnRouter,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import ModelRequest, ModelResponse
from deskpilot.domain.model_routing import ModelGatewayPolicy, ModelProviderPricing
from deskpilot.domain.task_workbench import (
    TurnRouteDecision,
    TurnRouteRead,
    TurnRouteStatus,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ConversationMessageRecord,
    ConversationRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    TurnPlanBindingRecord,
    TurnPlannerAdjudicationRecord,
    TurnPlannerRunRecord,
    TurnRouteRecord,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=UTC)
DecisionFactory = Callable[[ModelRequest], dict[str, Any]]


class ScriptedTurnPlannerProvider(FakeModelProvider):
    def __init__(
        self,
        decisions: list[DecisionFactory | Exception],
        *,
        blocked: bool = False,
    ) -> None:
        super().__init__(provider_id="planner-local")
        self.decisions = decisions
        self.calls = 0
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        if not blocked:
            self.release.set()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.started.set()
        await self.release.wait()
        if not self.decisions:
            raise AssertionError("Scripted Turn Planner Provider was replayed")
        outcome = self.decisions.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = await super().complete(request)
        return response.model_copy(update={"structured_output": outcome(request)})


def _unsupported_route(
    *,
    task_id: str,
    conversation_id: str,
    message_id: str,
    message_digest: str,
    created_at: datetime,
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
        created_at=created_at,
        updated_at=created_at,
    )


async def _seed_turn(
    database: Database,
    *,
    suffix: str,
    message: str,
) -> tuple[str, TurnRouteRead]:
    task_id = f"tsk_{suffix * 32}"
    conversation_id = f"cnv_{suffix * 32}"
    message_id = f"msg_{suffix * 32}"
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
        created_at=NOW,
    )
    async with database.session() as session, session.begin():
        session.add_all(
            [
                ConversationRecord(
                    conversation_id=conversation_id,
                    title="Turn Planner test",
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


async def _assert_atomic_lineage_read(
    runtime: TurnPlannerRuntime,
    database: Database,
    task_id: str,
) -> None:
    statements: list[str] = []

    def capture(
        _connection: object,
        _cursor: object,
        statement: str,
        _parameters: object,
        _context: object,
        _executemany: bool,
    ) -> None:
        lowered = statement.casefold()
        if any(
            table in lowered
            for table in (
                "turn_planner_runs",
                "turn_planner_adjudications",
                "turn_plan_bindings",
            )
        ):
            statements.append(lowered)

    event.listen(database.engine.sync_engine, "before_cursor_execute", capture)
    try:
        assert await runtime.get(task_id) is not None
    finally:
        event.remove(database.engine.sync_engine, "before_cursor_execute", capture)
    assert len(statements) == 1
    assert "left outer join turn_planner_adjudications" in statements[0]
    assert "left outer join turn_plan_bindings" in statements[0]


def _runtime(
    database: Database,
    provider: ScriptedTurnPlannerProvider,
) -> tuple[TurnPlannerRuntime, PlanCompilationService]:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (provider.descriptor,))
    gateway = ModelGateway(
        default_provider_id=provider.descriptor.provider_id,
        policy=ModelGatewayPolicy(
            provider_pricing=(
                ModelProviderPricing(provider_id=provider.descriptor.provider_id),
            ),
            circuit_failure_threshold=1,
        ),
    )
    gateway.register(provider)
    planning = PlanCompilationService(
        database,
        PlanCompiler(agents, tools, capabilities),
    )
    return (
        TurnPlannerRuntime(
            database,
            agents,
            gateway,
            capabilities,
            planning,
            provider_hint=provider.descriptor.provider_id,
            clock=lambda: NOW,
        ),
        planning,
    )


def _offer_key_for(request: ModelRequest, parameter_name: str) -> str:
    payload = json.loads(request.messages[1].content)
    matches = [
        item["offer"]["offer_key"]
        for item in payload["offers"]
        if any(
            spec["parameter_name"] == parameter_name
            for spec in item["parameter_specs"]
        )
    ]
    assert len(matches) == 1
    return str(matches[0])


@pytest.mark.asyncio
async def test_turn_planner_single_step_binds_once_and_preserves_verbatim_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def select_knowledge(request: ModelRequest) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": '"cats"'}],
                }
            ],
        }

    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'single.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([select_knowledge])
    runtime, planning = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="1",
            message='please use "cats"',
        )
        prepared = await runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        assert prepared.run.status == "prepared"
        assert provider.calls == 0

        async def reject_legacy_recovery(_task_id: str) -> None:
            raise AssertionError("fresh single-step result used legacy recovery")

        monkeypatch.setattr(
            runtime,
            "_recover_single_step_binding",
            reject_legacy_recovery,
        )
        completed = await runtime.interpret(task_id)
        repeated = await runtime.interpret(task_id)
        bound = await runtime.get_bound_route(task_id)

        assert completed == repeated
        assert completed.adjudication is not None
        assert completed.adjudication.outcome == "single_step"
        assert completed.binding is not None
        assert completed.binding.status == "bound"
        assert bound is not None
        assert bound.route_id == "knowledge_lookup"
        assert bound.parameters == {"query": "cats"}
        assert provider.calls == 1
        state = await planning.get_state(task_id)
        assert state.active_plan_generation == 1
        async with database.session() as session:
            route = await session.get(TurnRouteRecord, task_id)
            count = await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == task_id
                )
            )
            assert route is not None
            assert route.route_version == "2"
            assert route.parameters == {"query": "cats"}
            assert route.turn_plan_binding_digest == completed.binding.binding_digest
            assert route.turn_planning_provenance_digest is not None
            assert count == 1
            route.route_version = "1"
            route.route_manifest_digest = RouteRecipeCatalog.digest(
                "knowledge_lookup",
                "1",
            )
            with pytest.raises(
                TurnRouteProofRejectedError,
                match="reservation anchor",
            ):
                object.__new__(TurnRouter)._validate_record(
                    route,
                    completed.user_message_digest,
                )
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_turn_planner_single_step_finalize_failure_rolls_back_all_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def select_knowledge(request: ModelRequest) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": "cats"}],
                }
            ],
        }

    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'atomic-failure.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([select_knowledge])
    runtime, _ = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="a",
            message="please use cats",
        )
        await runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        original_binding_record = runtime._binding_record

        def reject_bound_binding(binding: Any) -> TurnPlanBindingRecord:
            if binding.status == "bound":
                raise TurnPlannerProofRejectedError(
                    "injected failure after tentative Plan activation"
                )
            return original_binding_record(binding)

        monkeypatch.setattr(runtime, "_binding_record", reject_bound_binding)
        failed = await runtime.interpret(task_id)
        repeated = await runtime.interpret(task_id)

        assert repeated == failed
        assert failed.run.status == "failed"
        assert failed.run.failure is not None
        assert failed.run.failure.error_code == "PLANNER_BINDING_REJECTED"
        assert failed.adjudication is not None
        assert failed.adjudication.outcome == "deterministic_fallback"
        assert failed.binding is not None
        assert failed.binding.status == "not_applicable"
        assert provider.calls == 1
        async with database.session() as session:
            plan_count = await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == task_id
                )
            )
            planning_state = await session.get(TaskPlanningStateRecord, task_id)
            adjudication_count = await session.scalar(
                select(func.count())
                .select_from(TurnPlannerAdjudicationRecord)
                .where(TurnPlannerAdjudicationRecord.task_id == task_id)
            )
            bound_count = await session.scalar(
                select(func.count())
                .select_from(TurnPlanBindingRecord)
                .where(
                    TurnPlanBindingRecord.task_id == task_id,
                    TurnPlanBindingRecord.status == "bound",
                )
            )
            route = await session.get(TurnRouteRecord, task_id)
            assert plan_count == 0
            assert planning_state is None
            assert adjudication_count == 1
            assert bound_count == 0
            assert route is not None
            assert route.decision == TurnRouteDecision.UNSUPPORTED.value
            assert route.candidate_digest == fallback.candidate_digest
            assert route.turn_planning_adjudication_id is None
            assert route.turn_plan_binding_id is None
            assert route.turn_planning_provenance_digest is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_turn_planner_single_step_route_drift_fails_without_model_authority(
    tmp_path: Path,
) -> None:
    def select_knowledge(request: ModelRequest) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": "cats"}],
                }
            ],
        }

    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'route-drift.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([select_knowledge])
    runtime, _ = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="b",
            message="please use cats",
        )
        await runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        async with database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, task_id)
            assert route is not None
            route.route_version = "tampered"

        failed = await runtime.interpret(task_id)
        repeated = await runtime.interpret(task_id)

        assert repeated == failed
        assert failed.run.status == "failed"
        assert failed.run.failure is not None
        assert failed.run.failure.error_code == "PLANNER_BINDING_REJECTED"
        assert provider.calls == 1
        async with database.session() as session:
            plan_count = await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == task_id
                )
            )
            route = await session.get(TurnRouteRecord, task_id)
            assert plan_count == 0
            assert route is not None
            assert route.decision == TurnRouteDecision.UNSUPPORTED.value
            assert route.route_version == "tampered"
            assert route.turn_planning_adjudication_id is None
            assert route.turn_plan_binding_id is None
            assert route.turn_planning_provenance_digest is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_turn_planner_recovers_legacy_single_step_missing_binding(
    tmp_path: Path,
) -> None:
    def select_knowledge(request: ModelRequest) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": "cats"}],
                }
            ],
        }

    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'legacy-binding.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([select_knowledge])
    runtime, _ = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="c",
            message="please use cats",
        )
        await runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        completed = await runtime.interpret(task_id)
        assert completed.binding is not None

        # Emulate a row set written by the pre-atomic implementation: the
        # succeeded Run and single-step adjudication exist, while the v2 Route
        # and Binding do not.  The exact generation-1 Plan may already exist.
        async with database.session() as session, session.begin():
            route = await session.get(TurnRouteRecord, task_id)
            assert route is not None
            route.decision = fallback.decision.value
            route.route_id = fallback.route_id
            route.route_version = fallback.route_version
            route.route_manifest_digest = fallback.route_manifest_digest
            route.candidate_digest = fallback.candidate_digest
            route.parameters = {}
            route.parameter_digest = fallback.parameter_digest
            route.turn_planning_adjudication_id = None
            route.turn_plan_binding_id = None
            route.turn_plan_binding_digest = None
            route.turn_planning_provenance_digest = None
            route.reason_code = fallback.reason_code
            route.status = fallback.status.value
            route.result_manifest = None
            route.result_digest = None
            route.error_code = None
            route.revision += 1
            await session.flush()
            binding = await session.get(
                TurnPlanBindingRecord,
                completed.binding.binding_id,
            )
            assert binding is not None
            await session.delete(binding)

        assert await runtime.get(task_id) is None
        assert task_id in await runtime.recoverable_task_ids()
        recovered = await runtime.interpret(task_id)
        repeated = await runtime.interpret(task_id)

        assert recovered == repeated
        assert recovered.binding is not None
        assert recovered.binding.status == "bound"
        assert provider.calls == 1
        async with database.session() as session:
            plan_count = await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == task_id
                )
            )
            route = await session.get(TurnRouteRecord, task_id)
            assert plan_count == 1
            assert route is not None
            assert route.decision == TurnRouteDecision.ROUTED.value
            assert route.turn_plan_binding_id == recovered.binding.binding_id
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_turn_planner_defers_multi_step_and_never_changes_fallback_route(
    tmp_path: Path,
) -> None:
    def select_two(request: ModelRequest) -> dict[str, Any]:
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

    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'deferred.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([select_two])
    runtime, planning = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="2",
            message="cats stats",
        )
        await runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup", "mcp_text_metrics"}),
        )
        completed = await runtime.interpret(task_id)
        repeated = await runtime.interpret(task_id)

        assert completed == repeated
        assert completed.adjudication is not None
        assert completed.adjudication.outcome == "multi_step_deferred"
        assert completed.binding is not None
        assert completed.binding.status == "multi_step_deferred"
        assert completed.binding.reason_code == "MULTI_STEP_PLAN_DEFERRED"
        assert provider.calls == 1
        with pytest.raises(PlanningNotFoundError):
            await planning.get_state(task_id)
        async with database.session() as session:
            route = await session.get(TurnRouteRecord, task_id)
            assert route is not None
            assert route.candidate_digest == fallback.candidate_digest
            assert route.turn_planning_adjudication_id is None
            assert route.turn_plan_binding_id is None
            assert route.turn_plan_binding_digest is None
            assert route.turn_planning_provenance_digest is None
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_turn_planner_failure_and_cancel_are_terminal_and_never_replayed(
    tmp_path: Path,
) -> None:
    def invented_parameter(request: ModelRequest) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": "invented"}],
                }
            ],
        }

    first_database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'failure.db').as_posix()}"
    )
    await first_database.migrate()
    failed_provider = ScriptedTurnPlannerProvider([invented_parameter])
    failed_runtime, _ = _runtime(first_database, failed_provider)
    try:
        task_id, fallback = await _seed_turn(
            first_database,
            suffix="3",
            message="cats",
        )
        await failed_runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        failed = await failed_runtime.interpret(task_id)
        assert (await failed_runtime.interpret(task_id)) == failed
        assert failed.run.status == "failed"
        assert failed.run.failure is not None
        assert failed.run.failure.error_code == "PLANNER_SCHEMA_REJECTED"
        assert failed.binding is not None
        assert failed.binding.status == "not_applicable"
        assert failed_provider.calls == 1
        await _assert_atomic_lineage_read(
            failed_runtime,
            first_database,
            task_id,
        )
        async with first_database.session() as session, session.begin():
            binding = await session.get(
                TurnPlanBindingRecord,
                failed.binding.binding_id,
            )
            assert binding is not None
            await session.delete(binding)
        with pytest.raises(
            TurnPlannerProofRejectedError,
            match="lacks its Binding",
        ):
            await failed_runtime.get(task_id)
    finally:
        await first_database.dispose()

    second_database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'cancel.db').as_posix()}"
    )
    await second_database.migrate()
    blocked_provider = ScriptedTurnPlannerProvider(
        [
            lambda request: {
                "schema_version": "deskpilot.turn-planner-decision.v1",
                "kind": "unsupported",
            }
        ],
        blocked=True,
    )
    blocked_runtime, _ = _runtime(second_database, blocked_provider)
    try:
        task_id, fallback = await _seed_turn(
            second_database,
            suffix="4",
            message="cats",
        )
        await blocked_runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        worker = asyncio.create_task(blocked_runtime.interpret(task_id))
        await blocked_provider.started.wait()
        cancelled = await blocked_runtime.cancel(task_id)
        assert cancelled is not None
        blocked_provider.release.set()
        late = await worker

        assert cancelled.run.status == "cancelled"
        assert late.run.status == "cancelled"
        assert late.run.failure is not None
        assert late.run.failure.error_code == "PLANNER_CANCELLED"
        assert late.binding is not None
        assert late.binding.status == "not_applicable"
        assert blocked_provider.calls == 1
    finally:
        await second_database.dispose()

    circuit_database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'circuit.db').as_posix()}"
    )
    await circuit_database.migrate()
    circuit_provider = ScriptedTurnPlannerProvider(
        [ModelTimeoutError("open circuit", provider_id="planner-local")]
    )
    circuit_runtime, _ = _runtime(circuit_database, circuit_provider)
    try:
        first_task_id, first_fallback = await _seed_turn(
            circuit_database,
            suffix="7",
            message="cats",
        )
        await circuit_runtime.prepare(
            first_task_id,
            first_fallback.user_message_id,
            first_fallback,
            frozenset({"knowledge_lookup"}),
        )
        opened = await circuit_runtime.interpret(first_task_id)
        assert opened.run.status == "failed"
        assert opened.run.failure is not None
        assert opened.run.failure.error_code == "PLANNER_TIMEOUT"
        assert circuit_provider.calls == 1

        task_id, fallback = await _seed_turn(
            circuit_database,
            suffix="8",
            message="cats",
        )
        # Reservation selection ignores the already-open circuit, while the
        # live dispatch remains fail-closed and does not call the Provider.
        prepared = await circuit_runtime.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"knowledge_lookup"}),
        )
        failed = await circuit_runtime.interpret(task_id)
        assert prepared.run.status == "prepared"
        assert failed.run.status == "failed"
        assert failed.run.failure is not None
        assert failed.run.failure.error_code == "PLANNER_PROVIDER_UNAVAILABLE"
        assert (await circuit_runtime.interpret(task_id)) == failed
        assert circuit_provider.calls == 1
        async with circuit_database.session() as session:
            reservation_count = await session.scalar(
                select(func.count()).select_from(TurnPlannerRunRecord).where(
                    TurnPlannerRunRecord.task_id == task_id
                )
            )
            route = await session.get(TurnRouteRecord, task_id)
            assert reservation_count == 1
            assert route is not None
            assert route.turn_planner_run_id == failed.run.run_id
            assert (
                route.turn_planning_reservation_digest
                == failed.run.reservation_digest
            )
    finally:
        await circuit_database.dispose()


@pytest.mark.asyncio
async def test_activate_initial_once_is_idempotent_and_rejects_generation_drift(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'initial-once.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([])
    _, planning = _runtime(database, provider)
    try:
        task_id, _ = await _seed_turn(database, suffix="5", message="cats")
        capabilities = create_builtin_capability_catalog()
        contract = knowledge_lookup_contract(task_id, capabilities)
        draft = knowledge_lookup_draft(task_id)
        first, repeated = await asyncio.gather(
            planning.activate_initial_once(contract, draft),
            planning.activate_initial_once(contract, draft),
        )
        assert first == repeated
        assert first.plan.plan_generation == 1

        with pytest.raises(PlanningVersionConflictError):
            await planning.activate_initial_once(
                mcp_text_metrics_contract(task_id, capabilities),
                mcp_text_metrics_draft(task_id),
            )
        async with database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(TaskPlanGenerationRecord).where(
                    TaskPlanGenerationRecord.task_id == task_id
                )
            )
            state = await session.get(TaskPlanningStateRecord, task_id)
            assert count == 1
            assert state is not None
            assert state.active_plan_generation == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_deterministic_route_bypasses_planner_without_a_provider_call(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'bypass.db').as_posix()}"
    )
    await database.migrate()
    provider = ScriptedTurnPlannerProvider(
        [ModelTimeoutError("must not run", provider_id="planner-local")]
    )
    runtime, _ = _runtime(database, provider)
    try:
        task_id, fallback = await _seed_turn(
            database,
            suffix="6",
            message="cats",
        )
        routed = fallback.model_copy(
            update={
                "decision": TurnRouteDecision.ROUTED,
                "route_id": "knowledge_lookup",
                "route_version": "1",
                "route_manifest_digest": "0" * 64,
                "status": TurnRouteStatus.READY,
            }
        )
        with pytest.raises(TurnPlannerNotEligibleError):
            await runtime.prepare(
                task_id,
                fallback.user_message_id,
                routed,
                frozenset({"knowledge_lookup"}),
            )
        assert provider.calls == 0
    finally:
        await database.dispose()
