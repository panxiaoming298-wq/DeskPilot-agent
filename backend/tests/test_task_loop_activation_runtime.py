import asyncio
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy import func, select

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_executor_registry import (
    CapabilityExecutorRegistry,
)
from deskpilot.application.capability_input_binding_catalog import (
    CapabilityInputBindingCatalog,
)
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationConflictError,
    TaskLoopActivationProofRejectedError,
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.task_loop_execution import (
    ModelPlannerNodeBinding,
    TaskLoopExecution,
    TaskLoopExecutionEvent,
    TaskLoopNodeAttempt,
    TaskLoopVerifiedResult,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ModelPlannerNodeBindingRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopExecutionEventRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
)

sys.path.insert(0, str(Path(__file__).parent))

from test_multi_step_plan_runtime import (  # noqa: E402
    NOW,
    ScriptedTurnPlannerProvider,
    _defer_two_offers,
    _offer_key_for,
    _runtimes,
    _select_two_offers,
)
from test_turn_planner_runtime import _seed_turn as _seed_custom_turn  # noqa: E402


def _activation_runtime(
    database: Database,
    provider: ScriptedTurnPlannerProvider,
    *,
    executors: CapabilityExecutorRegistry | None = None,
) -> tuple[TaskLoopActivationRuntime, Any]:
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    if executors is None:
        executors = create_builtin_capability_executor_registry(
            planner._capabilities,  # noqa: SLF001 - shared exact test fixture
            knowledge=cast(Any, object()),
            mcp=cast(Any, object()),
        )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001 - shared exact test fixture
        planner._agents,  # noqa: SLF001 - shared exact test fixture
    )
    runtime = TaskLoopActivationRuntime(
        database,
        task_loops,
        planner,
        planning,
        execution,
        ModelPlannerNodeBinder(
            planner._agents,  # noqa: SLF001
            executors,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        ),
        clock=lambda: NOW,
    )
    return runtime, task_loops


async def _count(database: Database, record_type: type[Any], task_id: str) -> int:
    async with database.session() as session:
        statement = select(func.count()).select_from(record_type)
        task_column = getattr(record_type, "task_id", None)
        if task_column is not None:
            statement = statement.where(task_column == task_id)
        return int(await session.scalar(statement) or 0)


@pytest.mark.asyncio
async def test_activation_atomically_seals_plan_run_authority_and_event(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'activation.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    runtime, task_loops = _activation_runtime(database, provider)
    try:
        planner = task_loops._turn_planner  # noqa: SLF001 - shared fixture
        task_id, _fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="a",
        )
        loop = await task_loops.plan(task_id)
        bundle = await task_loops.get_bundle(task_id)
        assert loop.status == "planned"
        assert bundle is not None and bundle.draft is not None

        first = await runtime.activate(task_id)
        repeated = await runtime.activate(task_id)

        assert first == repeated
        assert first.status == "active"
        assert first.plan_generation == 1
        assert first.plan_manifest_digest == (bundle.draft.expected_plan_manifest_digest)
        assert first.event_count == 1
        assert first.node_binding_count == 2
        assert provider.calls == 1

        async with database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            plan = await session.get(TaskPlanGenerationRecord, (task_id, 1))
            run = await session.get(TaskExecutionRunRecord, first.run_id)
            events = tuple(
                (
                    await session.scalars(
                        select(TaskLoopExecutionEventRecord).where(
                            TaskLoopExecutionEventRecord.execution_id == first.execution_id
                        )
                    )
                ).all()
            )
            bindings = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerNodeBindingRecord)
                        .where(ModelPlannerNodeBindingRecord.execution_id == first.execution_id)
                        .order_by(ModelPlannerNodeBindingRecord.step_ordinal)
                    )
                ).all()
            )
            node_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.run_id == first.run_id)
                )
                or 0
            )
            edge_count = int(
                await session.scalar(
                    select(func.count())
                    .select_from(TaskExecutionEdgeRecord)
                    .where(TaskExecutionEdgeRecord.run_id == first.run_id)
                )
                or 0
            )
        assert state is not None and state.active_plan_generation == 1
        assert plan is not None and plan.plan_manifest_digest == first.plan_manifest_digest
        assert run is not None and run.plan_digest == first.plan_manifest_digest
        assert len(events) == 1 and events[0].kind == "activated"
        assert node_count == len(bundle.draft.expected_plan.nodes)
        assert edge_count >= 1
        assert len(bindings) == 2
        assert {item.step_ordinal for item in bindings} == {1, 2}
        assert all(
            item.effective_authority_manifest["authority_rule"]
            == "composite_intersection_source_step"
            for item in bindings
        )
        assert all(item.bound_input_manifest for item in bindings)
        assert all(
            item.runtime_eligibility_manifest["runtime_kind"] == "capability_executor"
            for item in bindings
        )
        assert await _count(database, TaskLoopExecutionRecord, task_id) == 1
        assert await _count(database, ModelPlannerNodeBindingRecord, task_id) == 2
        assert await _count(database, TaskLoopNodeAttemptRecord, task_id) == 0
        assert await _count(database, TaskLoopVerifiedResultRecord, task_id) == 0

        async with database.session() as session, session.begin():
            node = await session.scalar(
                select(TaskExecutionNodeRecord).where(
                    TaskExecutionNodeRecord.run_id == first.run_id
                )
            )
            assert node is not None
            node.node_spec_digest = "0" * 64
        with pytest.raises(TaskLoopActivationProofRejectedError):
            await runtime.get(task_id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_missing_exact_executor_rejects_before_plan_or_run_write(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'missing-executor.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    runtime, task_loops = _activation_runtime(
        database,
        provider,
        executors=CapabilityExecutorRegistry(),
    )
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            task_loops._turn_planner,  # noqa: SLF001 - shared fixture
            provider,
            suffix="b",
        )
        await task_loops.plan(task_id)

        with pytest.raises(TaskLoopActivationProofRejectedError):
            await runtime.activate(task_id)

        assert await _count(database, TaskPlanningStateRecord, task_id) == 0
        assert await _count(database, TaskPlanGenerationRecord, task_id) == 0
        assert await _count(database, TaskExecutionRunRecord, task_id) == 0
        assert await _count(database, TaskLoopExecutionRecord, task_id) == 0
        assert provider.calls == 1
    finally:
        await database.dispose()


