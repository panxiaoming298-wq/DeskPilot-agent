"""Atomic persistence and proof-checked reads for immutable planning manifests."""

from datetime import UTC, datetime
from typing import Literal, cast

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.plan_compiler import PlanCompiler, PlanCompilerError
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_replanning import (
    BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT,
    AgentReplanBudgetProof,
    AgentReplanBudgetTotals,
    AgentReplanContinuationIntent,
    AgentReplanFailureSnapshot,
    AgentReplanPage,
    AgentReplanRead,
    AgentReplanRepairAdvice,
    classify_agent_replan_continuation,
    condition_replan_generation_limit,
)
from deskpilot.domain.agent_runtime import (
    ExecutionNodeStatus,
    ExecutionRunStatus,
    InvocationExecutionStatus,
    ModelTurnStatus,
)
from deskpilot.domain.task_plans import (
    DraftPlan,
    ExecutablePlan,
    ExecutablePlanPage,
    ExecutablePlanRead,
    PlanningStateRead,
    PlanNodeBudget,
    TaskContract,
    TaskContractVersionPage,
    TaskContractVersionRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentInvocationRecord,
    AgentModelTurnRecord,
    AgentReplanRecord,
    ConversationMessageRecord,
    TaskContractVersionRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    TurnRouteRecord,
    utc_now,
)


class PlanningError(RuntimeError):
    code = "PLANNING_ERROR"


class PlanningNotFoundError(PlanningError):
    code = "PLANNING_NOT_FOUND"


class PlanningVersionConflictError(PlanningError):
    code = "PLANNING_VERSION_CONFLICT"


class PlanningProofRejectedError(PlanningError):
    code = "PLANNING_PROOF_REJECTED"


REPAIRABLE_AGENT_ERROR_CODES = frozenset(
    {
        "AGENT_TASK_GRAPH_REJECTED",
        "AGENT_ROUTE_BINDING_REJECTED",
        "AGENT_LOOP_NO_PROGRESS",
        "AGENT_GRAPH_TEST_CONDITION_NOT_MET",
    }
)
READ_ONLY_REPAIRABLE_AGENT_ERROR_CODES = frozenset(
    REPAIRABLE_AGENT_ERROR_CODES - {"AGENT_GRAPH_TEST_CONDITION_NOT_MET"}
)


