"""Atomic generation-1 activation for sealed model-planner Task Loops."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.agent_execution_runtime import (
    AgentExecutionRuntime,
    AgentRuntimeError,
)
from deskpilot.application.model_planner_composer import RevalidatedOfferStep
from deskpilot.application.model_planner_node_binder import (
    ModelPlannerNodeBinder,
    ModelPlannerNodeBindingError,
)
from deskpilot.application.multi_step_plan_runtime import (
    ModelPlannerTaskLoopBundle,
    MultiStepPlanRuntime,
    MultiStepPlanRuntimeError,
)
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
)
from deskpilot.application.turn_planner_runtime import (
    TurnPlannerRuntime,
    TurnPlannerRuntimeError,
)
from deskpilot.application.workspace_command_plan_binder import (
    WorkspaceCommandPlanBinder,
    WorkspaceCommandPlanBindingError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import VerifiedCapabilityResultRef
from deskpilot.domain.coding_tools import GitCommitPreview, GitCommitReceipt
from deskpilot.domain.task_loop import ModelPlannerDraft, TaskLoop
from deskpilot.domain.task_loop_approvals import (
    TaskLoopCapabilityApproval,
    parse_task_loop_capability_preview,
)
from deskpilot.domain.task_loop_cycle import TaskLoopCycleEvent, TaskLoopCycleRead
from deskpilot.domain.task_loop_execution import (
    ModelPlannerNodeBinding,
    TaskLoopExecution,
    TaskLoopExecutionEvent,
    TaskLoopExecutionNodeRead,
    TaskLoopExecutionRead,
    TaskLoopNodeAttempt,
    TaskLoopVerifiedResult,
    WorkspaceCodingChangeRead,
    WorkspaceCodingCoordinatorEvidenceRead,
    WorkspaceCodingDeliveryWorkbenchRead,
    WorkspaceCodingFailureRepairRead,
    WorkspaceCodingGitCommitEvidenceRead,
    WorkspaceCodingPlannerEvidenceRead,
    WorkspaceCodingRollbackPointRead,
    WorkspaceCodingTestRunRead,
)
from deskpilot.domain.task_plans import DraftNodeKind, ExecutablePlan, TaskContract
from deskpilot.domain.workspace_coding_amendments import (
    WorkspaceCodingAmendmentBinding,
)
from deskpilot.domain.workspace_command_plans import WorkspaceCommandPlanBinding
from deskpilot.domain.workspace_files import WorkspacePatchPreview, WorkspacePatchReceipt
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentInvocationRecord,
    AgentModelTurnRecord,
    ConversationMessageRecord,
    ModelDispatchAttemptRecord,
    ModelPlannerDraftRecord,
    ModelPlannerNodeBindingRecord,
    TaskContractVersionRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopCycleEventRecord,
    TaskLoopExecutionEventRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopRecord,
    TaskLoopVerifiedResultRecord,
    TaskPlanGenerationRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    TurnRouteRecord,
    WorkspaceCodingAmendmentBindingRecord,
    WorkspaceCodingDeliveryRecord,
)


class TaskLoopActivationError(RuntimeError):
    code = "TASK_LOOP_ACTIVATION_ERROR"


class TaskLoopActivationNotFoundError(TaskLoopActivationError):
    code = "TASK_LOOP_ACTIVATION_NOT_FOUND"


class TaskLoopActivationNotEligibleError(TaskLoopActivationError):
    code = "TASK_LOOP_ACTIVATION_NOT_ELIGIBLE"


class TaskLoopActivationProofRejectedError(TaskLoopActivationError):
    code = "TASK_LOOP_ACTIVATION_PROOF_REJECTED"


class TaskLoopActivationConflictError(TaskLoopActivationError):
    code = "TASK_LOOP_ACTIVATION_CONFLICT"


class TaskLoopActivationRuntime:
    """Seal Plan, Run, node authority bindings and activation in one commit."""

    def __init__(
        self,
        database: Database,
        task_loops: MultiStepPlanRuntime,
        turn_planner: TurnPlannerRuntime,
        planning: PlanCompilationService,
        execution: AgentExecutionRuntime,
        node_binder: ModelPlannerNodeBinder,
        *,
        command_plans: WorkspaceCommandPlanBinder | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._task_loops = task_loops
        self._turn_planner = turn_planner
        self._planning = planning
        self._execution = execution
        self._node_binder = node_binder
        self._command_plans = command_plans
        self._clock = clock or (lambda: datetime.now(UTC))

    async def activate(self, task_id: str) -> TaskLoopExecution:
        """Activate only a planned Task Loop; no Provider or executor is invoked."""

        try:
            bundle = await self._task_loops.get_bundle(task_id)
            if bundle is None:
                raise TaskLoopActivationNotFoundError("Task has no model-planner Task Loop")
            if bundle.loop.status != "planned" or bundle.draft is None:
                raise TaskLoopActivationNotEligibleError(
                    "Task Loop does not contain a sealed planned Draft"
                )
            revalidated = await self._turn_planner.revalidate_task_loop_plan(task_id)
            if bundle.loop.source.turn_plan_binding_digest != (
                revalidated.planning.binding.binding_digest
                if revalidated.planning.binding is not None
                else None
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Deferred Turn Planner lineage changed before activation"
                )
            bindings = self._node_binder.bind(
                bundle.draft,
                bundle.steps,
                revalidated,
            )
            self._assert_current_command_plans(bundle, revalidated.steps)
        except TaskLoopActivationError:
            raise
        except (
            MultiStepPlanRuntimeError,
            TurnPlannerRuntimeError,
            ModelPlannerNodeBindingError,
            WorkspaceCommandPlanBindingError,
            ValidationError,
            ValueError,
        ) as error:
            raise TaskLoopActivationProofRejectedError(
                "Task-loop activation preflight proof was rejected"
            ) from error

        try:
            async with self._database.session() as session, session.begin():
                loop_record = await session.scalar(
                    select(TaskLoopRecord)
                    .where(TaskLoopRecord.loop_id == bundle.loop.loop_id)
                    .with_for_update()
                )
                draft_record = await session.scalar(
                    select(ModelPlannerDraftRecord)
                    .where(ModelPlannerDraftRecord.draft_id == bundle.draft.draft_id)
                    .with_for_update()
                )
                if loop_record is None or draft_record is None:
                    raise TaskLoopActivationNotFoundError(
                        "Task Loop or sealed Draft disappeared before activation"
                    )
                self._assert_locked_source(loop_record, draft_record, bundle.loop, bundle.draft)
                existing = await session.scalar(
                    select(TaskLoopExecutionRecord)
                    .where(TaskLoopExecutionRecord.loop_id == bundle.loop.loop_id)
                    .with_for_update()
                )
                if existing is not None:
                    return await self._read_exact(
                        session,
                        existing,
                        draft=bundle.draft,
                        expected_bindings=bindings,
                    )

                # Recheck current registry eligibility at the write boundary.
                # Registries are in-memory and this call performs no external I/O.
                bindings = self._node_binder.bind(
                    bundle.draft,
                    bundle.steps,
                    revalidated,
                )
                self._assert_current_command_plans(bundle, revalidated.steps)
                activated_plan = await self._planning.activate_initial_once_in_session(
                    session,
                    bundle.draft.task_contract,
                    bundle.draft.draft_plan,
                )
                if activated_plan.plan != bundle.draft.expected_plan:
                    raise TaskLoopActivationProofRejectedError(
                        "Activated generation-1 Plan differs from the sealed preview"
                    )
                run = await self._execution.start_exact_in_session(
                    session,
                    bundle.draft.expected_plan,
                )
                created_at = self._now()
                execution, event = TaskLoopExecution.activate(
                    loop_id=bundle.loop.loop_id,
                    draft_id=bundle.draft.draft_id,
                    task_id=task_id,
                    plan_id=bundle.draft.expected_plan.plan_id,
                    plan_manifest_digest=(bundle.draft.expected_plan_manifest_digest),
                    run_id=run.run_id,
                    bindings=bindings,
                    created_at=created_at,
                )
                session.add(self._execution_record(execution))
                await session.flush()
                session.add_all(
                    self._binding_record(item, execution.execution_id, created_at)
                    for item in bindings
                )
                session.add(self._event_record(event))
                await session.flush()
                return await self._read_exact(
                    session,
                    await self._required_execution_record(
                        session,
                        execution.execution_id,
                    ),
                    draft=bundle.draft,
                    expected_bindings=bindings,
                )
        except IntegrityError:
            # SQLite has no row-level FOR UPDATE. Uniqueness elects one writer;
            # a concurrent loser may accept only the fully matching winner.
            persisted_read = await self.get(task_id)
            if persisted_read is None or persisted_read.execution is None:
                raise TaskLoopActivationConflictError(
                    "Concurrent Task Loop activation did not converge"
                ) from None
            await self._assert_persisted_exact(task_id, bundle.draft, bindings)
            return persisted_read.execution
        except TaskLoopActivationError:
            raise
        except (PlanningError, AgentRuntimeError) as error:
            raise TaskLoopActivationProofRejectedError(
                "Atomic Plan or Run activation was rejected"
            ) from error

    async def cancel_for_amendment(self, task_id: str) -> TaskLoopExecutionRead | None:
        """Fence the old Run first, then seal its TaskLoop execution as cancelled."""

        current = await self.get(task_id)
        if current is None or current.execution is None:
            return current
        execution = current.execution
        if execution.status in {"failed", "succeeded", "cancelled"}:
            return current
        # This is deliberately first: once the Run is cancelled every stale
        # worker mutation fails its Run/node/fencing checks, even if the process
        # stops before the TaskLoop event is appended.
        await self._execution.cancel(execution.run_id)
        async with self._database.session() as session, session.begin():
            record = await session.scalar(
                select(TaskLoopExecutionRecord)
                .where(
                    TaskLoopExecutionRecord.execution_id
                    == execution.execution_id
                )
                .with_for_update()
            )
            if record is None:
                raise TaskLoopActivationNotFoundError(
                    "Task Loop execution disappeared during amendment"
                )
            persisted = TaskLoopExecution.model_validate(record.manifest)
            if persisted.status != "cancelled":
                if (
                    persisted.execution_digest != execution.execution_digest
                    or persisted.status in {"failed", "succeeded"}
                ):
                    raise TaskLoopActivationConflictError(
                        "Task Loop execution changed during amendment fencing"
                    )
                now = self._now()
                invocations = tuple(
                    (
                        await session.scalars(
                            select(AgentInvocationRecord).where(
                                AgentInvocationRecord.run_id == execution.run_id
                            )
                        )
                    ).all()
                )
                invocation_ids = tuple(item.invocation_id for item in invocations)
                unknown_turns: tuple[AgentModelTurnRecord, ...] = ()
                if invocation_ids:
                    unknown_turns = tuple(
                        (
                            await session.scalars(
                                select(AgentModelTurnRecord)
                                .where(
                                    AgentModelTurnRecord.invocation_id.in_(
                                        invocation_ids
                                    ),
                                    AgentModelTurnRecord.status == "dispatching",
                                )
                                .with_for_update()
                            )
                        ).all()
                    )
                unknown_invocations = {
                    item.invocation_id for item in unknown_turns
                }
                for turn in unknown_turns:
                    turn.status = "outcome_unknown"
                    turn.stable_error_code = (
                        "CONTRACT_AMENDMENT_DURING_DISPATCH"
                    )
                    turn.updated_at = now
                    dispatch = await session.scalar(
                        select(ModelDispatchAttemptRecord)
                        .where(
                            ModelDispatchAttemptRecord.turn_id == turn.turn_id,
                            ModelDispatchAttemptRecord.status == "dispatching",
                        )
                        .with_for_update()
                    )
                    if dispatch is not None:
                        dispatch.status = "outcome_unknown"
                        dispatch.stable_error_code = (
                            "CONTRACT_AMENDMENT_DURING_DISPATCH"
                        )
                        dispatch.updated_at = now
                attempts = tuple(
                    (
                        await session.scalars(
                            select(TaskLoopNodeAttemptRecord)
                            .where(
                                TaskLoopNodeAttemptRecord.execution_id
                                == execution.execution_id,
                                TaskLoopNodeAttemptRecord.status.in_(
                                    (
                                        "prepared",
                                        "claimed",
                                        "running",
                                        "awaiting_verification",
                                    )
                                ),
                            )
                            .with_for_update()
                        )
                    ).all()
                )
                invocation_by_attempt = {
                    (item.node_id, item.attempt): item for item in invocations
                }
                for attempt_record in attempts:
                    attempt = self._attempt_from_record(attempt_record)
                    invocation = invocation_by_attempt.get(
                        (attempt.node_id, attempt.attempt)
                    )
                    outcome_unknown = bool(
                        invocation is not None
                        and invocation.invocation_id in unknown_invocations
                    )
                    error_code = (
                        "CONTRACT_AMENDMENT_OUTCOME_UNKNOWN"
                        if outcome_unknown
                        else "CONTRACT_AMENDMENT_CANCELLED"
                    )
                    material = attempt.model_dump(
                        mode="python",
                        exclude={"attempt_digest"},
                    )
                    material.update(
                        {
                            "status": (
                                "outcome_unknown"
                                if outcome_unknown
                                else "cancelled"
                            ),
                            "revision": attempt.revision + 1,
                            "claim_owner_id": None,
                            "claim_acquired_at": None,
                            "claim_expires_at": None,
                            "error_code": error_code,
                            "error_digest": sha256_digest(
                                {
                                    "attempt_id": attempt.attempt_id,
                                    "error_code": error_code,
                                }
                            ),
                            "updated_at": now,
                        }
                    )
                    amended = TaskLoopNodeAttempt.model_validate(
                        {
                            **material,
                            "attempt_digest": sha256_digest(material),
                        }
                    )
                    self._apply_attempt_record(attempt_record, amended)
                cancelled, event = persisted.transition(
                    status="cancelled",
                    kind="cancelled",
                    updated_at=now,
                )
                self._apply_execution_record(record, cancelled)
                session.add(self._event_record(event))
        return await self.get(task_id)

    async def bind_conversation_amendment(
        self,
        source_task_id: str,
        successor_task_id: str,
    ) -> WorkspaceCodingAmendmentBinding:
        """Bind an already fenced generation to one same-conversation user turn."""

        current = await self.get(source_task_id)
        if (
            current is None
            or current.execution is None
            or current.execution.status != "cancelled"
        ):
            raise TaskLoopActivationNotEligibleError(
                "Workspace coding amendment source is not terminal cancelled"
            )
        execution = current.execution
        try:
            async with self._database.session() as session, session.begin():
                source_execution = await session.scalar(
                    select(TaskLoopExecutionRecord)
                    .where(
                        TaskLoopExecutionRecord.execution_id
                        == execution.execution_id
                    )
                    .with_for_update()
                )
                if source_execution is None:
                    raise TaskLoopActivationNotFoundError(
                        "Workspace coding amendment source execution is missing"
                    )
                persisted_execution = TaskLoopExecution.model_validate(
                    source_execution.manifest
                )
                if (
                    persisted_execution != execution
                    or persisted_execution.status != "cancelled"
                ):
                    raise TaskLoopActivationConflictError(
                        "Workspace coding amendment source execution changed"
                    )
                existing = await session.scalar(
                    select(WorkspaceCodingAmendmentBindingRecord)
                    .where(
                        WorkspaceCodingAmendmentBindingRecord.source_execution_id
                        == execution.execution_id
                    )
                    .with_for_update()
                )
                existing_binding: WorkspaceCodingAmendmentBinding | None = None
                if existing is not None:
                    existing_binding = self._amendment_from_record(existing)
                    if (
                        existing_binding.source_task_id != source_task_id
                        or existing_binding.successor_task_id != successor_task_id
                    ):
                        raise TaskLoopActivationConflictError(
                            "Workspace coding generation already has another successor"
                        )

                source_task = await session.get(TaskRecord, source_task_id)
                successor_task = await session.get(TaskRecord, successor_task_id)
                planning_state = await session.get(
                    TaskPlanningStateRecord,
                    source_task_id,
                )
                if (
                    source_task is None
                    or successor_task is None
                    or planning_state is None
                    or source_task.conversation_id is None
                    or successor_task.conversation_id != source_task.conversation_id
                    or source_task_id == successor_task_id
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment is not in one conversation"
                    )
                contract_record = await session.get(
                    TaskContractVersionRecord,
                    (source_task_id, planning_state.active_contract_version),
                )
                plan_record = await session.get(
                    TaskPlanGenerationRecord,
                    (source_task_id, planning_state.active_plan_generation),
                )
                if (
                    contract_record is None
                    or plan_record is None
                    or plan_record.status != "active"
                    or contract_record.contract_digest
                    != planning_state.active_contract_digest
                    or plan_record.plan_manifest_digest
                    != planning_state.active_plan_digest
                    or execution.plan_generation != plan_record.generation
                    or execution.plan_id != plan_record.plan_id
                    or execution.plan_manifest_digest
                    != plan_record.plan_manifest_digest
                    or plan_record.contract_version != contract_record.version
                    or plan_record.contract_digest != contract_record.contract_digest
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment planning lineage changed"
                    )
                contract = TaskContract.model_validate(contract_record.manifest)
                if (
                    contract.task_id != source_task_id
                    or contract.version != contract_record.version
                    or contract.contract_id != contract_record.contract_id
                    or contract.previous_contract_digest
                    != contract_record.previous_contract_digest
                    or contract.digest != contract_record.contract_digest
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment Contract proof changed"
                    )
                plan = ExecutablePlan.model_validate(plan_record.manifest)
                if (
                    plan.plan_manifest_digest != plan_record.plan_manifest_digest
                    or plan.task_id != source_task_id
                    or plan.task_contract.version != contract_record.version
                    or plan.task_contract.digest != contract_record.contract_digest
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment Plan proof changed"
                    )
                terminal_event_record = await session.get(
                    TaskLoopExecutionEventRecord,
                    execution.latest_event_id,
                )
                if terminal_event_record is None:
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment terminal event is missing"
                    )
                terminal_event = self._event_from_record(terminal_event_record)
                if (
                    terminal_event.execution_id != execution.execution_id
                    or terminal_event.kind != "cancelled"
                    or terminal_event.event_digest != execution.latest_event_digest
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment terminal event changed"
                    )
                successor_route = await session.get(TurnRouteRecord, successor_task_id)
                if (
                    successor_route is None
                    or successor_route.conversation_id != source_task.conversation_id
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment successor route is missing"
                    )
                successor_message = await session.get(
                    ConversationMessageRecord,
                    successor_route.user_message_id,
                )
                if successor_message is None:
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment successor message is missing"
                    )
                message_material = {
                    "message_id": successor_message.message_id,
                    "conversation_id": successor_message.conversation_id,
                    "task_id": successor_message.task_id,
                    "role": successor_message.role,
                    "content": successor_message.content,
                    "content_ref": successor_message.content_ref,
                    "classification": successor_message.classification,
                    "created_at": self._aware(successor_message.created_at),
                }
                if (
                    successor_message.conversation_id
                    != source_task.conversation_id
                    or successor_message.task_id != successor_task_id
                    or successor_message.role != "user"
                    or successor_message.status != "active"
                    or successor_message.content is None
                    or successor_message.content_ref is not None
                    or successor_task.goal != successor_message.content
                    or successor_message.message_digest
                    != sha256_digest(message_material)
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Workspace coding amendment successor message changed"
                    )
                binding = WorkspaceCodingAmendmentBinding.build(
                    conversation_id=source_task.conversation_id,
                    source_task_id=source_task_id,
                    source_execution_id=execution.execution_id,
                    source_contract_version=contract_record.version,
                    source_contract_digest=contract_record.contract_digest,
                    source_plan_generation=plan_record.generation,
                    source_plan_digest=plan_record.plan_manifest_digest,
                    source_execution_digest=execution.execution_digest,
                    source_execution_event_digest=terminal_event.event_digest,
                    successor_task_id=successor_task_id,
                    successor_user_message_id=successor_message.message_id,
                    successor_user_message_digest=successor_message.message_digest,
                    created_at=(
                        existing_binding.created_at
                        if existing_binding is not None
                        else self._now()
                    ),
                )
                if existing_binding is not None:
                    if binding != existing_binding:
                        raise TaskLoopActivationProofRejectedError(
                            "Workspace coding amendment referent proof changed"
                        )
                    return existing_binding
                session.add(self._amendment_record(binding))
                await session.flush()
                return binding
        except IntegrityError as error:
            raise TaskLoopActivationConflictError(
                "Workspace coding amendment was bound concurrently"
            ) from error
        except TaskLoopActivationError:
            raise
        except (ValidationError, ValueError) as error:
            raise TaskLoopActivationProofRejectedError(
                "Workspace coding amendment proof was rejected"
            ) from error

    def _assert_current_command_plans(
        self,
        bundle: ModelPlannerTaskLoopBundle,
        current_steps: tuple[RevalidatedOfferStep, ...],
    ) -> None:
        has_command = any(
            item.route.route_id == "workspace_command_profile"
            for item in current_steps
        )
        if not has_command:
            if bundle.command_plans:
                raise WorkspaceCommandPlanBindingError(
                    "Persisted Workspace command Plan lost its current Offer"
                )
            return
        if bundle.draft is None or self._command_plans is None:
            raise WorkspaceCommandPlanBindingError(
                "Workspace command Plan binder is unavailable at activation"
            )
        current = self._command_plans.bind(
            loop_id=bundle.loop.loop_id,
            draft=bundle.draft,
            steps=bundle.steps,
            current_steps=current_steps,
        )
        if current != bundle.command_plans:
            raise WorkspaceCommandPlanBindingError(
                "Workspace command Plan changed before activation"
            )

    async def get(self, task_id: str) -> TaskLoopExecutionRead | None:
        """Reconstruct one proof-checked internal read without external I/O."""

        try:
            bundle = await self._task_loops.get_bundle(task_id)
        except (MultiStepPlanRuntimeError, ValidationError, ValueError) as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted Task Loop proof was rejected during execution recovery"
            ) from error
        if bundle is None:
            return None
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopExecutionRecord)
                        .where(TaskLoopExecutionRecord.task_id == task_id)
                        .order_by(TaskLoopExecutionRecord.created_at)
                    )
                ).all()
            )
            if bundle.loop.status != "planned" or bundle.draft is None:
                if records:
                    raise TaskLoopActivationProofRejectedError(
                        "Unplanned Task Loop unexpectedly has an execution"
                    )
                return self._pre_execution_read(bundle.loop, draft=None, command_plans=())
            if not records:
                return self._pre_execution_read(
                    bundle.loop,
                    draft=bundle.draft,
                    command_plans=bundle.command_plans,
                )
            if len(records) != 1:
                raise TaskLoopActivationProofRejectedError(
                    "Task has more than one model-planner execution"
                )
            return await self._read_internal(
                session,
                records[0],
                loop=bundle.loop,
                draft=bundle.draft,
                command_plans=bundle.command_plans,
            )

    async def recoverable_task_ids(self, *, limit: int = 100) -> tuple[str, ...]:
        """Return bounded, revalidated nonterminal Task Loop task ids."""

        if not 1 <= limit <= 1_000:
            raise ValueError("Task Loop execution recovery limit is invalid")
        async with self._database.session() as session:
            candidates = tuple(
                (
                    await session.scalars(
                        select(TaskLoopRecord.task_id)
                        .outerjoin(
                            TaskLoopExecutionRecord,
                            TaskLoopExecutionRecord.loop_id == TaskLoopRecord.loop_id,
                        )
                        .where(
                            or_(
                                TaskLoopRecord.status == "observed",
                                and_(
                                    TaskLoopRecord.status == "planned",
                                    or_(
                                        TaskLoopExecutionRecord.execution_id.is_(None),
                                        TaskLoopExecutionRecord.status.in_(
                                            (
                                                "active",
                                                "paused",
                                                "awaiting_user",
                                                "repairing",
                                            )
                                        ),
                                    ),
                                ),
                            )
                        )
                        .order_by(TaskLoopRecord.updated_at, TaskLoopRecord.task_id)
                        .limit(limit)
                    )
                ).all()
            )
        recovered: list[str] = []
        for candidate in candidates:
            read = await self.get(candidate)
            if read is None:
                raise TaskLoopActivationProofRejectedError(
                    "Recovery candidate disappeared during proof reconstruction"
                )
            if read.recoverable:
                recovered.append(candidate)
        return tuple(dict.fromkeys(recovered))

    def _pre_execution_read(
        self,
        loop: TaskLoop,
        *,
        draft: ModelPlannerDraft | None,
        command_plans: tuple[WorkspaceCommandPlanBinding, ...],
    ) -> TaskLoopExecutionRead:
        nodes: tuple[TaskLoopExecutionNodeRead, ...] = ()
        if draft is not None:
            command_nodes = self._command_node_projection(command_plans)
            nodes = tuple(
                TaskLoopExecutionNodeRead.build(
                    node_id=node.node_id,
                    local_key=node.local_key,
                    kind=node.kind,
                    status="ready" if not node.depends_on else "pending",
                    depends_on=node.depends_on,
                    verified_dependency_node_ids=(),
                    dependency_count=len(node.depends_on),
                    verified_dependency_count=0,
                    dependencies_verified=not node.depends_on,
                    attempt_count=0,
                    max_attempts=node.budget.retries + 1,
                    candidate_present=False,
                    verified_result_present=False,
                    **command_nodes.get(node.node_id, {}),
                    created_at=loop.updated_at,
                    updated_at=loop.updated_at,
                )
                for node in sorted(
                    draft.expected_plan.nodes,
                    key=lambda item: item.local_key,
                )
            )
        return TaskLoopExecutionRead.build(
            task_id=loop.source.task_id,
            loop_id=loop.loop_id,
            loop_status=loop.status,
            phase=loop.phase,
            loop_revision=loop.revision,
            loop_event_count=loop.event_count,
            loop_progress_digest=loop.progress_digest,
            execution=None,
            nodes=nodes,
            recoverable=loop.status in {"observed", "planned"},
            created_at=loop.created_at,
            updated_at=loop.updated_at,
        )

    @staticmethod
    def _command_node_projection(
        command_plans: tuple[WorkspaceCommandPlanBinding, ...],
    ) -> dict[str, dict[str, object]]:
        result: dict[str, dict[str, object]] = {}
        for binding in command_plans:
            for mapping in binding.mappings:
                if mapping.composite_node_id in result:
                    raise TaskLoopActivationProofRejectedError(
                        "Task Loop command projection repeats one node"
                    )
                step = binding.command_plan.steps[mapping.command_step_sequence - 1]
                if (
                    step.step_id != mapping.command_step_id
                    or step.step_digest != mapping.command_step_digest
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Task Loop command projection changed its exact step"
                    )
                result[mapping.composite_node_id] = {
                    "command_plan_id": binding.command_plan.plan_id,
                    "command_step_sequence": step.sequence,
                    "command_profile_id": step.command_profile.command_profile_id,
                }
        return result

    async def _read_internal(
        self,
        session: AsyncSession,
        record: TaskLoopExecutionRecord,
        *,
        loop: TaskLoop,
        draft: ModelPlannerDraft,
        command_plans: tuple[WorkspaceCommandPlanBinding, ...],
    ) -> TaskLoopExecutionRead:
        execution = await self._read_exact(
            session,
            record,
            draft=draft,
            expected_bindings=None,
        )
        binding_records = tuple(
            (
                await session.scalars(
                    select(ModelPlannerNodeBindingRecord)
                    .where(ModelPlannerNodeBindingRecord.execution_id == execution.execution_id)
                    .order_by(ModelPlannerNodeBindingRecord.composite_node_id)
                )
            ).all()
        )
        bindings = tuple(self._binding_from_record(item) for item in binding_records)
        bindings_by_id = {item.node_binding_id: item for item in bindings}
        bindings_by_node = {item.composite_node_id: item for item in bindings}
        command_nodes = self._command_node_projection(command_plans)
        if len(bindings_by_id) != len(bindings) or len(bindings_by_node) != len(bindings):
            raise TaskLoopActivationProofRejectedError("Task Loop execution repeats a node binding")

        node_records = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord)
                    .where(TaskExecutionNodeRecord.run_id == execution.run_id)
                    .order_by(TaskExecutionNodeRecord.local_key)
                )
            ).all()
        )
        nodes_by_id = {item.node_id: item for item in node_records}
        attempt_records = tuple(
            (
                await session.scalars(
                    select(TaskLoopNodeAttemptRecord)
                    .where(TaskLoopNodeAttemptRecord.execution_id == execution.execution_id)
                    .order_by(
                        TaskLoopNodeAttemptRecord.node_id,
                        TaskLoopNodeAttemptRecord.attempt,
                    )
                )
            ).all()
        )
        attempts: list[TaskLoopNodeAttempt] = []
        attempts_by_node: dict[str, list[TaskLoopNodeAttempt]] = {}
        attempts_by_id: dict[str, TaskLoopNodeAttempt] = {}
        for attempt_record in attempt_records:
            attempt = self._attempt_from_record(attempt_record)
            binding = bindings_by_id.get(attempt.node_binding_id)
            if (
                attempt.execution_id != execution.execution_id
                or attempt.run_id != execution.run_id
                or binding is None
                or binding.composite_node_id != attempt.node_id
                or attempt.node_id not in nodes_by_id
                or attempt.attempt_id in attempts_by_id
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop attempt crosses its exact execution binding"
                )
            attempts.append(attempt)
            attempts_by_id[attempt.attempt_id] = attempt
            attempts_by_node.setdefault(attempt.node_id, []).append(attempt)

        approval_records = tuple(
            (
                await session.scalars(
                    select(TaskLoopCapabilityApprovalRecord)
                    .where(
                        TaskLoopCapabilityApprovalRecord.execution_id
                        == execution.execution_id
                    )
                    .order_by(TaskLoopCapabilityApprovalRecord.created_at)
                )
            ).all()
        )
        approvals_by_node: dict[str, TaskLoopCapabilityApproval] = {}
        for approval_record in approval_records:
            approval = self._approval_from_record(approval_record)
            approval_attempt = attempts_by_id.get(approval.attempt_id)
            binding = bindings_by_id.get(approval.node_binding_id)
            if (
                approval.execution_id != execution.execution_id
                or approval.task_id != execution.task_id
                or approval.run_id != execution.run_id
                or approval.plan_generation != execution.plan_generation
                or approval_attempt is None
                or approval_attempt.node_id != approval.node_id
                or approval_attempt.attempt != approval.attempt
                or approval_attempt.input_digest != approval.input_binding_digest
                or binding is None
                or binding.composite_node_id != approval.node_id
                or approval.node_id in approvals_by_node
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop capability approval crossed its execution binding"
                )
            approvals_by_node[approval.node_id] = approval

        result_records = tuple(
            (
                await session.scalars(
                    select(TaskLoopVerifiedResultRecord)
                    .where(TaskLoopVerifiedResultRecord.execution_id == execution.execution_id)
                    .order_by(
                        TaskLoopVerifiedResultRecord.node_id,
                        TaskLoopVerifiedResultRecord.created_at,
                    )
                )
            ).all()
        )
        results_by_node: dict[str, TaskLoopVerifiedResult] = {}
        failure_results_by_node: dict[str, list[TaskLoopVerifiedResult]] = {}
        for result_record in result_records:
            result = self._verified_result_from_record(result_record)
            result_attempt = attempts_by_id.get(result.attempt_id)
            binding = bindings_by_id.get(result.node_binding_id)
            if result_attempt is None or binding is None:
                raise TaskLoopActivationProofRejectedError(
                    "Verified ResultRef lost its attempt or node binding"
                )
            self._validate_verified_result(
                execution=execution,
                result=result,
                attempt=result_attempt,
                binding=binding,
            )
            if result_attempt.status == "failed":
                failure_results_by_node.setdefault(result.node_id, []).append(result)
                continue
            if result.node_id in results_by_node:
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop node has more than one verified ResultRef"
                )
            results_by_node[result.node_id] = result

        cycle_records = tuple(
            (
                await session.scalars(
                    select(TaskLoopCycleEventRecord)
                    .where(
                        TaskLoopCycleEventRecord.execution_id
                        == execution.execution_id
                    )
                    .order_by(TaskLoopCycleEventRecord.sequence)
                )
            ).all()
        )
        cycle_events = tuple(
            self._cycle_event_from_record(item) for item in cycle_records
        )
        for index, event in enumerate(cycle_events, start=1):
            previous = cycle_events[index - 2] if index > 1 else None
            if (
                event.sequence != index
                or event.execution_id != execution.execution_id
                or event.task_id != execution.task_id
                or event.plan_generation != execution.plan_generation
                or event.previous_event_digest
                != (previous.event_digest if previous is not None else None)
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop cycle event chain changed"
                )

        node_reads: list[TaskLoopExecutionNodeRead] = []
        for node in node_records:
            node_attempts = attempts_by_node.get(node.node_id, [])
            if [item.attempt for item in node_attempts] != list(range(1, node.attempt_count + 1)):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop node attempt sequence is incomplete"
                )
            binding = bindings_by_node.get(node.node_id)
            if node.node_kind in {
                DraftNodeKind.AGENT.value,
                DraftNodeKind.CAPABILITY.value,
            }:
                if binding is None:
                    raise TaskLoopActivationProofRejectedError(
                        "Runnable Task Loop node lost its exact binding"
                    )
            elif (
                binding is not None
                or node_attempts
                or node.node_id in results_by_node
                or node.node_id in failure_results_by_node
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Control node contains dispatch or ResultRef evidence"
                )

            latest_attempt = node_attempts[-1] if node_attempts else None
            verified_result = results_by_node.get(node.node_id)
            failure_results = tuple(failure_results_by_node.get(node.node_id, ()))
            node_approval = approvals_by_node.get(node.node_id)
            if node_approval is not None and latest_attempt is not None:
                valid_approval_state = (
                    node_approval.status == "pending"
                    and execution.status == "awaiting_user"
                    and node.status == "waiting_user"
                    and latest_attempt.status == "prepared"
                ) or (
                    node_approval.status == "approved"
                    and execution.status == "active"
                    and node.status in {"ready", "running"}
                    and latest_attempt.status in {"prepared", "running"}
                ) or (
                    node_approval.status == "consumed"
                    and node.status in {"awaiting_verification", "verified"}
                    and latest_attempt.status in {"awaiting_verification", "verified"}
                )
                if not valid_approval_state:
                    raise TaskLoopActivationProofRejectedError(
                        "Task Loop capability approval lifecycle changed: "
                        f"approval={node_approval.status}, execution={execution.status}, "
                        f"node={node.status}, attempt={latest_attempt.status}"
                    )
            if any(
                item.status == "verified"
                and item.attempt_id
                != (verified_result.attempt_id if verified_result is not None else None)
                for item in node_attempts
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Verified attempt has no exact immutable ResultRef"
                )
            if verified_result is not None and (
                latest_attempt is None
                or latest_attempt.attempt_id != verified_result.attempt_id
                or latest_attempt.status != "verified"
                or node.status != "verified"
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Verified ResultRef differs from the current node state"
                )
            if node.status == "verified" and (
                node.node_kind in {DraftNodeKind.AGENT.value, DraftNodeKind.CAPABILITY.value}
                and verified_result is None
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Runnable verified node has no verified ResultRef"
                )

            verified_dependencies = tuple(
                dependency_id
                for dependency_id in node.depends_on
                if self._dependency_is_verified(
                    dependency_id,
                    nodes_by_id=nodes_by_id,
                    results_by_node=results_by_node,
                )
            )
            dependencies_verified = len(verified_dependencies) == len(node.depends_on)
            if (
                node.status
                in {
                    "ready",
                    "claimed",
                    "running",
                    "awaiting_verification",
                    "verified",
                    "waiting_user",
                    "waiting_children",
                }
                and not dependencies_verified
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Node advanced without verified dependency ResultRefs"
                )
            candidate_present = bool(
                latest_attempt is not None
                and latest_attempt.status == "awaiting_verification"
                and latest_attempt.candidate_manifest is not None
                and latest_attempt.verification_manifest is None
                and verified_result is None
            )
            if latest_attempt is not None and latest_attempt.candidate_manifest is not None:
                if (
                    latest_attempt.status == "awaiting_verification"
                    and verified_result is None
                    and not candidate_present
                ):
                    raise TaskLoopActivationProofRejectedError(
                        "Unverified candidate evidence is incomplete"
                    )
            max_attempts = int(node.budget["retries"]) + 1
            node_reads.append(
                TaskLoopExecutionNodeRead.build(
                    node_id=node.node_id,
                    local_key=node.local_key,
                    kind=DraftNodeKind(node.node_kind),
                    status=node.status,
                    depends_on=tuple(node.depends_on),
                    verified_dependency_node_ids=verified_dependencies,
                    dependency_count=len(node.depends_on),
                    verified_dependency_count=len(verified_dependencies),
                    dependencies_verified=dependencies_verified,
                    attempt_count=node.attempt_count,
                    max_attempts=max_attempts,
                    candidate_present=candidate_present,
                    verified_result_present=verified_result is not None,
                    verified_failure_result_count=len(failure_results),
                    **command_nodes.get(node.node_id, {}),
                    created_at=self._aware(node.created_at),
                    updated_at=self._aware(node.updated_at),
                )
            )

        phase = self._execution_phase(execution, tuple(node_reads))
        node_state_digest = sha256_digest(
            {
                "nodes": [
                    {
                        "node_id": item.node_id,
                        "status": item.status,
                        "revision": item.revision,
                        "attempt_count": item.attempt_count,
                    }
                    for item in sorted(node_records, key=lambda value: value.node_id)
                ]
            }
        )
        latest_cycle = cycle_events[-1] if cycle_events else None
        no_progress_count = 0
        if latest_cycle is not None and (
            latest_cycle.evidence_manifest.get("node_state_digest")
            == node_state_digest
        ):
            if latest_cycle.kind == "no_progress_observed":
                value = latest_cycle.evidence_manifest.get("observation_count")
                if not isinstance(value, int) or not 1 <= value <= 3:
                    raise TaskLoopActivationProofRejectedError(
                        "Task Loop no-progress counter proof changed"
                    )
                no_progress_count = value
            elif latest_cycle.kind == "no_progress_terminated":
                no_progress_count = 3
        cycle = TaskLoopCycleRead.build(
            no_progress_count=no_progress_count,
            repair_count=sum(item.kind == "repair_started" for item in cycle_events),
            budget_exhausted=any(
                item.kind == "budget_exhausted" for item in cycle_events
            ),
            latest_event_kind=(latest_cycle.kind if latest_cycle is not None else None),
            latest_event_sequence=(latest_cycle.sequence if latest_cycle is not None else 0),
        )
        updated_at = max(
            loop.updated_at,
            execution.updated_at,
            *(item.updated_at for item in node_reads),
            *(item.updated_at for item in approvals_by_node.values()),
        )
        workspace_patch: WorkspacePatchPreview | WorkspacePatchReceipt | None = None
        git_commit: GitCommitPreview | GitCommitReceipt | None = None
        for approval in sorted(
            approvals_by_node.values(),
            key=lambda item: (item.updated_at, item.approval_id),
        ):
            try:
                preview = parse_task_loop_capability_preview(
                    approval.preview_manifest,
                    expected_schema_digest=approval.preview_schema_digest,
                )
            except ValueError as error:
                raise TaskLoopActivationProofRejectedError(
                    "Capability approval preview proof changed"
                ) from error
            attempt = attempts_by_id[approval.attempt_id]
            receipt: object | None = None
            if approval.status == "consumed":
                if attempt.candidate_manifest is None:
                    raise TaskLoopActivationProofRejectedError(
                        "Consumed capability approval has no durable candidate"
                    )
                candidate_output = attempt.candidate_manifest.get("output_manifest")
                receipt = (
                    candidate_output.get("receipt")
                    if isinstance(candidate_output, dict)
                    else None
                )
            if isinstance(preview, WorkspacePatchPreview):
                workspace_patch = preview
                if approval.status != "consumed":
                    continue
                try:
                    workspace_patch = WorkspacePatchReceipt.model_validate(receipt)
                except ValidationError as error:
                    raise TaskLoopActivationProofRejectedError(
                        "Consumed patch approval receipt proof changed"
                    ) from error
            elif isinstance(preview, GitCommitPreview):
                git_commit = preview
                if approval.status != "consumed":
                    continue
                try:
                    git_commit = GitCommitReceipt.model_validate(receipt)
                except ValidationError as error:
                    raise TaskLoopActivationProofRejectedError(
                        "Consumed Git approval receipt proof changed"
                    ) from error
        delivery_record = await session.scalar(
            select(WorkspaceCodingDeliveryRecord).where(
                WorkspaceCodingDeliveryRecord.execution_id
                == execution.execution_id
            )
        )
        coding_delivery = (
            self._coding_delivery_read(delivery_record, execution)
            if delivery_record is not None
            else None
        )
        is_coding_loop = any(
            item.recipe.route_id == "workspace_coding_loop" for item in bindings
        )
        if execution.status == "succeeded" and is_coding_loop and coding_delivery is None:
            raise TaskLoopActivationProofRejectedError(
                "Succeeded workspace coding loop has no delivery evidence"
            )
        if delivery_record is not None:
            updated_at = max(updated_at, self._aware(delivery_record.created_at))
        return TaskLoopExecutionRead.build(
            task_id=execution.task_id,
            loop_id=execution.loop_id,
            loop_status=loop.status,
            phase=phase,
            loop_revision=loop.revision,
            loop_event_count=loop.event_count,
            loop_progress_digest=loop.progress_digest,
            execution=execution,
            cycle=cycle,
            workspace_patch=workspace_patch,
            git_commit=git_commit,
            coding_delivery=coding_delivery,
            nodes=tuple(node_reads),
            recoverable=execution.status in {"active", "paused", "awaiting_user", "repairing"},
            created_at=loop.created_at,
            updated_at=updated_at,
        )

    @classmethod
    def _coding_delivery_read(
        cls,
        record: WorkspaceCodingDeliveryRecord,
        execution: TaskLoopExecution,
    ) -> WorkspaceCodingDeliveryWorkbenchRead:
        manifest = record.manifest
        if (
            record.execution_id != execution.execution_id
            or record.task_id != execution.task_id
            or record.run_id != execution.run_id
            or record.plan_id != execution.plan_id
            or record.plan_manifest_digest != execution.plan_manifest_digest
            or manifest.get("schema_version")
            not in {
                "deskpilot.workspace-coding-delivery.v1",
                "deskpilot.workspace-coding-delivery.v2",
                "deskpilot.workspace-coding-delivery.v3",
            }
            or manifest.get("delivery_id") != record.delivery_id
            or record.delivery_digest != sha256_digest(manifest)
        ):
            raise TaskLoopActivationProofRejectedError(
                "Workspace coding delivery scope or digest changed"
            )
        try:
            raw_changes = manifest["structured_diff"]
            raw_coordinator = manifest["coordinator_evidence"]
            raw_planners = manifest["patch_planner_evidence"]
            raw_tests = manifest["test_runs"]
            raw_failures = manifest["failure_repair_history"]
            raw_git = manifest.get("git_commit")
            raw_rollbacks = manifest["rollback_points"]
            changed_files = tuple(str(item) for item in manifest["changed_files"])
            risks = tuple(str(item) for item in manifest["remaining_risks"])
            if not all(
                isinstance(items, list)
                for items in (
                    raw_changes,
                    raw_planners,
                    raw_tests,
                    raw_failures,
                    raw_rollbacks,
                )
            ):
                raise ValueError("Coding delivery collections must be arrays")
            changes = tuple(
                WorkspaceCodingChangeRead.model_validate(item)
                for item in raw_changes
            )
            coordinator = WorkspaceCodingCoordinatorEvidenceRead.model_validate(
                {
                    "agent_id": raw_coordinator["agent_id"],
                    "agent_version": raw_coordinator["agent_version"],
                    "node_count": raw_coordinator["node_count"],
                    "output_node_key": raw_coordinator["output_node_key"],
                    "graph_digest": raw_coordinator["graph_digest"],
                    "decision_digest": raw_coordinator["decision_digest"],
                    "verification_digest": raw_coordinator[
                        "verification_digest"
                    ],
                }
            )
            planners = tuple(
                WorkspaceCodingPlannerEvidenceRead.model_validate(
                    {
                        "path": item["path"],
                        "agent_id": item["agent_id"],
                        "agent_version": item["agent_version"],
                        "decision_digest": item["decision_digest"],
                        "verification_digest": item["verification_digest"],
                    }
                )
                for item in raw_planners
                if isinstance(item, dict)
            )
            tests = tuple(
                WorkspaceCodingTestRunRead.model_validate(
                    {
                        "kind": item["result_kind"],
                        "attempt": item["attempt"],
                        "project_path": item["project_path"],
                        "test_path": item["test_path"],
                        "status": item["status"],
                        "exit_code": item["exit_code"],
                    }
                )
                for item in raw_tests
                if isinstance(item, dict)
            )
            failures = tuple(
                WorkspaceCodingFailureRepairRead.model_validate(
                    {
                        "attempt": item["attempt"],
                        "status": item["status"],
                        "failure_receipt_digest": item[
                            "failure_receipt_digest"
                        ],
                    }
                )
                for item in raw_failures
                if isinstance(item, dict)
            )
            delivery_version = manifest["schema_version"]
            if delivery_version in {
                "deskpilot.workspace-coding-delivery.v1",
                "deskpilot.workspace-coding-delivery.v2",
            }:
                if "file_count" in manifest or record.changed_file_count != 2:
                    raise ValueError("Legacy coding delivery file count changed")
            else:
                raw_file_count = manifest.get("file_count")
                if (
                    not isinstance(raw_file_count, int)
                    or isinstance(raw_file_count, bool)
                    or raw_file_count not in range(3, 9)
                    or raw_file_count != record.changed_file_count
                ):
                    raise ValueError("Bounded coding delivery file count changed")
            if delivery_version == "deskpilot.workspace-coding-delivery.v1":
                if raw_git is not None:
                    raise ValueError("Legacy coding delivery cannot contain Git evidence")
                git_commit_evidence = None
            else:
                if not isinstance(raw_git, dict):
                    raise ValueError("Git coding delivery lost its commit evidence")
                git_commit_evidence = WorkspaceCodingGitCommitEvidenceRead.model_validate(
                    {
                        "target_branch": raw_git["target_branch"],
                        "expected_head_oid": raw_git["expected_head_oid"],
                        "commit_oid": raw_git["commit_oid"],
                        "receipt_digest": raw_git["receipt_digest"],
                        "push_disabled": raw_git["push_disabled"],
                        "rollback_available": raw_git["rollback_available"],
                    }
                )
            rollbacks = tuple(
                WorkspaceCodingRollbackPointRead(
                    path=str(item["path"]),
                    available=item.get("backup_relative_path") is not None,
                    previous_version_digest=str(item["previous_version_digest"]),
                    version_digest=str(item["version_digest"]),
                )
                for item in raw_rollbacks
                if isinstance(item, dict)
            )
        except (KeyError, TypeError, ValueError, ValidationError) as error:
            raise TaskLoopActivationProofRejectedError(
                "Workspace coding delivery projection Schema was rejected"
            ) from error
        if (
            len(changes) != record.changed_file_count
            or len(changed_files) != record.changed_file_count
            or len(planners) != record.changed_file_count
            or len(rollbacks) != record.changed_file_count
            or len(tests) != record.test_run_count
            or len(failures) != record.failure_count
            or bool(manifest.get("rollback_available"))
            is not record.rollback_available
        ):
            raise TaskLoopActivationProofRejectedError(
                "Workspace coding delivery projection counts changed"
            )
        return WorkspaceCodingDeliveryWorkbenchRead.build(
            delivery_id=record.delivery_id,
            changed_files=changed_files,
            changes=tuple(sorted(changes, key=lambda item: item.path)),
            coordinator_evidence=coordinator,
            patch_planner_evidence=tuple(
                sorted(planners, key=lambda item: item.path)
            ),
            tests=tuple(sorted(tests, key=lambda item: item.attempt)),
            failure_repair_history=tuple(
                sorted(failures, key=lambda item: item.attempt)
            ),
            git_commit=git_commit_evidence,
            remaining_risks=risks,
            rollback_points=tuple(sorted(rollbacks, key=lambda item: item.path)),
            rollback_available=record.rollback_available,
            evidence_digest=record.delivery_digest,
            created_at=cls._aware(record.created_at),
        )

    @staticmethod
    def _cycle_event_from_record(
        record: TaskLoopCycleEventRecord,
    ) -> TaskLoopCycleEvent:
        try:
            event = TaskLoopCycleEvent.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop cycle event Schema was rejected"
            ) from error
        if (
            record.event_id != event.event_id
            or record.execution_id != event.execution_id
            or record.task_id != event.task_id
            or record.sequence != event.sequence
            or record.previous_event_digest != event.previous_event_digest
            or record.kind != event.kind
            or record.plan_generation != event.plan_generation
            or record.source_progress_digest != event.source_progress_digest
            or record.reason_code != event.reason_code
            or record.evidence_manifest != event.evidence_manifest
            or record.evidence_digest != event.evidence_digest
            or record.event_digest != event.event_digest
        ):
            raise TaskLoopActivationProofRejectedError(
                "Task Loop cycle event columns changed"
            )
        return event

    @classmethod
    def _approval_from_record(
        cls,
        record: TaskLoopCapabilityApprovalRecord,
    ) -> TaskLoopCapabilityApproval:
        values = {
            "schema_version": "deskpilot.task-loop-capability-approval.v1",
            **{
                field: getattr(record, field)
                for field in (
                    "approval_id",
                    "execution_id",
                    "task_id",
                    "run_id",
                    "node_id",
                    "node_binding_id",
                    "attempt_id",
                    "attempt",
                    "plan_generation",
                    "input_binding_digest",
                    "executor_manifest_digest",
                    "preview_schema_digest",
                    "preview_manifest",
                    "confirmation_digest",
                    "requested_execution_revision",
                    "status",
                    "revision",
                    "result_digest",
                    "approval_digest",
                )
            },
            "approved_at": cls._aware_optional(record.approved_at),
            "consumed_at": cls._aware_optional(record.consumed_at),
            "created_at": cls._aware(record.created_at),
            "updated_at": cls._aware(record.updated_at),
        }
        try:
            approval = TaskLoopCapabilityApproval.model_validate(values)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop capability approval Schema was rejected"
            ) from error
        if record.manifest != approval.model_dump(mode="json"):
            raise TaskLoopActivationProofRejectedError(
                "Task Loop capability approval columns changed"
            )
        return approval

    @staticmethod
    def _dependency_is_verified(
        node_id: str,
        *,
        nodes_by_id: dict[str, TaskExecutionNodeRecord],
        results_by_node: dict[str, TaskLoopVerifiedResult],
    ) -> bool:
        dependency = nodes_by_id.get(node_id)
        if dependency is None:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop dependency points outside its exact Run"
            )
        if dependency.node_kind in {
            DraftNodeKind.AGENT.value,
            DraftNodeKind.CAPABILITY.value,
        }:
            return node_id in results_by_node
        return dependency.status == "verified"

    @staticmethod
    def _execution_phase(
        execution: TaskLoopExecution,
        nodes: tuple[TaskLoopExecutionNodeRead, ...],
    ) -> str:
        if execution.status == "awaiting_user":
            return "awaiting_user"
        if execution.status in {"repairing", "failed"}:
            return "repair"
        if any(item.status == "awaiting_verification" or item.candidate_present for item in nodes):
            return "verify"
        return "execute"

    async def _assert_persisted_exact(
        self,
        task_id: str,
        draft: ModelPlannerDraft,
        bindings: tuple[ModelPlannerNodeBinding, ...],
    ) -> None:
        async with self._database.session() as session:
            record = await session.scalar(
                select(TaskLoopExecutionRecord).where(TaskLoopExecutionRecord.task_id == task_id)
            )
            if record is None:
                raise TaskLoopActivationConflictError("Concurrent activation winner is missing")
            await self._read_exact(
                session,
                record,
                draft=draft,
                expected_bindings=bindings,
            )

    async def _read_exact(
        self,
        session: AsyncSession,
        record: TaskLoopExecutionRecord,
        *,
        draft: ModelPlannerDraft,
        expected_bindings: tuple[ModelPlannerNodeBinding, ...] | None,
    ) -> TaskLoopExecution:
        try:
            execution = TaskLoopExecution.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted Task Loop execution manifest is invalid"
            ) from error
        expected_record = self._execution_record(execution)
        for field in (
            "execution_id",
            "loop_id",
            "draft_id",
            "task_id",
            "plan_id",
            "plan_generation",
            "plan_manifest_digest",
            "run_id",
            "status",
            "revision",
            "event_count",
            "latest_event_id",
            "latest_event_digest",
            "node_binding_count",
            "binding_set_digest",
            "manifest",
            "execution_digest",
        ):
            if getattr(record, field) != getattr(expected_record, field):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop execution columns diverge from its manifest"
                )
        if (
            self._aware(record.created_at) != execution.created_at
            or self._aware(record.updated_at) != execution.updated_at
            or execution.draft_id != draft.draft_id
            or execution.plan_id != draft.expected_plan.plan_id
            or execution.plan_manifest_digest != draft.expected_plan_manifest_digest
        ):
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution scope differs from the sealed Draft"
            )
        events = tuple(
            (
                await session.scalars(
                    select(TaskLoopExecutionEventRecord)
                    .where(TaskLoopExecutionEventRecord.execution_id == execution.execution_id)
                    .order_by(TaskLoopExecutionEventRecord.sequence)
                )
            ).all()
        )
        if not events or len(events) != execution.event_count:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution event chain is incomplete"
            )
        parsed_events = tuple(self._event_from_record(item) for item in events)
        previous_digest: str | None = None
        for sequence, event in enumerate(parsed_events, start=1):
            if (
                event.sequence != sequence
                or event.previous_event_digest != previous_digest
                or event.execution_id != execution.execution_id
                or event.task_id != execution.task_id
                or event.plan_manifest_digest != execution.plan_manifest_digest
                or event.run_id != execution.run_id
                or event.binding_set_digest != execution.binding_set_digest
                or (sequence == 1 and event.kind != "activated")
                or (sequence > 1 and event.kind == "activated")
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop execution event chain changed"
                )
            previous_digest = event.event_digest
        event = parsed_events[-1]
        status_by_event = {
            "activated": "active",
            "paused": "paused",
            "resumed": "active",
            "awaiting_user": "awaiting_user",
            "repair_started": "repairing",
            "failed": "failed",
            "succeeded": "succeeded",
            "cancelled": "cancelled",
        }
        if (
            event.event_id != execution.latest_event_id
            or event.event_digest != execution.latest_event_digest
            or event.binding_set_digest != execution.binding_set_digest
            or status_by_event[event.kind] != execution.status
            or execution.revision != execution.event_count
        ):
            raise TaskLoopActivationProofRejectedError("Task Loop execution event pointer changed")
        binding_records = tuple(
            (
                await session.scalars(
                    select(ModelPlannerNodeBindingRecord)
                    .where(ModelPlannerNodeBindingRecord.execution_id == execution.execution_id)
                    .order_by(ModelPlannerNodeBindingRecord.composite_node_id)
                )
            ).all()
        )
        bindings = tuple(self._binding_from_record(item) for item in binding_records)
        if len(bindings) != execution.node_binding_count:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution node-binding set is incomplete"
            )
        binding_set_digest = sha256_digest(
            {
                "node_bindings": [
                    {
                        "node_binding_id": item.node_binding_id,
                        "binding_digest": item.binding_digest,
                    }
                    for item in bindings
                ]
            }
        )
        if binding_set_digest != execution.binding_set_digest:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution binding-set digest changed"
            )
        if expected_bindings is not None and bindings != tuple(
            sorted(expected_bindings, key=lambda item: item.composite_node_id)
        ):
            raise TaskLoopActivationProofRejectedError(
                "Persisted node authorities differ from current exact bindings"
            )
        await self._validate_plan_run_lineage(session, execution, bindings)
        return execution

    @staticmethod
    async def _validate_plan_run_lineage(
        session: AsyncSession,
        execution: TaskLoopExecution,
        bindings: tuple[ModelPlannerNodeBinding, ...],
    ) -> None:
        state = await session.get(TaskPlanningStateRecord, execution.task_id)
        plan = await session.get(
            TaskPlanGenerationRecord,
            (execution.task_id, execution.plan_generation),
        )
        run = await session.get(TaskExecutionRunRecord, execution.run_id)
        try:
            executable = ExecutablePlan.model_validate(plan.manifest) if plan is not None else None
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution Plan manifest is invalid"
            ) from error
        if (
            state is None
            or plan is None
            or run is None
            or state.active_plan_generation != 1
            or state.active_plan_digest != execution.plan_manifest_digest
            or plan.status != "active"
            or plan.plan_id != execution.plan_id
            or plan.plan_manifest_digest != execution.plan_manifest_digest
            or run.task_id != execution.task_id
            or run.plan_generation != 1
            or run.plan_digest != execution.plan_manifest_digest
            or executable is None
            or executable.plan_manifest_digest != execution.plan_manifest_digest
        ):
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution Plan or Run lineage changed"
            )
        node_records = tuple(
            (
                await session.scalars(
                    select(TaskExecutionNodeRecord).where(
                        TaskExecutionNodeRecord.run_id == execution.run_id
                    )
                )
            ).all()
        )
        nodes_by_id = {item.node_id: item for item in node_records}
        if len(node_records) != len(executable.nodes):
            raise TaskLoopActivationProofRejectedError("Task Loop execution node set changed")
        for expected in executable.nodes:
            actual = nodes_by_id.get(expected.node_id)
            if actual is None or (
                actual.run_id != execution.run_id
                or actual.local_key != expected.local_key
                or actual.node_kind != expected.kind.value
                or actual.node_spec_digest != expected.node_spec_digest
                or tuple(actual.depends_on) != expected.depends_on
                or actual.handoff_parent_node_id != expected.handoff_parent_node_id
                or actual.bound_agent
                != (
                    expected.bound_agent.model_dump(mode="json")
                    if expected.bound_agent is not None
                    else None
                )
                or actual.capability
                != (
                    expected.capability.model_dump(mode="json")
                    if expected.capability is not None
                    else None
                )
                or tuple(actual.acceptance_refs) != expected.acceptance_refs
                or actual.budget != expected.budget.model_dump(mode="json")
                or actual.runtime_enabled is not expected.runtime_enabled
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop execution node differs from its exact Plan"
                )
        edge_records = tuple(
            (
                await session.scalars(
                    select(TaskExecutionEdgeRecord).where(
                        TaskExecutionEdgeRecord.run_id == execution.run_id
                    )
                )
            ).all()
        )
        expected_edges = {
            (source, node.node_id, "verified")
            for node in executable.nodes
            for source in node.depends_on
        }
        actual_edges = {
            (item.from_node_id, item.to_node_id, item.requirement) for item in edge_records
        }
        if actual_edges != expected_edges:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution edges differ from its exact Plan"
            )
        if any(item.composite_node_id not in nodes_by_id for item in bindings):
            raise TaskLoopActivationProofRejectedError(
                "Task Loop node binding points outside its exact Run"
            )

    @staticmethod
    def _assert_locked_source(
        loop_record: TaskLoopRecord,
        draft_record: ModelPlannerDraftRecord,
        loop: TaskLoop,
        draft: ModelPlannerDraft,
    ) -> None:
        if (
            loop_record.status != "planned"
            or loop_record.loop_digest != loop.loop_digest
            or loop_record.active_draft_id != draft.draft_id
            or loop_record.active_draft_record_digest != draft.draft_record_digest
            or draft_record.loop_id != loop.loop_id
            or draft_record.task_id != loop.source.task_id
            or draft_record.draft_record_digest != draft.draft_record_digest
            or draft_record.expected_plan_manifest_digest != draft.expected_plan_manifest_digest
            or draft_record.manifest != draft.model_dump(mode="json")
        ):
            raise TaskLoopActivationProofRejectedError(
                "Locked Task Loop or Draft proof changed before activation"
            )

    @staticmethod
    async def _required_execution_record(
        session: AsyncSession,
        execution_id: str,
    ) -> TaskLoopExecutionRecord:
        record = await session.get(TaskLoopExecutionRecord, execution_id)
        if record is None:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution disappeared inside activation transaction"
            )
        return record

    @staticmethod
    def _execution_record(execution: TaskLoopExecution) -> TaskLoopExecutionRecord:
        return TaskLoopExecutionRecord(
            execution_id=execution.execution_id,
            loop_id=execution.loop_id,
            draft_id=execution.draft_id,
            task_id=execution.task_id,
            plan_id=execution.plan_id,
            plan_generation=execution.plan_generation,
            plan_manifest_digest=execution.plan_manifest_digest,
            run_id=execution.run_id,
            status=execution.status,
            revision=execution.revision,
            event_count=execution.event_count,
            latest_event_id=execution.latest_event_id,
            latest_event_digest=execution.latest_event_digest,
            node_binding_count=execution.node_binding_count,
            binding_set_digest=execution.binding_set_digest,
            manifest=execution.model_dump(mode="json"),
            execution_digest=execution.execution_digest,
            created_at=execution.created_at,
            updated_at=execution.updated_at,
        )

    @staticmethod
    def _apply_execution_record(
        record: TaskLoopExecutionRecord,
        execution: TaskLoopExecution,
    ) -> None:
        for field in (
            "status",
            "revision",
            "event_count",
            "latest_event_id",
            "latest_event_digest",
            "manifest",
            "execution_digest",
            "updated_at",
        ):
            value = (
                execution.model_dump(mode="json")
                if field == "manifest"
                else getattr(execution, field)
            )
            setattr(record, field, value)

    @staticmethod
    def _apply_attempt_record(
        record: TaskLoopNodeAttemptRecord,
        attempt: TaskLoopNodeAttempt,
    ) -> None:
        for field in (
            "status",
            "revision",
            "claim_owner_id",
            "claim_fencing_token",
            "claim_acquired_at",
            "claim_expires_at",
            "input_manifest",
            "input_digest",
            "context_manifest",
            "context_digest",
            "candidate_manifest",
            "candidate_digest",
            "candidate_recorded_at",
            "verification_manifest",
            "verification_digest",
            "verified_at",
            "receipt_manifest",
            "receipt_digest",
            "error_code",
            "error_digest",
            "created_at",
            "updated_at",
            "attempt_digest",
        ):
            setattr(record, field, getattr(attempt, field))
        record.manifest = attempt.model_dump(mode="json")

    @staticmethod
    def _event_record(event: TaskLoopExecutionEvent) -> TaskLoopExecutionEventRecord:
        return TaskLoopExecutionEventRecord(
            event_id=event.event_id,
            execution_id=event.execution_id,
            task_id=event.task_id,
            sequence=event.sequence,
            previous_event_digest=event.previous_event_digest,
            kind=event.kind,
            plan_manifest_digest=event.plan_manifest_digest,
            run_id=event.run_id,
            binding_set_digest=event.binding_set_digest,
            manifest=event.model_dump(mode="json"),
            event_digest=event.event_digest,
            created_at=event.created_at,
        )

    @staticmethod
    def _amendment_record(
        binding: WorkspaceCodingAmendmentBinding,
    ) -> WorkspaceCodingAmendmentBindingRecord:
        return WorkspaceCodingAmendmentBindingRecord(
            amendment_id=binding.amendment_id,
            conversation_id=binding.conversation_id,
            source_task_id=binding.source_task_id,
            source_execution_id=binding.source_execution_id,
            source_contract_version=binding.source_contract_version,
            source_contract_digest=binding.source_contract_digest,
            source_plan_generation=binding.source_plan_generation,
            source_plan_digest=binding.source_plan_digest,
            source_execution_digest=binding.source_execution_digest,
            source_execution_event_digest=(
                binding.source_execution_event_digest
            ),
            successor_task_id=binding.successor_task_id,
            successor_user_message_id=binding.successor_user_message_id,
            successor_user_message_digest=(
                binding.successor_user_message_digest
            ),
            manifest=binding.model_dump(mode="json"),
            amendment_digest=binding.amendment_digest,
            created_at=binding.created_at,
        )

    @staticmethod
    def _amendment_from_record(
        record: WorkspaceCodingAmendmentBindingRecord,
    ) -> WorkspaceCodingAmendmentBinding:
        try:
            binding = WorkspaceCodingAmendmentBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted workspace coding amendment is invalid"
            ) from error
        expected = TaskLoopActivationRuntime._amendment_record(binding)
        for field in (
            "amendment_id",
            "conversation_id",
            "source_task_id",
            "source_execution_id",
            "source_contract_version",
            "source_contract_digest",
            "source_plan_generation",
            "source_plan_digest",
            "source_execution_digest",
            "source_execution_event_digest",
            "successor_task_id",
            "successor_user_message_id",
            "successor_user_message_digest",
            "manifest",
            "amendment_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise TaskLoopActivationProofRejectedError(
                    "Workspace coding amendment columns diverge from its manifest"
                )
        if TaskLoopActivationRuntime._aware(record.created_at) != binding.created_at:
            raise TaskLoopActivationProofRejectedError(
                "Workspace coding amendment timestamp changed"
            )
        return binding

    @staticmethod
    def _binding_record(
        binding: ModelPlannerNodeBinding,
        execution_id: str,
        created_at: datetime,
    ) -> ModelPlannerNodeBindingRecord:
        return ModelPlannerNodeBindingRecord(
            node_binding_id=binding.node_binding_id,
            execution_id=execution_id,
            task_id=binding.task_id,
            user_message_id=binding.user_message_id,
            draft_id=binding.draft_id,
            step_binding_id=binding.step_binding_id,
            step_binding_digest=binding.step_binding_digest,
            step_ordinal=binding.step_ordinal,
            offer_id=binding.offer_id,
            offer_key=binding.offer_key,
            offer_digest=binding.offer_digest,
            recipe_manifest=binding.recipe.model_dump(mode="json"),
            recipe_digest=binding.recipe.route_manifest_digest,
            policy_snapshot_digest=binding.policy_snapshot_digest,
            source_contract_digest=binding.source_contract_digest,
            source_plan_id=binding.source_plan_id,
            source_plan_manifest_digest=binding.source_plan_manifest_digest,
            source_node_id=binding.source_node_id,
            source_node_spec_digest=binding.source_node_spec_digest,
            composite_contract_digest=binding.composite_contract_digest,
            composite_plan_id=binding.composite_plan_id,
            composite_plan_manifest_digest=(binding.composite_plan_manifest_digest),
            composite_node_id=binding.composite_node_id,
            composite_node_spec_digest=binding.composite_node_spec_digest,
            mapping_manifest=binding.mapping.model_dump(mode="json"),
            mapping_digest=binding.mapping.mapping_digest,
            parameter_bindings_manifest=[
                item.model_dump(mode="json") for item in binding.parameter_bindings
            ],
            parameter_bindings_digest=binding.parameter_bindings_digest,
            bound_input_manifest=binding.bound_input_manifest,
            bound_input_digest=binding.bound_input_digest,
            effective_authority_manifest=(binding.effective_authority.model_dump(mode="json")),
            effective_authority_digest=(binding.effective_authority.authority_digest),
            runtime_eligibility_manifest=(binding.runtime_eligibility.model_dump(mode="json")),
            runtime_eligibility_digest=(binding.runtime_eligibility.eligibility_digest),
            manifest=binding.model_dump(mode="json"),
            binding_digest=binding.binding_digest,
            created_at=created_at,
        )

    @staticmethod
    def _event_from_record(
        record: TaskLoopExecutionEventRecord,
    ) -> TaskLoopExecutionEvent:
        try:
            event = TaskLoopExecutionEvent.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted Task Loop execution event is invalid"
            ) from error
        expected = TaskLoopActivationRuntime._event_record(event)
        for field in (
            "event_id",
            "execution_id",
            "task_id",
            "sequence",
            "previous_event_digest",
            "kind",
            "plan_manifest_digest",
            "run_id",
            "binding_set_digest",
            "manifest",
            "event_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop execution event columns diverge from its manifest"
                )
        if TaskLoopActivationRuntime._aware(record.created_at) != event.created_at:
            raise TaskLoopActivationProofRejectedError(
                "Task Loop execution event timestamp changed"
            )
        return event

    @staticmethod
    def _binding_from_record(
        record: ModelPlannerNodeBindingRecord,
    ) -> ModelPlannerNodeBinding:
        try:
            binding = ModelPlannerNodeBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted model-planner node binding is invalid"
            ) from error
        expected = TaskLoopActivationRuntime._binding_record(
            binding,
            record.execution_id,
            record.created_at,
        )
        for field in (
            "node_binding_id",
            "execution_id",
            "task_id",
            "user_message_id",
            "draft_id",
            "step_binding_id",
            "step_binding_digest",
            "step_ordinal",
            "offer_id",
            "offer_key",
            "offer_digest",
            "recipe_manifest",
            "recipe_digest",
            "policy_snapshot_digest",
            "source_contract_digest",
            "source_plan_id",
            "source_plan_manifest_digest",
            "source_node_id",
            "source_node_spec_digest",
            "composite_contract_digest",
            "composite_plan_id",
            "composite_plan_manifest_digest",
            "composite_node_id",
            "composite_node_spec_digest",
            "mapping_manifest",
            "mapping_digest",
            "parameter_bindings_manifest",
            "parameter_bindings_digest",
            "bound_input_manifest",
            "bound_input_digest",
            "effective_authority_manifest",
            "effective_authority_digest",
            "runtime_eligibility_manifest",
            "runtime_eligibility_digest",
            "manifest",
            "binding_digest",
        ):
            if getattr(record, field) != getattr(expected, field):
                raise TaskLoopActivationProofRejectedError(
                    "Model Planner node binding columns diverge from its manifest"
                )
        return binding

    @staticmethod
    def _attempt_from_record(
        record: TaskLoopNodeAttemptRecord,
    ) -> TaskLoopNodeAttempt:
        try:
            attempt = TaskLoopNodeAttempt.model_validate(record.manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted Task Loop node attempt is invalid"
            ) from error
        for field in (
            "attempt_id",
            "execution_id",
            "node_binding_id",
            "run_id",
            "node_id",
            "attempt",
            "status",
            "revision",
            "claim_owner_id",
            "claim_fencing_token",
            "input_manifest",
            "input_digest",
            "context_manifest",
            "context_digest",
            "candidate_manifest",
            "candidate_digest",
            "verification_manifest",
            "verification_digest",
            "receipt_manifest",
            "receipt_digest",
            "error_code",
            "error_digest",
            "manifest",
            "attempt_digest",
        ):
            expected = (
                attempt.model_dump(mode="json") if field == "manifest" else getattr(attempt, field)
            )
            if getattr(record, field) != expected:
                raise TaskLoopActivationProofRejectedError(
                    "Task Loop attempt columns diverge from its manifest"
                )
        for field in (
            "claim_acquired_at",
            "claim_expires_at",
            "candidate_recorded_at",
            "verified_at",
            "created_at",
            "updated_at",
        ):
            if TaskLoopActivationRuntime._aware_optional(getattr(record, field)) != getattr(
                attempt, field
            ):
                raise TaskLoopActivationProofRejectedError("Task Loop attempt timestamp changed")
        return attempt

    @staticmethod
    def _verified_result_from_record(
        record: TaskLoopVerifiedResultRecord,
    ) -> TaskLoopVerifiedResult:
        values = {
            **{
                field: getattr(record, field)
                for field in (
                    "result_ref_id",
                    "attempt_id",
                    "execution_id",
                    "node_binding_id",
                    "node_binding_digest",
                    "run_id",
                    "node_id",
                    "producer_kind",
                    "capability_manifest",
                    "capability_digest",
                    "agent_binding_manifest",
                    "agent_binding_digest",
                    "executor_manifest_digest",
                    "agent_result_proof_digest",
                    "input_binding_digest",
                    "context_digest",
                    "candidate_digest",
                    "result_kind",
                    "output_manifest",
                    "output_schema_digest",
                    "output_digest",
                    "verification_manifest",
                    "verification_digest",
                    "result_ref_manifest",
                    "result_ref_digest",
                )
            },
            "created_at": TaskLoopActivationRuntime._aware(record.created_at),
        }
        try:
            result = TaskLoopVerifiedResult.model_validate(values)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted verified ResultRef proof is invalid"
            ) from error
        for field, value in values.items():
            if field == "created_at":
                continue
            if getattr(record, field) != value:
                raise TaskLoopActivationProofRejectedError(
                    "Verified ResultRef columns changed during reconstruction"
                )
        return result

    @staticmethod
    def _validate_verified_result(
        *,
        execution: TaskLoopExecution,
        result: TaskLoopVerifiedResult,
        attempt: TaskLoopNodeAttempt,
        binding: ModelPlannerNodeBinding,
    ) -> None:
        try:
            result_ref = VerifiedCapabilityResultRef.model_validate(result.result_ref_manifest)
        except ValidationError as error:
            raise TaskLoopActivationProofRejectedError(
                "Persisted ResultRef does not match the strict verified Schema"
            ) from error
        result_id_material = {
            "attempt_id": attempt.attempt_id,
            "result_ref_digest": result_ref.result_ref_digest,
        }
        expected_result_id = f"tlr_{sha256_digest(result_id_material)}"
        capability_manifest = result_ref.capability.model_dump(mode="json")
        verified_failure = bool(
            result.result_kind
            in {"workspace_check", "python_test", "node_test", "command_profile"}
            and result.output_manifest.get("status") != "passed"
        )
        if (
            result.result_ref_id != expected_result_id
            or result.execution_id != execution.execution_id
            or result.run_id != execution.run_id
            or result.node_id != binding.composite_node_id
            or result.node_binding_id != binding.node_binding_id
            or result.node_binding_digest != binding.binding_digest
            or result.attempt_id != attempt.attempt_id
            or attempt.status not in {"verified", "failed"}
            or (attempt.status == "failed" and not verified_failure)
            or attempt.execution_id != execution.execution_id
            or attempt.run_id != execution.run_id
            or attempt.node_id != binding.composite_node_id
            or attempt.node_binding_id != binding.node_binding_id
            or attempt.input_digest != result.input_binding_digest
            or attempt.context_digest != result.context_digest
            or attempt.verification_digest != result.verification_digest
            or attempt.verification_manifest is None
            or attempt.verified_at is None
            or result_ref.task_id != execution.task_id
            or result_ref.run_id != execution.run_id
            or result_ref.plan_generation != execution.plan_generation
            or result_ref.producer_node_id != binding.composite_node_id
            or result_ref.producer_attempt != attempt.attempt
            or result_ref.capability.model_dump(mode="json") != result.capability_manifest
            or result.capability_digest != sha256_digest(capability_manifest)
            or result_ref.result_kind.value != result.result_kind
            or result_ref.result_schema_digest != result.output_schema_digest
            or result_ref.result_digest != result.output_digest
            or result_ref.verification_digest != result.verification_digest
            or result_ref.result_ref_digest != result.result_ref_digest
            or result.created_at < attempt.created_at
        ):
            raise TaskLoopActivationProofRejectedError(
                "Verified ResultRef lineage differs from its exact attempt"
            )
        if result.producer_kind == "capability_executor":
            capability = binding.effective_authority.capability
            if (
                binding.runtime_eligibility.runtime_kind != "capability_executor"
                or capability is None
                or result_ref.capability != capability
                or result.executor_manifest_digest
                != binding.runtime_eligibility.executor_manifest_digest
                or result.candidate_digest != attempt.candidate_digest
                or attempt.candidate_manifest is None
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Capability ResultRef producer proof changed"
                )
        else:
            bound_agent = binding.effective_authority.bound_agent
            if (
                binding.runtime_eligibility.runtime_kind != "agent"
                or bound_agent is None
                or result.agent_binding_manifest != bound_agent.model_dump(mode="json")
                or result.agent_binding_digest != sha256_digest(bound_agent.model_dump(mode="json"))
                or result.agent_result_proof_digest is None
            ):
                raise TaskLoopActivationProofRejectedError(
                    "Agent-bridge ResultRef producer proof changed"
                )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Task-loop activation clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @staticmethod
    def _aware_optional(value: datetime | None) -> datetime | None:
        return None if value is None else TaskLoopActivationRuntime._aware(value)