def _attempt_record(attempt: TaskLoopNodeAttempt) -> TaskLoopNodeAttemptRecord:
    return TaskLoopNodeAttemptRecord(
        **attempt.model_dump(mode="python", exclude={"schema_version"}),
        manifest=attempt.model_dump(mode="json"),
    )


def _apply_attempt(
    record: TaskLoopNodeAttemptRecord,
    attempt: TaskLoopNodeAttempt,
) -> None:
    for name, value in attempt.model_dump(
        mode="python",
        exclude={"schema_version"},
    ).items():
        setattr(record, name, value)
    record.manifest = attempt.model_dump(mode="json")


def _build_candidate_attempt(
    *,
    execution: TaskLoopExecution,
    binding: ModelPlannerNodeBinding,
    candidate_label: str,
) -> TaskLoopNodeAttempt:
    candidate_digest = sha256_digest({"candidate": candidate_label})
    input_manifest = {
        "schema_version": "deskpilot.test-bound-input.v1",
        "node_binding_digest": binding.binding_digest,
    }
    context_manifest = {
        "schema_version": "deskpilot.test-execution-context.v1",
        "execution_id": execution.execution_id,
    }
    attempt_id_material = {
        "execution_id": execution.execution_id,
        "node_id": binding.composite_node_id,
        "attempt": 1,
    }
    values: dict[str, Any] = {
        "schema_version": "deskpilot.task-loop-node-attempt.v1",
        "attempt_id": f"tla_{sha256_digest(attempt_id_material)}",
        "execution_id": execution.execution_id,
        "node_binding_id": binding.node_binding_id,
        "run_id": execution.run_id,
        "node_id": binding.composite_node_id,
        "attempt": 1,
        "status": "awaiting_verification",
        "revision": 1,
        "claim_owner_id": None,
        "claim_fencing_token": 1,
        "claim_acquired_at": None,
        "claim_expires_at": None,
        "input_manifest": input_manifest,
        "input_digest": sha256_digest(input_manifest),
        "context_manifest": context_manifest,
        "context_digest": sha256_digest(context_manifest),
        "candidate_manifest": {
            "schema_version": "deskpilot.test-candidate.v1",
            "candidate_digest": candidate_digest,
        },
        "candidate_digest": candidate_digest,
        "candidate_recorded_at": NOW,
        "verification_manifest": None,
        "verification_digest": None,
        "verified_at": None,
        "receipt_manifest": None,
        "receipt_digest": None,
        "error_code": None,
        "error_digest": None,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return TaskLoopNodeAttempt(
        **values,
        attempt_digest=sha256_digest(values),
    )


def _verified_attempt_and_result(
    *,
    execution: TaskLoopExecution,
    binding: ModelPlannerNodeBinding,
    candidate: TaskLoopNodeAttempt,
) -> tuple[TaskLoopNodeAttempt, TaskLoopVerifiedResult]:
    verified_at = NOW + timedelta(seconds=1)
    verification_digest = sha256_digest(
        {"candidate_digest": candidate.candidate_digest, "status": "verified"}
    )
    attempt_values = candidate.model_dump(mode="python", exclude={"attempt_digest"})
    attempt_values.update(
        {
            "status": "verified",
            "revision": 2,
            "verification_manifest": {
                "schema_version": "deskpilot.test-verification.v1",
                "verification_digest": verification_digest,
            },
            "verification_digest": verification_digest,
            "verified_at": verified_at,
            "updated_at": verified_at,
        }
    )
    attempt = TaskLoopNodeAttempt(
        **attempt_values,
        attempt_digest=sha256_digest(attempt_values),
    )
    capability = binding.effective_authority.capability
    assert capability is not None
    result_kind = (
        CapabilityResultKind.KNOWLEDGE
        if capability.capability_id == "knowledge.local.v1"
        else CapabilityResultKind.MCP
    )
    output_digest = sha256_digest({"node_id": binding.composite_node_id, "status": "verified"})
    output_schema_digest = sha256_digest({"schema": result_kind.value})
    result_ref = VerifiedCapabilityResultRef.build(
        task_id=execution.task_id,
        run_id=execution.run_id,
        plan_generation=execution.plan_generation,
        producer_node_id=binding.composite_node_id,
        producer_attempt=attempt.attempt,
        capability=capability,
        result_kind=result_kind,
        result_schema_digest=output_schema_digest,
        result_digest=output_digest,
        verification_digest=verification_digest,
    )
    result_id_material = {
        "attempt_id": attempt.attempt_id,
        "result_ref_digest": result_ref.result_ref_digest,
    }
    result = TaskLoopVerifiedResult(
        result_ref_id=f"tlr_{sha256_digest(result_id_material)}",
        attempt_id=attempt.attempt_id,
        execution_id=execution.execution_id,
        node_binding_id=binding.node_binding_id,
        node_binding_digest=binding.binding_digest,
        run_id=execution.run_id,
        node_id=binding.composite_node_id,
        producer_kind="capability_executor",
        capability_manifest=capability.model_dump(mode="json"),
        capability_digest=sha256_digest(capability.model_dump(mode="json")),
        agent_binding_manifest=None,
        agent_binding_digest=None,
        executor_manifest_digest=(binding.runtime_eligibility.executor_manifest_digest),
        agent_result_proof_digest=None,
        input_binding_digest=attempt.input_digest,
        context_digest=attempt.context_digest,
        candidate_digest=attempt.candidate_digest,
        result_kind=result_kind.value,
        output_manifest={
            "schema_version": "deskpilot.test-output.v1",
            "result_digest": output_digest,
        },
        output_schema_digest=output_schema_digest,
        output_digest=output_digest,
        verification_manifest={
            "schema_version": "deskpilot.test-verification.v1",
            "verification_digest": verification_digest,
        },
        verification_digest=verification_digest,
        result_ref_manifest=result_ref.model_dump(mode="json"),
        result_ref_digest=result_ref.result_ref_digest,
        created_at=verified_at,
    )
    return attempt, result


def _record_keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_record_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_record_keys(item) for item in value), set())
    return set()