class PlanCompilationService:
    def __init__(self, database: Database, compiler: PlanCompiler) -> None:
        self._database = database
        self._compiler = compiler

    def preview_initial(
        self,
        contract: TaskContract,
        draft: DraftPlan,
    ) -> ExecutablePlan:
        """Compile the exact generation-1 manifest without persisting state."""

        if contract.version != 1 or contract.previous_contract_digest is not None:
            raise PlanningVersionConflictError(
                "Initial Planner preview requires Task Contract version 1"
            )
        return self._compiler.compile(contract, draft, generation=1)

    async def activate(
        self,
        contract: TaskContract,
        draft: DraftPlan,
    ) -> ExecutablePlanRead:
        """Compile and atomically advance one Task's immutable planning generation."""

        try:
            async with self._database.session() as session, session.begin():
                task = await session.scalar(
                    select(TaskRecord)
                    .where(TaskRecord.task_id == contract.task_id)
                    .with_for_update()
                )
                if task is None:
                    raise PlanningNotFoundError("Task does not exist")
                state = await session.scalar(
                    select(TaskPlanningStateRecord)
                    .where(TaskPlanningStateRecord.task_id == contract.task_id)
                    .with_for_update()
                )
                generation, insert_contract = await self._next_generation(
                    session,
                    contract,
                    state,
                )
                plan = self._compiler.compile(contract, draft, generation=generation)
                if insert_contract:
                    session.add(
                        TaskContractVersionRecord(
                            task_id=contract.task_id,
                            version=contract.version,
                            contract_id=contract.contract_id,
                            previous_contract_digest=contract.previous_contract_digest,
                            manifest=contract.model_dump(mode="json"),
                            contract_digest=contract.digest,
                            created_at=utc_now(),
                        )
                    )
                if state is not None:
                    previous = await session.get(
                        TaskPlanGenerationRecord,
                        (contract.task_id, state.active_plan_generation),
                    )
                    if previous is None or previous.status != "active":
                        raise PlanningProofRejectedError(
                            "Active plan pointer has no matching generation"
                        )
                    self._plan_from_record(previous)
                    previous.status = "superseded"
                session.add(
                    TaskPlanGenerationRecord(
                        task_id=contract.task_id,
                        generation=generation,
                        plan_id=plan.plan_id,
                        contract_version=contract.version,
                        contract_digest=contract.digest,
                        status="active",
                        manifest=plan.model_dump(mode="json"),
                        plan_manifest_digest=plan.plan_manifest_digest,
                        created_at=utc_now(),
                    )
                )
                if state is None:
                    state = TaskPlanningStateRecord(
                        task_id=contract.task_id,
                        active_contract_version=contract.version,
                        active_contract_digest=contract.digest,
                        active_plan_generation=generation,
                        active_plan_digest=plan.plan_manifest_digest,
                        revision=1,
                        updated_at=utc_now(),
                    )
                    session.add(state)
                else:
                    state.active_contract_version = contract.version
                    state.active_contract_digest = contract.digest
                    state.active_plan_generation = generation
                    state.active_plan_digest = plan.plan_manifest_digest
                    state.revision += 1
                    state.updated_at = utc_now()
                await session.flush()
                return ExecutablePlanRead(plan=plan, status="active")
        except IntegrityError as error:
            raise PlanningVersionConflictError(
                "Planning generation changed concurrently"
            ) from error

    async def activate_initial_once(
        self,
        contract: TaskContract,
        draft: DraftPlan,
    ) -> ExecutablePlanRead:
        """Create generation 1 once, or return that exact active generation.

        This entry point is deliberately narrower than ``activate``.  It is used
        after a terminal Turn Planner adjudication, where a crash may leave the
        exact plan active before its separate provenance Binding is persisted.
        Recovery must never turn that situation into generation 2.
        """

        expected = self.preview_initial(contract, draft)
        try:
            async with self._database.session() as session, session.begin():
                return await self._activate_initial_once_locked(
                    session,
                    contract=contract,
                    expected=expected,
                )
        except IntegrityError:
            # SQLite does not implement row-level FOR UPDATE.  Its uniqueness
            # constraints still elect one generation-1 writer; the loser may
            # only accept the exact winner and can never advance a generation.
            async with self._database.session() as session:
                state = await session.get(TaskPlanningStateRecord, contract.task_id)
                if state is None:
                    raise PlanningVersionConflictError(
                        "Initial planning generation changed concurrently"
                    ) from None
                return await self._validate_initial_exact(
                    session,
                    state=state,
                    contract=contract,
                    expected=expected,
                )

    async def activate_initial_once_in_session(
        self,
        session: AsyncSession,
        contract: TaskContract,
        draft: DraftPlan,
    ) -> ExecutablePlanRead:
        """Activate generation 1 inside the caller's larger atomic transaction."""

        expected = self.preview_initial(contract, draft)
        return await self._activate_initial_once_locked(
            session,
            contract=contract,
            expected=expected,
        )

    async def _activate_initial_once_locked(
        self,
        session: AsyncSession,
        *,
        contract: TaskContract,
        expected: ExecutablePlan,
    ) -> ExecutablePlanRead:
        task = await session.scalar(
            select(TaskRecord)
            .where(TaskRecord.task_id == contract.task_id)
            .with_for_update()
        )
        if task is None:
            raise PlanningNotFoundError("Task does not exist")
        state = await session.scalar(
            select(TaskPlanningStateRecord)
            .where(TaskPlanningStateRecord.task_id == contract.task_id)
            .with_for_update()
        )
        if state is not None:
            return await self._validate_initial_exact(
                session,
                state=state,
                contract=contract,
                expected=expected,
            )
        now = utc_now()
        session.add(
            TaskContractVersionRecord(
                task_id=contract.task_id,
                version=contract.version,
                contract_id=contract.contract_id,
                previous_contract_digest=contract.previous_contract_digest,
                manifest=contract.model_dump(mode="json"),
                contract_digest=contract.digest,
                created_at=now,
            )
        )
        session.add(
            TaskPlanGenerationRecord(
                task_id=contract.task_id,
                generation=1,
                plan_id=expected.plan_id,
                contract_version=contract.version,
                contract_digest=contract.digest,
                status="active",
                manifest=expected.model_dump(mode="json"),
                plan_manifest_digest=expected.plan_manifest_digest,
                created_at=now,
            )
        )
        session.add(
            TaskPlanningStateRecord(
                task_id=contract.task_id,
                active_contract_version=1,
                active_contract_digest=contract.digest,
                active_plan_generation=1,
                active_plan_digest=expected.plan_manifest_digest,
                revision=1,
                updated_at=now,
            )
        )
        await session.flush()
        return ExecutablePlanRead(plan=expected, status="active")

    async def _validate_initial_exact(
        self,
        session: AsyncSession,
        *,
        state: TaskPlanningStateRecord,
        contract: TaskContract,
        expected: ExecutablePlan,
    ) -> ExecutablePlanRead:
        await self._validate_active_state(session, state)
        if (
            state.active_contract_version != 1
            or state.active_plan_generation != 1
            or state.active_contract_digest != contract.digest
            or state.active_plan_digest != expected.plan_manifest_digest
        ):
            raise PlanningVersionConflictError(
                "Task already has a different active planning generation"
            )
        contract_record = await session.get(
            TaskContractVersionRecord,
            (contract.task_id, 1),
        )
        plan_record = await session.get(
            TaskPlanGenerationRecord,
            (contract.task_id, 1),
        )
        if contract_record is None or plan_record is None:
            raise PlanningProofRejectedError(
                "Initial planning evidence is incomplete"
            )
        persisted_contract = self._contract_from_record(contract_record)
        persisted_plan = self._plan_from_record(plan_record)
        if persisted_contract != contract or persisted_plan != expected:
            raise PlanningVersionConflictError(
                "Existing generation 1 does not match the trusted Planner recipe"
            )
        return ExecutablePlanRead(plan=persisted_plan, status="active")

    async def get_state(self, task_id: str) -> PlanningStateRead:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no planning state")
            await self._validate_active_state(session, state)
            return self._state_read(state)

    async def get_current_contract(self, task_id: str) -> TaskContractVersionRead:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Task Contract")
            await self._validate_active_state(session, state)
            record = await session.get(
                TaskContractVersionRecord,
                (task_id, state.active_contract_version),
            )
            if record is None:
                raise PlanningProofRejectedError("Active Task Contract is missing")
            return TaskContractVersionRead(
                contract=self._contract_from_record(record),
                contract_digest=record.contract_digest,
                active=True,
            )

    async def list_contracts(self, task_id: str) -> TaskContractVersionPage:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Task Contract")
            await self._validate_active_state(session, state)
            records = tuple(
                (
                    await session.scalars(
                        select(TaskContractVersionRecord)
                        .where(TaskContractVersionRecord.task_id == task_id)
                        .order_by(TaskContractVersionRecord.version)
                    )
                ).all()
            )
            return TaskContractVersionPage(
                contracts=tuple(
                    TaskContractVersionRead(
                        contract=self._contract_from_record(record),
                        contract_digest=record.contract_digest,
                        active=record.version == state.active_contract_version,
                    )
                    for record in records
                )
            )

    async def get_plan(self, task_id: str, generation: int) -> ExecutablePlanRead:
        async with self._database.session() as session:
            record = await session.get(TaskPlanGenerationRecord, (task_id, generation))
            if record is None:
                raise PlanningNotFoundError("Executable Plan generation does not exist")
            await self._validate_plan_contract(session, record)
            return ExecutablePlanRead(
                plan=self._plan_from_record(record),
                status=self._plan_status(record.status),
            )

    async def list_plans(self, task_id: str) -> ExecutablePlanPage:
        async with self._database.session() as session:
            state = await session.get(TaskPlanningStateRecord, task_id)
            if state is None:
                raise PlanningNotFoundError("Task has no Executable Plan")
            await self._validate_active_state(session, state)
            records = tuple(
                (
                    await session.scalars(
                        select(TaskPlanGenerationRecord)
                        .where(TaskPlanGenerationRecord.task_id == task_id)
                        .order_by(TaskPlanGenerationRecord.generation)
                    )
                ).all()
            )
            result: list[ExecutablePlanRead] = []
            for record in records:
                await self._validate_plan_contract(session, record)
                result.append(
                    ExecutablePlanRead(
                        plan=self._plan_from_record(record),
                        status=self._plan_status(record.status),
                    )
                )
            return ExecutablePlanPage(plans=tuple(result))

    async def replan_failed_agent_execution(
        self,
        task_id: str,
        source_run_id: str,
        draft: DraftPlan,
        continuation_intent: AgentReplanContinuationIntent | None = None,
    ) -> AgentReplanRead:
        """Atomically replace one eligible failed Agent Run with generation N+1."""

        try:
            async with self._database.session() as session, session.begin():
                state = await session.scalar(
                    select(TaskPlanningStateRecord)
                    .where(TaskPlanningStateRecord.task_id == task_id)
                    .with_for_update()
                )
                source_run = await session.scalar(
                    select(TaskExecutionRunRecord)
                    .where(TaskExecutionRunRecord.run_id == source_run_id)
                    .with_for_update()
                )
                route = await session.scalar(
                    select(TurnRouteRecord)
                    .where(TurnRouteRecord.task_id == task_id)
                    .with_for_update()
                )
                if state is None or source_run is None or route is None:
                    raise PlanningNotFoundError("Replan source evidence does not exist")
                if (
                    source_run.task_id != task_id
                    or source_run.status != ExecutionRunStatus.FAILED.value
                    or source_run.plan_generation != state.active_plan_generation
                ):
                    raise PlanningVersionConflictError(
                        "Only the current terminal failed Run can be replanned"
                    )
                is_read_only_replan = bool(
                    route.route_id == "workspace_directory_analyze"
                    and route.error_code in READ_ONLY_REPAIRABLE_AGENT_ERROR_CODES
                )
                is_condition_replan = bool(
                    route.route_id == "workspace_dynamic_patch_test"
                    and route.error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                )
                if (
                    route.status != "failed"
                    or not (is_read_only_replan or is_condition_replan)
                    or (is_read_only_replan and source_run.plan_generation != 1)
                ):
                    raise PlanningVersionConflictError(
                        "Failure is not eligible for the bounded Agent replan"
                    )
                await self._validate_active_state(session, state)
                source_plan_record = await session.get(
                    TaskPlanGenerationRecord,
                    (task_id, source_run.plan_generation),
                )
                contract_record = await session.get(
                    TaskContractVersionRecord,
                    (task_id, state.active_contract_version),
                )
                if source_plan_record is None or contract_record is None:
                    raise PlanningProofRejectedError("Replan planning evidence is missing")
                source_plan = self._plan_from_record(source_plan_record)
                contract = self._contract_from_record(contract_record)
                if (
                    source_run.plan_digest != source_plan.plan_manifest_digest
                    or source_plan.task_contract.digest != contract.digest
                    or draft.task_id != task_id
                ):
                    raise PlanningProofRejectedError("Replan source proof changed")
                generation_limit = (
                    condition_replan_generation_limit(contract.constraints)
                    if is_condition_replan
                    else None
                )
                if is_condition_replan and generation_limit is None:
                    raise PlanningProofRejectedError(
                        "Task Contract does not authorize a user-requested Patch replan"
                    )
                if is_condition_replan and source_run.plan_generation >= cast(
                    int,
                    generation_limit,
                ):
                    raise PlanningVersionConflictError(
                        "Patch repair Plan generation limit was reached"
                    )
                if is_condition_replan:
                    if continuation_intent is None:
                        raise PlanningProofRejectedError(
                            "Conditional Patch replan has no explicit user continuation"
                        )
                    await self._validate_continuation_intent(
                        session,
                        continuation_intent,
                        task_id,
                    )
                elif continuation_intent is not None:
                    raise PlanningProofRejectedError(
                        "Read-only Agent replan cannot contain a Patch continuation"
                    )

                failed_nodes = tuple(
                    (
                        await session.scalars(
                            select(TaskExecutionNodeRecord)
                            .where(
                                TaskExecutionNodeRecord.run_id == source_run_id,
                                TaskExecutionNodeRecord.status == ExecutionNodeStatus.FAILED.value,
                            )
                            .order_by(TaskExecutionNodeRecord.node_id)
                        )
                    ).all()
                )
                failed_invocations = tuple(
                    (
                        await session.scalars(
                            select(AgentInvocationRecord)
                            .where(
                                AgentInvocationRecord.run_id == source_run_id,
                                AgentInvocationRecord.execution_status
                                == InvocationExecutionStatus.FAILED_TERMINAL.value,
                            )
                            .order_by(AgentInvocationRecord.invocation_id)
                        )
                    ).all()
                )
                failed_turns = tuple(
                    (
                        await session.scalars(
                            select(AgentModelTurnRecord)
                            .join(
                                AgentInvocationRecord,
                                AgentInvocationRecord.invocation_id
                                == AgentModelTurnRecord.invocation_id,
                            )
                            .where(
                                AgentInvocationRecord.run_id == source_run_id,
                                AgentModelTurnRecord.status == ModelTurnStatus.FAILED.value,
                                AgentModelTurnRecord.stable_error_code == route.error_code,
                            )
                            .order_by(AgentModelTurnRecord.turn_id)
                        )
                    ).all()
                )
                if (
                    not failed_nodes
                    or not failed_invocations
                    or (is_read_only_replan and not failed_turns)
                    or (is_condition_replan and failed_turns)
                ):
                    raise PlanningProofRejectedError(
                        "Terminal failure has no complete stable Agent evidence"
                    )
                from deskpilot.application.agent_supervisor_runtime import (
                    AgentSupervisorError,
                    AgentSupervisorRuntime,
                )

                try:
                    failed_conditions = (
                        await AgentSupervisorRuntime.collect_failed_condition_decisions(
                            session,
                            source_run,
                        )
                        if is_condition_replan
                        else ()
                    )
                except AgentSupervisorError as error:
                    raise PlanningProofRejectedError(
                        "Replan condition decision proof changed"
                    ) from error
                if is_condition_replan and not failed_conditions:
                    raise PlanningProofRejectedError(
                        "Conditional failure has no false server decision"
                    )
                snapshot_material = {
                    "schema_version": (
                        "deskpilot.agent-replan-failure-snapshot.v2"
                        if is_condition_replan
                        else "deskpilot.agent-replan-failure-snapshot.v1"
                    ),
                    "task_id": task_id,
                    "source_run_id": source_run_id,
                    "source_plan_generation": source_run.plan_generation,
                    "source_plan_digest": source_run.plan_digest,
                    "contract_version": contract.version,
                    "contract_digest": contract.digest,
                    "route_id": route.route_id,
                    "route_parameter_digest": route.parameter_digest,
                    "route_revision": route.revision,
                    "stable_error_code": route.error_code,
                    "failed_node_ids": tuple(item.node_id for item in failed_nodes),
                    "failed_invocation_ids": tuple(
                        item.invocation_id for item in failed_invocations
                    ),
                    "failed_model_turn_ids": tuple(item.turn_id for item in failed_turns),
                }
                if is_condition_replan:
                    snapshot_material["condition_decision_digests"] = tuple(
                        item.decision_digest for item in failed_conditions
                    )
                failure_snapshot = AgentReplanFailureSnapshot.model_validate(
                    {
                        **snapshot_material,
                        "snapshot_digest": sha256_digest(snapshot_material),
                    }
                )
                try:
                    reusable_sources = await AgentSupervisorRuntime.collect_verified_replan_sources(
                        session,
                        source_run,
                        exclude_result_kinds=(
                            frozenset({"patch_test"})
                            if is_condition_replan
                            else frozenset()
                        ),
                    )
                except AgentSupervisorError as error:
                    raise PlanningProofRejectedError(
                        "Replan reusable ResultRef proof changed"
                    ) from error
                strategy_code = {
                    "AGENT_TASK_GRAPH_REJECTED": "rebuild_graph_from_current_offer",
                    "AGENT_ROUTE_BINDING_REJECTED": ("reuse_verified_evidence_and_rebind_route"),
                    "AGENT_LOOP_NO_PROGRESS": ("simplify_graph_and_consume_verified_evidence"),
                    "AGENT_GRAPH_TEST_CONDITION_NOT_MET": (
                        "propose_fresh_patch_after_failed_test"
                    ),
                }[failure_snapshot.stable_error_code]
                advice_material = {
                    "schema_version": (
                        "deskpilot.agent-replan-repair-advice.v2"
                        if is_condition_replan
                        else "deskpilot.agent-replan-repair-advice.v1"
                    ),
                    "failure_snapshot_digest": failure_snapshot.snapshot_digest,
                    "stable_error_code": failure_snapshot.stable_error_code,
                    "strategy_code": strategy_code,
                    "objective": (
                        (
                            "Build a fresh graph from the current server offer after the exact "
                            "test condition failed. The previous Patch receipt and test result "
                            "are evidence only: propose a new Patch node, stage the current file "
                            "again, and require a new user confirmation before any write."
                        )
                        if is_condition_replan
                        else (
                            "Build a fresh graph only from the current server offer. Reuse a "
                            "named verified ResultRef when it avoids repeating completed "
                            "read-only work; rebind every Route input and do not request new "
                            "capabilities."
                        )
                    ),
                    "granted_capability_ids": (),
                    "result_sources": reusable_sources,
                }
                repair_advice = AgentReplanRepairAdvice.model_validate(
                    {
                        **advice_material,
                        "advice_digest": sha256_digest(advice_material),
                    }
                )

                target_generation = source_run.plan_generation + 1
                target_plan = self._compiler.compile(
                    contract,
                    draft,
                    generation=target_generation,
                )
                budget_proof: AgentReplanBudgetProof | None = None
                if (
                    is_condition_replan
                    and BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT in contract.constraints
                ):
                    assert generation_limit is not None
                    budget_proof = await self._build_replan_budget_proof(
                        session,
                        task_id=task_id,
                        contract=contract,
                        source_plan_generation=source_run.plan_generation,
                        target_plan=target_plan,
                        maximum_plan_generations=generation_limit,
                    )
                now = utc_now()
                source_plan_record.status = "superseded"
                session.add(
                    TaskPlanGenerationRecord(
                        task_id=task_id,
                        generation=target_generation,
                        plan_id=target_plan.plan_id,
                        contract_version=contract.version,
                        contract_digest=contract.digest,
                        status="active",
                        manifest=target_plan.model_dump(mode="json"),
                        plan_manifest_digest=target_plan.plan_manifest_digest,
                        created_at=now,
                    )
                )
                state.active_plan_generation = target_generation
                state.active_plan_digest = target_plan.plan_manifest_digest
                state.revision += 1
                state.updated_at = now
                await session.flush()
                target_run = await self._add_execution_run(session, target_plan, now)

                route.status = "ready"
                route.result_manifest = None
                route.result_digest = None
                route.error_code = None
                route.revision += 1
                route.updated_at = now

                replan_id = f"rpl_{sha256_digest({'source_run_id': source_run_id})}"
                replan_material: dict[str, object] = {
                    "schema_version": (
                        "deskpilot.agent-replan.v5"
                        if budget_proof is not None
                        else "deskpilot.agent-replan.v4"
                        if is_condition_replan
                        else "deskpilot.agent-replan.v2"
                    ),
                    "replan_id": replan_id,
                    "task_id": task_id,
                    "source_run_id": source_run_id,
                    "source_plan_generation": source_run.plan_generation,
                    "source_plan_digest": source_run.plan_digest,
                    "target_run_id": target_run.run_id,
                    "target_plan_generation": target_generation,
                    "target_plan_digest": target_plan.plan_manifest_digest,
                    "contract_version": contract.version,
                    "contract_digest": contract.digest,
                    "failure_snapshot": failure_snapshot,
                    "repair_advice": repair_advice,
                    "status": "activated",
                    "created_at": now,
                }
                if continuation_intent is not None:
                    replan_material["continuation_intent"] = continuation_intent
                if budget_proof is not None:
                    replan_material["budget_proof"] = budget_proof
                replan = AgentReplanRead.model_validate(
                    {
                        **replan_material,
                        "replan_digest": sha256_digest(replan_material),
                    }
                )
                session.add(
                    AgentReplanRecord(
                        replan_id=replan.replan_id,
                        task_id=task_id,
                        source_run_id=source_run_id,
                        source_plan_generation=source_run.plan_generation,
                        source_plan_digest=source_run.plan_digest,
                        target_run_id=target_run.run_id,
                        target_plan_generation=target_generation,
                        target_plan_digest=target_plan.plan_manifest_digest,
                        contract_version=contract.version,
                        contract_digest=contract.digest,
                        status="activated",
                        manifest=replan.model_dump(mode="json"),
                        replan_digest=replan.replan_digest,
                        created_at=now,
                    )
                )
                await session.flush()
                return replan
        except IntegrityError as error:
            raise PlanningVersionConflictError("Replan generation changed concurrently") from error

    async def _build_replan_budget_proof(
        self,
        session: AsyncSession,
        *,
        task_id: str,
        contract: TaskContract,
        source_plan_generation: int,
        target_plan: ExecutablePlan,
        maximum_plan_generations: int,
    ) -> AgentReplanBudgetProof:
        allocated_before = await self._allocated_budget_through_generation(
            session,
            task_id=task_id,
            maximum_generation=source_plan_generation,
        )
        target_plan_allocation = AgentReplanBudgetTotals.from_plan_budgets(
            node.budget for node in target_plan.nodes
        )
        allocated_after = allocated_before.plus(target_plan_allocation)
        budget_limit = AgentReplanBudgetTotals.from_task_budget(contract.budget)
        if not budget_limit.contains(allocated_after):
            raise PlanningProofRejectedError(
                "Cross-generation Task budget cannot fund another replacement Plan"
            )
        material = {
            "schema_version": "deskpilot.agent-replan-budget-proof.v1",
            "contract_digest": contract.digest,
            "maximum_plan_generations": maximum_plan_generations,
            "source_plan_generation": source_plan_generation,
            "target_plan_generation": target_plan.plan_generation,
            "budget_limit": budget_limit,
            "allocated_before": allocated_before,
            "target_plan_allocation": target_plan_allocation,
            "allocated_after_activation": allocated_after,
            "remaining_after_activation": budget_limit.remaining_after(allocated_after),
        }
        return AgentReplanBudgetProof.model_validate(
            {**material, "budget_digest": sha256_digest(material)}
        )

    @staticmethod
    async def _allocated_budget_through_generation(
        session: AsyncSession,
        *,
        task_id: str,
        maximum_generation: int,
    ) -> AgentReplanBudgetTotals:
        records = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .join(
                        TaskExecutionRunRecord,
                        TaskExecutionRunRecord.run_id == TaskExecutionNodeRecord.run_id,
                    )
                    .where(
                        TaskExecutionRunRecord.task_id == task_id,
                        TaskExecutionRunRecord.plan_generation <= maximum_generation,
                    )
                )
            ).all()
        )
        return AgentReplanBudgetTotals.from_plan_budgets(
            PlanNodeBudget.model_validate(item.budget) for item in records
        )

    async def replan_failed_directory_analysis(
        self,
        task_id: str,
        source_run_id: str,
        draft: DraftPlan,
    ) -> AgentReplanRead:
        """Backward-compatible entry for the original read-only replan slice."""

        return await self.replan_failed_agent_execution(task_id, source_run_id, draft)

    async def list_replans(self, task_id: str) -> AgentReplanPage:
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(AgentReplanRecord)
                        .where(AgentReplanRecord.task_id == task_id)
                        .order_by(AgentReplanRecord.target_plan_generation)
                    )
                ).all()
            )
            replans = []
            for record in records:
                replans.append(await self._replan_from_record(session, record))
            return AgentReplanPage(replans=tuple(replans))

    async def _add_execution_run(
        self,
        session: AsyncSession,
        plan: ExecutablePlan,
        now: datetime,
    ) -> TaskExecutionRunRecord:
        runnable = tuple(
            node
            for node in plan.nodes
            if (node.bound_agent is not None or node.capability is not None)
            and node.runtime_enabled
        )
        if not runnable:
            raise PlanningProofRejectedError("Replacement Plan has no runnable node")
        run_identity = {
            "task_id": plan.task_id,
            "generation": plan.plan_generation,
            "plan_digest": plan.plan_manifest_digest,
        }
        run_id = f"run_{sha256_digest(run_identity)}"
        run = TaskExecutionRunRecord(
            run_id=run_id,
            task_id=plan.task_id,
            plan_generation=plan.plan_generation,
            plan_digest=plan.plan_manifest_digest,
            status=ExecutionRunStatus.ACTIVE.value,
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(run)
        await session.flush()
        for node in plan.nodes:
            is_ready = (
                not node.depends_on
                and node.handoff_parent_node_id is None
                and (node.bound_agent is not None or node.capability is not None)
                and node.runtime_enabled
            )
            session.add(
                TaskExecutionNodeRecord(
                    node_id=node.node_id,
                    run_id=run_id,
                    local_key=node.local_key,
                    node_kind=node.kind.value,
                    node_spec_digest=node.node_spec_digest,
                    depends_on=list(node.depends_on),
                    handoff_parent_node_id=node.handoff_parent_node_id,
                    bound_agent=(
                        node.bound_agent.model_dump(mode="json")
                        if node.bound_agent is not None
                        else None
                    ),
                    capability=(
                        node.capability.model_dump(mode="json")
                        if node.capability is not None
                        else None
                    ),
                    acceptance_refs=list(node.acceptance_refs),
                    budget=node.budget.model_dump(mode="json"),
                    runtime_enabled=node.runtime_enabled,
                    status=(
                        ExecutionNodeStatus.READY.value
                        if is_ready
                        else ExecutionNodeStatus.PENDING.value
                    ),
                    revision=1,
                    attempt_count=0,
                    claim_fencing_token=0,
                    created_at=now,
                    updated_at=now,
                )
            )
        await session.flush()
        for node in plan.nodes:
            for source in node.depends_on:
                session.add(
                    TaskExecutionEdgeRecord(
                        run_id=run_id,
                        from_node_id=source,
                        to_node_id=node.node_id,
                        requirement="verified",
                    )
                )
        await session.flush()
        return run

    async def _replan_from_record(
        self,
        session: AsyncSession,
        record: AgentReplanRecord,
    ) -> AgentReplanRead:
        try:
            replan = AgentReplanRead.model_validate(record.manifest)
        except ValidationError as error:
            raise PlanningProofRejectedError("Agent replan manifest is invalid") from error
        if (
            replan.replan_id != record.replan_id
            or replan.task_id != record.task_id
            or replan.source_run_id != record.source_run_id
            or replan.source_plan_generation != record.source_plan_generation
            or replan.source_plan_digest != record.source_plan_digest
            or replan.target_run_id != record.target_run_id
            or replan.target_plan_generation != record.target_plan_generation
            or replan.target_plan_digest != record.target_plan_digest
            or replan.contract_version != record.contract_version
            or replan.contract_digest != record.contract_digest
            or replan.status != record.status
            or replan.replan_digest != record.replan_digest
        ):
            raise PlanningProofRejectedError("Agent replan record binding changed")
        source_run = await session.get(TaskExecutionRunRecord, record.source_run_id)
        target_run = await session.get(TaskExecutionRunRecord, record.target_run_id)
        source_plan = await session.get(
            TaskPlanGenerationRecord,
            (record.task_id, record.source_plan_generation),
        )
        target_plan = await session.get(
            TaskPlanGenerationRecord,
            (record.task_id, record.target_plan_generation),
        )
        contract = await session.get(
            TaskContractVersionRecord,
            (record.task_id, record.contract_version),
        )
        route = await session.get(TurnRouteRecord, record.task_id)
        if any(
            item is None
            for item in (source_run, target_run, source_plan, target_plan, contract, route)
        ):
            raise PlanningProofRejectedError("Agent replan lineage evidence is missing")
        assert source_run is not None
        assert target_run is not None
        assert source_plan is not None
        assert target_plan is not None
        assert contract is not None
        assert route is not None
        self._plan_from_record(source_plan)
        target_plan_manifest = self._plan_from_record(target_plan)
        contract_manifest = self._contract_from_record(contract)
        if (
            source_run.task_id != record.task_id
            or source_run.plan_generation != record.source_plan_generation
            or source_run.plan_digest != record.source_plan_digest
            or source_run.status != ExecutionRunStatus.FAILED.value
            or target_run.task_id != record.task_id
            or target_run.plan_generation != record.target_plan_generation
            or target_run.plan_digest != record.target_plan_digest
            or source_plan.status != "superseded"
            or contract.contract_digest != record.contract_digest
            or route.route_id != replan.failure_snapshot.route_id
            or route.parameter_digest != replan.failure_snapshot.route_parameter_digest
        ):
            raise PlanningProofRejectedError("Agent replan lineage proof changed")
        failed_node_ids = set(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord.node_id).where(
                        TaskExecutionNodeRecord.run_id == record.source_run_id,
                        TaskExecutionNodeRecord.status == ExecutionNodeStatus.FAILED.value,
                    )
                )
            ).all()
        )
        failed_invocation_ids = set(
            (
                await session.scalars(
                    select(AgentInvocationRecord.invocation_id).where(
                        AgentInvocationRecord.run_id == record.source_run_id,
                        AgentInvocationRecord.execution_status
                        == InvocationExecutionStatus.FAILED_TERMINAL.value,
                    )
                )
            ).all()
        )
        failed_model_turn_ids = set(
            (
                await session.scalars(
                    select(AgentModelTurnRecord.turn_id)
                    .join(
                        AgentInvocationRecord,
                        AgentInvocationRecord.invocation_id == AgentModelTurnRecord.invocation_id,
                    )
                    .where(
                        AgentInvocationRecord.run_id == record.source_run_id,
                        AgentModelTurnRecord.status == ModelTurnStatus.FAILED.value,
                        AgentModelTurnRecord.stable_error_code
                        == replan.failure_snapshot.stable_error_code,
                    )
                )
            ).all()
        )
        if (
            not set(replan.failure_snapshot.failed_node_ids).issubset(failed_node_ids)
            or not set(replan.failure_snapshot.failed_invocation_ids).issubset(
                failed_invocation_ids
            )
            or not set(replan.failure_snapshot.failed_model_turn_ids).issubset(
                failed_model_turn_ids
            )
        ):
            raise PlanningProofRejectedError("Agent replan failure evidence changed")
        if replan.failure_snapshot.schema_version == (
            "deskpilot.agent-replan-failure-snapshot.v2"
        ):
            from deskpilot.application.agent_supervisor_runtime import (
                AgentSupervisorError,
                AgentSupervisorRuntime,
            )

            try:
                failed_conditions = (
                    await AgentSupervisorRuntime.collect_failed_condition_decisions(
                        session,
                        source_run,
                    )
                )
            except AgentSupervisorError as error:
                raise PlanningProofRejectedError(
                    "Agent replan condition decision proof changed"
                ) from error
            if replan.failure_snapshot.condition_decision_digests != tuple(
                item.decision_digest for item in failed_conditions
            ):
                raise PlanningProofRejectedError(
                    "Agent replan condition failure evidence changed"
                )
        if replan.schema_version in {
            "deskpilot.agent-replan.v4",
            "deskpilot.agent-replan.v5",
        }:
            assert replan.continuation_intent is not None
            await self._validate_continuation_intent(
                session,
                replan.continuation_intent,
                record.task_id,
            )
        if replan.schema_version == "deskpilot.agent-replan.v5":
            assert replan.budget_proof is not None
            generation_limit = condition_replan_generation_limit(
                contract_manifest.constraints
            )
            if generation_limit != replan.budget_proof.maximum_plan_generations:
                raise PlanningProofRejectedError(
                    "Agent replan generation limit proof changed"
                )
            expected_budget_proof = await self._build_replan_budget_proof(
                session,
                task_id=record.task_id,
                contract=contract_manifest,
                source_plan_generation=record.source_plan_generation,
                target_plan=target_plan_manifest,
                maximum_plan_generations=generation_limit,
            )
            if replan.budget_proof != expected_budget_proof:
                raise PlanningProofRejectedError(
                    "Agent replan cross-generation budget proof changed"
                )
        if replan.schema_version in {
            "deskpilot.agent-replan.v2",
            "deskpilot.agent-replan.v3",
            "deskpilot.agent-replan.v4",
            "deskpilot.agent-replan.v5",
        }:
            from deskpilot.application.agent_supervisor_runtime import (
                AgentSupervisorError,
                AgentSupervisorRuntime,
            )

            try:
                verified_sources = await AgentSupervisorRuntime.collect_verified_replan_sources(
                    session,
                    source_run,
                    exclude_result_kinds=(
                        frozenset({"patch_test"})
                        if replan.failure_snapshot.stable_error_code
                        == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                        else frozenset()
                    ),
                )
            except AgentSupervisorError as error:
                raise PlanningProofRejectedError(
                    "Agent replan imported ResultRef proof changed"
                ) from error
            if (
                replan.repair_advice is None
                or replan.repair_advice.result_sources != verified_sources
                or replan.repair_advice.granted_capability_ids
            ):
                raise PlanningProofRejectedError("Agent replan repair advice proof changed")
        return replan

    @staticmethod
    async def _validate_continuation_intent(
        session: AsyncSession,
        intent: AgentReplanContinuationIntent,
        task_id: str,
    ) -> None:
        message = await session.get(ConversationMessageRecord, intent.message_id)
        if message is None:
            raise PlanningProofRejectedError(
                "Agent replan continuation message is missing"
            )
        material = {
            "message_id": message.message_id,
            "conversation_id": message.conversation_id,
            "task_id": message.task_id,
            "role": message.role,
            "content": message.content,
            "content_ref": message.content_ref,
            "classification": message.classification,
            "created_at": (
                message.created_at.replace(tzinfo=UTC)
                if message.created_at.tzinfo is None
                else message.created_at.astimezone(UTC)
            ),
        }
        if (
            intent.task_id != task_id
            or message.task_id != task_id
            or message.role != "user"
            or message.status != "active"
            or message.content is None
            or message.content_ref is not None
            or message.message_digest != sha256_digest(material)
            or intent.message_digest != message.message_digest
            or classify_agent_replan_continuation(message.content) != intent.intent_code
        ):
            raise PlanningProofRejectedError(
                "Agent replan continuation message proof changed"
            )

    async def _next_generation(
        self,
        session: AsyncSession,
        contract: TaskContract,
        state: TaskPlanningStateRecord | None,
    ) -> tuple[int, bool]:
        if state is None:
            if contract.version != 1 or contract.previous_contract_digest is not None:
                raise PlanningVersionConflictError(
                    "First Task Contract must be version 1 without a predecessor"
                )
            return 1, True
        await self._validate_active_state(session, state)
        active_record = await session.get(
            TaskContractVersionRecord,
            (contract.task_id, state.active_contract_version),
        )
        if active_record is None:
            raise PlanningProofRejectedError("Active Task Contract is missing")
        active_contract = self._contract_from_record(active_record)
        if contract.contract_id != active_contract.contract_id:
            raise PlanningVersionConflictError("Task Contract identity cannot change")
        if contract.version == state.active_contract_version:
            if contract.digest != state.active_contract_digest:
                raise PlanningVersionConflictError("Existing Task Contract version is immutable")
            return state.active_plan_generation + 1, False
        if contract.version != state.active_contract_version + 1:
            raise PlanningVersionConflictError("Task Contract versions must be contiguous")
        if contract.previous_contract_digest != state.active_contract_digest:
            raise PlanningVersionConflictError("Task Contract predecessor digest is stale")
        return state.active_plan_generation + 1, True

    async def _validate_active_state(
        self,
        session: AsyncSession,
        state: TaskPlanningStateRecord,
    ) -> None:
        contract_record = await session.get(
            TaskContractVersionRecord,
            (state.task_id, state.active_contract_version),
        )
        plan_record = await session.get(
            TaskPlanGenerationRecord,
            (state.task_id, state.active_plan_generation),
        )
        if contract_record is None or plan_record is None or plan_record.status != "active":
            raise PlanningProofRejectedError("Planning state points to missing evidence")
        contract = self._contract_from_record(contract_record)
        plan = self._plan_from_record(plan_record)
        if (
            contract.digest != state.active_contract_digest
            or plan.plan_manifest_digest != state.active_plan_digest
            or plan.task_contract.digest != contract.digest
        ):
            raise PlanningProofRejectedError("Planning state digest does not match evidence")

    async def _validate_plan_contract(
        self,
        session: AsyncSession,
        record: TaskPlanGenerationRecord,
    ) -> None:
        contract_record = await session.get(
            TaskContractVersionRecord,
            (record.task_id, record.contract_version),
        )
        if contract_record is None:
            raise PlanningProofRejectedError("Executable Plan Task Contract is missing")
        contract = self._contract_from_record(contract_record)
        plan = self._plan_from_record(record)
        if (
            record.contract_digest != contract.digest
            or plan.task_contract.version != contract.version
            or plan.task_contract.digest != contract.digest
        ):
            raise PlanningProofRejectedError("Executable Plan Task Contract binding changed")

    @staticmethod
    def _contract_from_record(record: TaskContractVersionRecord) -> TaskContract:
        try:
            contract = TaskContract.model_validate(record.manifest)
        except ValidationError as error:
            raise PlanningProofRejectedError("Task Contract manifest is invalid") from error
        if (
            contract.task_id != record.task_id
            or contract.version != record.version
            or contract.contract_id != record.contract_id
            or contract.previous_contract_digest != record.previous_contract_digest
            or contract.digest != record.contract_digest
        ):
            raise PlanningProofRejectedError("Task Contract digest does not match")
        return contract

    def _plan_from_record(self, record: TaskPlanGenerationRecord) -> ExecutablePlan:
        try:
            plan = ExecutablePlan.model_validate(record.manifest)
            self._compiler.validate_manifest(plan)
        except (ValidationError, PlanCompilerError) as error:
            raise PlanningProofRejectedError("Executable Plan manifest is invalid") from error
        if (
            plan.task_id != record.task_id
            or plan.plan_generation != record.generation
            or plan.plan_id != record.plan_id
            or plan.task_contract.version != record.contract_version
            or plan.task_contract.digest != record.contract_digest
            or plan.plan_manifest_digest != record.plan_manifest_digest
        ):
            raise PlanningProofRejectedError("Executable Plan digest does not match")
        return plan

    @staticmethod
    def _state_read(state: TaskPlanningStateRecord) -> PlanningStateRead:
        return PlanningStateRead(
            task_id=state.task_id,
            active_contract_version=state.active_contract_version,
            active_contract_digest=state.active_contract_digest,
            active_plan_generation=state.active_plan_generation,
            active_plan_digest=state.active_plan_digest,
            revision=state.revision,
        )

    @staticmethod
    def _plan_status(value: str) -> Literal["active", "superseded"]:
        if value not in {"active", "superseded"}:
            raise PlanningProofRejectedError("Executable Plan status is invalid")
        return cast(Literal["active", "superseded"], value)