def _cancelled_execution(
    execution: TaskLoopExecution,
) -> tuple[TaskLoopExecution, TaskLoopExecutionEvent]:
    created_at = NOW + timedelta(seconds=2)
    event_values: dict[str, Any] = {
        "schema_version": "deskpilot.task-loop-execution-event.v1",
        "execution_id": execution.execution_id,
        "task_id": execution.task_id,
        "sequence": 2,
        "previous_event_digest": execution.latest_event_digest,
        "kind": "cancelled",
        "plan_manifest_digest": execution.plan_manifest_digest,
        "run_id": execution.run_id,
        "binding_set_digest": execution.binding_set_digest,
        "created_at": created_at,
    }
    event_id = f"txe_{sha256_digest(event_values)}"
    event_digest_values = {**event_values, "event_id": event_id}
    event = TaskLoopExecutionEvent(
        **event_digest_values,
        event_digest=sha256_digest(event_digest_values),
    )
    execution_values = execution.model_dump(
        mode="python",
        exclude={"execution_digest"},
    )
    execution_values.update(
        {
            "status": "cancelled",
            "revision": 2,
            "event_count": 2,
            "latest_event_id": event.event_id,
            "latest_event_digest": event.event_digest,
            "updated_at": created_at,
        }
    )
    return (
        TaskLoopExecution(
            **execution_values,
            execution_digest=sha256_digest(execution_values),
        ),
        event,
    )


def _apply_execution(
    record: TaskLoopExecutionRecord,
    execution: TaskLoopExecution,
) -> None:
    for name, value in execution.model_dump(
        mode="python",
        exclude={"schema_version"},
    ).items():
        if name != "created_at":
            setattr(record, name, value)
    record.manifest = execution.model_dump(mode="json")


class _DuplicateBindingBinder(ModelPlannerNodeBinder):
    def bind(self, *args: Any, **kwargs: Any) -> tuple[Any, ...]:
        bindings = super().bind(*args, **kwargs)
        return (bindings[0], bindings[0])


@pytest.mark.asyncio
async def test_binding_persistence_failure_rolls_back_plan_run_and_nodes(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rollback.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    planner, planning, task_loops, _composer = _runtimes(database, provider)
    executors = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        knowledge=cast(Any, object()),
        mcp=cast(Any, object()),
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001
        planner._agents,  # noqa: SLF001
    )
    runtime = TaskLoopActivationRuntime(
        database,
        task_loops,
        planner,
        planning,
        execution,
        _DuplicateBindingBinder(
            planner._agents,  # noqa: SLF001
            executors,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        ),
        clock=lambda: NOW,
    )
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            planner,
            provider,
            suffix="c",
        )
        await task_loops.plan(task_id)

        with pytest.raises(TaskLoopActivationConflictError):
            await runtime.activate(task_id)

        assert await _count(database, TaskPlanningStateRecord, task_id) == 0
        assert await _count(database, TaskPlanGenerationRecord, task_id) == 0
        assert await _count(database, TaskExecutionRunRecord, task_id) == 0
        assert await _count(database, TaskExecutionNodeRecord, task_id) == 0
        assert await _count(database, TaskLoopExecutionRecord, task_id) == 0
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_concurrent_activation_converges_without_second_provider_call(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'concurrent-activation.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    first_runtime, task_loops = _activation_runtime(database, provider)
    second_runtime, _second_loops = _activation_runtime(database, provider)
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            task_loops._turn_planner,  # noqa: SLF001
            provider,
            suffix="d",
        )
        await task_loops.plan(task_id)

        first, second = await asyncio.gather(
            first_runtime.activate(task_id),
            second_runtime.activate(task_id),
        )

        assert first == second
        assert await _count(database, TaskPlanningStateRecord, task_id) == 1
        assert await _count(database, TaskPlanGenerationRecord, task_id) == 1
        assert await _count(database, TaskExecutionRunRecord, task_id) == 1
        assert await _count(database, TaskLoopExecutionRecord, task_id) == 1
        assert await _count(database, ModelPlannerNodeBindingRecord, task_id) == 2
        assert provider.calls == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_read_projection_requires_verified_result_ref_to_unlock_dependencies(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'read-projection.db').as_posix()}")
    await database.migrate()
    provider = ScriptedTurnPlannerProvider([_select_two_offers])
    runtime, task_loops = _activation_runtime(database, provider)
    try:
        task_id, _fallback = await _defer_two_offers(
            database,
            task_loops._turn_planner,  # noqa: SLF001
            provider,
            suffix="e",
        )
        await task_loops.plan(task_id)
        execution = await runtime.activate(task_id)
        async with database.session() as session, session.begin():
            nodes = tuple(
                (
                    await session.scalars(
                        select(TaskExecutionNodeRecord)
                        .where(TaskExecutionNodeRecord.run_id == execution.run_id)
                        .order_by(TaskExecutionNodeRecord.local_key)
                    )
                ).all()
            )
            root = next(item for item in nodes if not item.depends_on)
            dependent = next(item for item in nodes if root.node_id in item.depends_on)
            binding_record = await session.scalar(
                select(ModelPlannerNodeBindingRecord).where(
                    ModelPlannerNodeBindingRecord.composite_node_id == root.node_id
                )
            )
            assert binding_record is not None
            binding = ModelPlannerNodeBinding.model_validate(binding_record.manifest)
            candidate = _build_candidate_attempt(
                execution=execution,
                binding=binding,
                candidate_label="opaque candidate payload",
            )
            root.status = "awaiting_verification"
            root.revision += 1
            root.attempt_count = 1
            root.updated_at = NOW
            session.add(_attempt_record(candidate))

        candidate_read = await runtime.get(task_id)
        assert candidate_read is not None
        assert candidate_read.phase == "verify"
        candidate_root = next(item for item in candidate_read.nodes if item.node_id == root.node_id)
        candidate_dependent = next(
            item for item in candidate_read.nodes if item.node_id == dependent.node_id
        )
        assert candidate_root.candidate_present
        assert not candidate_root.verified_result_present
        assert not candidate_dependent.dependencies_verified
        assert candidate_dependent.verified_dependency_count == 0

        workbench = candidate_read.workbench
        public = workbench.model_dump(mode="json")
        forbidden_keys = {
            "bound_input_manifest",
            "parameter_bindings",
            "offer_key",
            "offer_id",
            "payload",
            "effective_authority",
            "resource_scopes",
            "input_manifest",
            "context_manifest",
            "candidate_manifest",
            "verification_manifest",
            "result_ref_manifest",
        }
        assert forbidden_keys.isdisjoint(_record_keys(public))
        assert "opaque candidate payload" not in str(public)
        assert workbench.candidate_count == 1
        assert workbench.verified_result_count == 0

        verified_attempt, verified_result = _verified_attempt_and_result(
            execution=execution,
            binding=binding,
            candidate=candidate,
        )
        async with database.session() as session, session.begin():
            root_record = await session.get(TaskExecutionNodeRecord, root.node_id)
            attempt_record = await session.get(
                TaskLoopNodeAttemptRecord,
                candidate.attempt_id,
            )
            assert root_record is not None and attempt_record is not None
            root_record.status = "verified"
            root_record.revision += 1
            root_record.updated_at = verified_attempt.updated_at
            _apply_attempt(attempt_record, verified_attempt)
            session.add(
                TaskLoopVerifiedResultRecord(
                    **verified_result.model_dump(
                        mode="python",
                        exclude={"schema_version"},
                    )
                )
            )

        verified_read = await runtime.get(task_id)
        assert verified_read is not None
        verified_root = next(item for item in verified_read.nodes if item.node_id == root.node_id)
        unlocked = next(item for item in verified_read.nodes if item.node_id == dependent.node_id)
        assert not verified_root.candidate_present
        assert verified_root.verified_result_present
        assert unlocked.dependencies_verified
        assert unlocked.verified_dependency_count == unlocked.dependency_count == 1
        assert verified_read.workbench.verified_result_count == 1

        async with database.session() as session, session.begin():
            result_record = await session.get(
                TaskLoopVerifiedResultRecord,
                verified_result.result_ref_id,
            )
            assert result_record is not None
            result_record.output_digest = "0" * 64
        with pytest.raises(TaskLoopActivationProofRejectedError):
            await runtime.get(task_id)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_recovery_includes_planned_and_active_but_filters_terminal_execution(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'recovery.db').as_posix()}")
    await database.migrate()
    planned_provider = ScriptedTurnPlannerProvider([_select_two_offers])
    runtime, task_loops = _activation_runtime(database, planned_provider)
    active_provider = ScriptedTurnPlannerProvider([_select_two_offers])
    active_runtime, active_task_loops = _activation_runtime(database, active_provider)
    try:
        planned_task_id, _fallback = await _defer_two_offers(
            database,
            task_loops._turn_planner,  # noqa: SLF001
            planned_provider,
            suffix="f",
        )
        await task_loops.plan(planned_task_id)
        active_task_id, _fallback = await _defer_two_offers(
            database,
            active_task_loops._turn_planner,  # noqa: SLF001
            active_provider,
            suffix="1",
        )
        await active_task_loops.plan(active_task_id)
        active = await active_runtime.activate(active_task_id)

        recovered = await runtime.recoverable_task_ids()
        assert set(recovered) == {planned_task_id, active_task_id}
        planned_read = await runtime.get(planned_task_id)
        active_read = await runtime.get(active_task_id)
        assert planned_read is not None and planned_read.phase == "plan"
        assert active_read is not None and active_read.phase == "execute"

        cancelled, event = _cancelled_execution(active)
        async with database.session() as session, session.begin():
            record = await session.get(TaskLoopExecutionRecord, active.execution_id)
            assert record is not None
            _apply_execution(record, cancelled)
            session.add(runtime._event_record(event))  # noqa: SLF001 - exact proof fixture

        terminal_read = await runtime.get(active_task_id)
        assert terminal_read is not None
        assert terminal_read.execution is not None
        assert terminal_read.execution.status == "cancelled"
        assert not terminal_read.recoverable
        assert await runtime.recoverable_task_ids() == (planned_task_id,)
        with pytest.raises(ValueError):
            await runtime.recoverable_task_ids(limit=0)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_activation_and_input_catalog_share_quoted_enum_canonicalization(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'canonical-input.db').as_posix()}")
    await database.migrate()
    message = '请用 “PYTHON-SYNTAX” 检查 "src/app.py"，并查询 cats'

    def select_workspace_and_knowledge(request: Any) -> dict[str, Any]:
        return {
            "schema_version": "deskpilot.turn-planner-decision.v1",
            "kind": "propose_steps",
            "steps": [
                {
                    "offer_key": _offer_key_for(request, "profile"),
                    "parameters": [
                        {"name": "profile", "value": "“PYTHON-SYNTAX”"},
                        {"name": "path", "value": '"src/app.py"'},
                    ],
                },
                {
                    "offer_key": _offer_key_for(request, "query"),
                    "parameters": [{"name": "query", "value": "cats"}],
                },
            ],
        }

    provider = ScriptedTurnPlannerProvider([select_workspace_and_knowledge])
    planner, planning, task_loops, _composer = _runtimes(database, provider)

    class EnabledCheckRuntime:
        enabled = True

    executors = create_builtin_capability_executor_registry(
        planner._capabilities,  # noqa: SLF001
        knowledge=cast(Any, object()),
        workspace=cast(Any, object()),
        workspace_checks=cast(Any, EnabledCheckRuntime()),
    )
    execution = AgentExecutionRuntime(
        database,
        planning._compiler,  # noqa: SLF001
        planner._agents,  # noqa: SLF001
    )
    runtime = TaskLoopActivationRuntime(
        database,
        task_loops,
        planner,
        planning,
        execution,
        ModelPlannerNodeBinder(
            planner._agents,  # noqa: SLF001
            executors,
            create_task_loop_agent_adapter_registry(
                research_available=True,
                workspace_file_available=True,
            ),
        ),
        clock=lambda: NOW,
    )
    try:
        task_id, fallback = await _seed_custom_turn(
            database,
            suffix="2",
            message=message,
        )
        await planner.prepare(
            task_id,
            fallback.user_message_id,
            fallback,
            frozenset({"workspace_snapshot_check", "knowledge_lookup"}),
        )
        interpreted = await planner.interpret(task_id)
        assert interpreted.adjudication is not None
        assert interpreted.adjudication.outcome == "multi_step_deferred"
        await task_loops.plan(task_id)
        activated = await runtime.activate(task_id)

        async with database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(ModelPlannerNodeBindingRecord).where(
                            ModelPlannerNodeBindingRecord.execution_id == activated.execution_id
                        )
                    )
                ).all()
            )
        bindings = tuple(ModelPlannerNodeBinding.model_validate(item.manifest) for item in records)
        workspace_binding = next(
            item for item in bindings if item.recipe.route_id == "workspace_snapshot_check"
        )
        assert workspace_binding.bound_input_manifest == {
            "path": "src/app.py",
            "profile": "python-syntax",
        }
        catalog = CapabilityInputBindingCatalog(planner._capabilities)  # noqa: SLF001
        bound = catalog.bind_node(node_binding=workspace_binding)
        assert bound.arguments.profile == "python-syntax"
        assert bound.arguments.path == "src/app.py"
        assert provider.calls == 1
    finally:
        await database.dispose()
