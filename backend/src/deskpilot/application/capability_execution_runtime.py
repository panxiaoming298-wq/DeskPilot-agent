"""Persistent fenced execution for exact stage-112B capability nodes.

The runtime deliberately splits database work from adapter I/O into five
boundaries: claim, execute, persist candidate, verify, and persist verified
result.  A candidate is durable recovery material only; it never satisfies a
Plan edge.  Successors are unlocked only after every incoming producer has one
strict, persistent ``TaskLoopVerifiedResultRecord``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.capability_execution_engine import (
    CapabilityExecutionCandidate,
    VerifiedCapabilityOutput,
)
from deskpilot.application.capability_executor_registry import (
    CapabilityExecutorRegistration,
    CapabilityExecutorRegistry,
)
from deskpilot.application.capability_input_binding_catalog import (
    BoundCapabilityInput,
    CapabilityInputBindingCatalog,
    ResolvedVerifiedCapabilityResult,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import ExecutionNodeStatus, ExecutionRunStatus
from deskpilot.domain.capability_execution import (
    CapabilityApprovalRequirement,
    CapabilityExecutionContext,
    CapabilityRecoveryPolicy,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.task_loop_approvals import TaskLoopCapabilityApproval
from deskpilot.domain.task_loop_execution import (
    ModelPlannerNodeBinding,
    TaskLoopExecution,
    TaskLoopNodeAttempt,
    TaskLoopVerifiedResult,
)
from deskpilot.domain.task_plans import (
    CapabilityRef,
    DraftNodeKind,
    ExecutablePlan,
    ExecutablePlanNode,
)
from deskpilot.domain.workspace_files import WorkspacePatchPreview
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ModelPlannerNodeBindingRecord,
    TaskExecutionEdgeRecord,
    TaskExecutionNodeRecord,
    TaskExecutionRunRecord,
    TaskLoopCapabilityApprovalRecord,
    TaskLoopExecutionEventRecord,
    TaskLoopExecutionRecord,
    TaskLoopNodeAttemptRecord,
    TaskLoopVerifiedResultRecord,
    TaskPlanGenerationRecord,
    utc_now,
)

CapabilityRuntimeOutcomeStatus = Literal[
    "awaiting_user",
    "verified",
    "failed",
    "outcome_unknown",
]
_ACTIVE_RUN_STATUSES = frozenset(
    {
        ExecutionRunStatus.ACTIVE.value,
        ExecutionRunStatus.AWAITING_VERIFICATION.value,
    }
)
_TERMINAL_ATTEMPT_STATUSES = frozenset({"verified", "failed", "outcome_unknown", "cancelled"})
_ERROR_CODE_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


class CapabilityExecutionRuntimeError(RuntimeError):
    code = "CAPABILITY_EXECUTION_RUNTIME_ERROR"


class CapabilityExecutionRuntimeNotFoundError(CapabilityExecutionRuntimeError):
    code = "CAPABILITY_EXECUTION_RUNTIME_NOT_FOUND"


class CapabilityExecutionRuntimeConflictError(CapabilityExecutionRuntimeError):
    code = "CAPABILITY_EXECUTION_RUNTIME_CONFLICT"


class CapabilityExecutionRuntimeProofRejectedError(CapabilityExecutionRuntimeError):
    code = "CAPABILITY_EXECUTION_RUNTIME_PROOF_REJECTED"


class CapabilityExecutionRuntimeStaleFenceError(CapabilityExecutionRuntimeConflictError):
    code = "CAPABILITY_EXECUTION_RUNTIME_STALE_FENCE"


class CapabilityVerificationDeferredError(CapabilityExecutionRuntimeError):
    """Verification failed outside the transaction; the candidate stays durable."""

    code = "CAPABILITY_VERIFICATION_DEFERRED"


class CapabilityExecutionEnginePort(Protocol):
    async def execute_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
    ) -> CapabilityExecutionCandidate: ...

    async def prepare_approval(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
    ) -> BaseModel: ...

    async def execute_approved_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
        preview_manifest: dict[str, Any],
    ) -> CapabilityExecutionCandidate: ...

    async def verify_candidate(
        self,
        context: CapabilityExecutionContext,
        bound_input: BoundCapabilityInput,
        candidate: CapabilityExecutionCandidate,
    ) -> VerifiedCapabilityOutput: ...


@dataclass(frozen=True, slots=True)
class CapabilityRuntimeOutcome:
    execution_id: str
    run_id: str
    node_id: str
    attempt_id: str
    attempt: int
    status: CapabilityRuntimeOutcomeStatus
    result_ref: VerifiedCapabilityResultRef | None = None
    error_code: str | None = None


@dataclass(frozen=True, slots=True)
class _CapabilityWork:
    execution: TaskLoopExecution
    plan: ExecutablePlan
    plan_node: ExecutablePlanNode
    binding: ModelPlannerNodeBinding
    attempt_id: str
    attempt: int
    persistence_owner_id: str
    persistence_fencing_token: int
    bound_input: BoundCapabilityInput
    context: CapabilityExecutionContext
    registration: CapabilityExecutorRegistration
    candidate: CapabilityExecutionCandidate | None = None
    approval: TaskLoopCapabilityApproval | None = None


class CapabilityExecutionRuntime:
    """Claim, execute and verify one exact capability node per call."""

    def __init__(
        self,
        database: Database,
        input_bindings: CapabilityInputBindingCatalog,
        executors: CapabilityExecutorRegistry,
        engine: CapabilityExecutionEnginePort,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._database = database
        self._input_bindings = input_bindings
        self._executors = executors
        self._engine = engine
        self._clock = clock

    async def run_once(
        self,
        task_id: str,
        owner_id: str,
        *,
        lease_seconds: int = 60,
    ) -> CapabilityRuntimeOutcome | None:
        """Run or resume one capability attempt without holding a DB transaction over I/O."""

        if not owner_id or len(owner_id) > 128:
            raise ValueError("Capability claim owner must contain 1 to 128 characters")
        if not 5 <= lease_seconds <= 600:
            raise ValueError("Capability lease must be between 5 and 600 seconds")
        try:
            work = await self._claim_work(
                task_id,
                owner_id,
                lease_seconds=lease_seconds,
            )
            if work is None:
                return None
            if work.candidate is None:
                try:
                    if (
                        work.registration.manifest.approval_requirement
                        is CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
                    ):
                        if work.approval is None:
                            raw_preview = await self._engine.prepare_approval(
                                work.context,
                                work.bound_input,
                            )
                            preview = WorkspacePatchPreview.model_validate(
                                raw_preview.model_dump(mode="json")
                            )
                            return await self._persist_approval(work, preview)
                        candidate = await self._engine.execute_approved_candidate(
                            work.context,
                            work.bound_input,
                            work.approval.preview_manifest,
                        )
                    else:
                        candidate = await self._engine.execute_candidate(
                            work.context,
                            work.bound_input,
                        )
                except Exception as error:
                    return await self._settle_execution_error(work, error)
                await self._persist_candidate(work, candidate)
                work = replace(work, candidate=candidate)
            assert work.candidate is not None
            try:
                verified = await self._engine.verify_candidate(
                    work.context,
                    work.bound_input,
                    work.candidate,
                )
            except Exception as error:
                await self._release_verification_claim(work, error)
                raise CapabilityVerificationDeferredError(
                    "Capability verification was deferred with its candidate intact"
                ) from error
            return await self._persist_verified(work, verified)
        except CapabilityExecutionRuntimeError:
            raise
        except (ValidationError, ValueError) as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability persistence proof failed Schema or digest validation"
            ) from error

    async def recover_expired(self, task_id: str) -> None:
        """Reconcile expired capability claims before the pure reducer snapshots them."""

        now = self._now()
        async with self._database.session() as session, session.begin():
            execution_records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopExecutionRecord)
                        .where(TaskLoopExecutionRecord.task_id == task_id)
                        .with_for_update()
                    )
                ).all()
            )
            if not execution_records:
                return
            if len(execution_records) != 1:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Task has multiple model-planner executions"
                )
            execution_record = execution_records[0]
            execution = self._execution_from_record(execution_record)
            if execution.status != "active":
                return
            plan = await self._load_plan(session, execution)
            await self._recover_expired_running(
                session,
                execution,
                plan,
                now=now,
            )

    async def _claim_work(
        self,
        task_id: str,
        owner_id: str,
        *,
        lease_seconds: int,
    ) -> _CapabilityWork | None:
        now = self._now()
        async with self._database.session() as session, session.begin():
            execution_record = await self._execution_record_for_task(session, task_id)
            execution = self._execution_from_record(execution_record)
            if execution.status != "active":
                return None
            run = await self._locked_run(session, execution)
            if run.status not in _ACTIVE_RUN_STATUSES:
                return None
            plan = await self._load_plan(session, execution)
            await self._recover_expired_running(
                session,
                execution,
                plan,
                now=now,
            )

            approved_attempt = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .join(
                    TaskLoopCapabilityApprovalRecord,
                    TaskLoopCapabilityApprovalRecord.attempt_id
                    == TaskLoopNodeAttemptRecord.attempt_id,
                )
                .where(
                    TaskLoopNodeAttemptRecord.execution_id == execution.execution_id,
                    TaskLoopNodeAttemptRecord.status == "prepared",
                    TaskLoopCapabilityApprovalRecord.status == "approved",
                )
                .order_by(TaskLoopNodeAttemptRecord.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if approved_attempt is not None:
                return await self._claim_approved_attempt(
                    session,
                    execution,
                    plan,
                    approved_attempt,
                    owner_id=owner_id,
                    now=now,
                    lease_seconds=lease_seconds,
                )

            resumable = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(
                    TaskLoopNodeAttemptRecord.execution_id == execution.execution_id,
                    TaskLoopNodeAttemptRecord.status == "awaiting_verification",
                )
                .order_by(TaskLoopNodeAttemptRecord.created_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if resumable is not None:
                expires_at = self._optional_aware(resumable.claim_expires_at)
                if expires_at is not None and expires_at > now:
                    return None
                return await self._claim_persisted_candidate(
                    session,
                    execution,
                    plan,
                    resumable,
                    owner_id=owner_id,
                    now=now,
                    lease_seconds=lease_seconds,
                )

            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(
                    TaskExecutionNodeRecord.run_id == execution.run_id,
                    TaskExecutionNodeRecord.status == ExecutionNodeStatus.READY.value,
                    TaskExecutionNodeRecord.runtime_enabled.is_(True),
                    TaskExecutionNodeRecord.node_kind == DraftNodeKind.CAPABILITY.value,
                )
                .order_by(TaskExecutionNodeRecord.local_key)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if node is None:
                return None
            return await self._claim_ready_node(
                session,
                execution,
                plan,
                node,
                owner_id=owner_id,
                now=now,
                lease_seconds=lease_seconds,
            )

    async def _claim_ready_node(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        node: TaskExecutionNodeRecord,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _CapabilityWork:
        plan_node = self._plan_node(plan, node.node_id)
        binding = await self._load_binding(session, execution, node, plan_node)
        dependencies = await self._load_dependencies(
            session,
            execution,
            plan,
            node,
            require_complete=True,
        )
        if node.attempt_count >= plan_node.budget.retries + 1:
            raise CapabilityExecutionRuntimeConflictError(
                "Capability node has exhausted its exact retry budget"
            )
        existing = tuple(
            (
                await session.scalars(
                    select(TaskLoopNodeAttemptRecord).where(
                        TaskLoopNodeAttemptRecord.execution_id == execution.execution_id,
                        TaskLoopNodeAttemptRecord.node_id == node.node_id,
                    )
                )
            ).all()
        )
        if any(item.status not in _TERMINAL_ATTEMPT_STATUSES for item in existing):
            raise CapabilityExecutionRuntimeConflictError(
                "Ready capability node already has a live Task Loop attempt"
            )

        bound_input = self._input_bindings.bind_node(
            node_binding=binding,
            dependencies=dependencies,
        )
        attempt_number = node.attempt_count + 1
        fencing_token = node.claim_fencing_token + 1
        context = CapabilityExecutionContext.build(
            task_id=execution.task_id,
            run_id=execution.run_id,
            plan_id=execution.plan_id,
            plan_generation=execution.plan_generation,
            plan_manifest_digest=execution.plan_manifest_digest,
            node_id=node.node_id,
            node_kind=DraftNodeKind.CAPABILITY,
            node_spec_digest=node.node_spec_digest,
            node_binding_id=binding.node_binding_id,
            node_binding_digest=binding.binding_digest,
            effective_authority_digest=binding.effective_authority.authority_digest,
            runtime_eligibility_digest=binding.runtime_eligibility.eligibility_digest,
            node_attempt=attempt_number,
            claim_owner_id=owner_id,
            claim_fencing_token=fencing_token,
            capability=bound_input.capability,
            step_input_digest=bound_input.binding_digest,
            upstream_result_refs=bound_input.dependency_result_refs,
            consumed_result_refs=bound_input.consumed_result_refs,
            budget=plan_node.budget,
        )
        registration = self._executors.resolve_for_execution(
            context,
            bound_capability=bound_input.capability,
            bound_node_kind=DraftNodeKind.CAPABILITY,
        )
        attempt_id = self._attempt_id(
            execution.execution_id,
            node.node_id,
            attempt_number,
        )
        expires_at = now + timedelta(seconds=lease_seconds)
        attempt = self._build_attempt(
            attempt_id=attempt_id,
            execution_id=execution.execution_id,
            node_binding_id=binding.node_binding_id,
            run_id=execution.run_id,
            node_id=node.node_id,
            attempt=attempt_number,
            status="running",
            revision=1,
            claim_owner_id=owner_id,
            claim_fencing_token=fencing_token,
            claim_acquired_at=now,
            claim_expires_at=expires_at,
            input_manifest=bound_input.model_dump(mode="json"),
            input_digest=bound_input.binding_digest,
            context_manifest=context.model_dump(mode="json"),
            context_digest=context.context_digest,
            created_at=now,
            updated_at=now,
        )
        session.add(self._attempt_record(attempt))
        node.status = ExecutionNodeStatus.RUNNING.value
        node.attempt_count = attempt_number
        node.claim_fencing_token = fencing_token
        node.claim_owner_id = owner_id
        node.claim_acquired_at = now
        node.claim_heartbeat_at = now
        node.claim_expires_at = expires_at
        node.revision += 1
        node.updated_at = now
        await session.flush()
        return _CapabilityWork(
            execution=execution,
            plan=plan,
            plan_node=plan_node,
            binding=binding,
            attempt_id=attempt_id,
            attempt=attempt_number,
            persistence_owner_id=owner_id,
            persistence_fencing_token=fencing_token,
            bound_input=bound_input,
            context=context,
            registration=registration,
        )

    async def _claim_persisted_candidate(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        attempt_record: TaskLoopNodeAttemptRecord,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _CapabilityWork:
        attempt = self._attempt_from_record(attempt_record)
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == attempt.node_id)
            .with_for_update()
        )
        if node is None:
            raise CapabilityExecutionRuntimeNotFoundError("Candidate capability node disappeared")
        plan_node = self._plan_node(plan, node.node_id)
        binding = await self._load_binding(session, execution, node, plan_node)
        dependencies = await self._load_dependencies(
            session,
            execution,
            plan,
            node,
            require_complete=True,
        )
        current_input = self._input_bindings.bind_node(
            node_binding=binding,
            dependencies=dependencies,
        )
        persisted_input = BoundCapabilityInput.model_validate(attempt.input_manifest)
        context = CapabilityExecutionContext.model_validate(attempt.context_manifest)
        if (
            attempt.status != "awaiting_verification"
            or attempt.execution_id != execution.execution_id
            or attempt.node_binding_id != binding.node_binding_id
            or attempt.run_id != execution.run_id
            or attempt.attempt != node.attempt_count
            or node.status != ExecutionNodeStatus.AWAITING_VERIFICATION.value
            or attempt.input_digest != persisted_input.binding_digest
            or persisted_input != current_input
            or attempt.context_digest != context.context_digest
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted candidate lost its exact execution or dependency binding"
            )
        self._assert_context_scope(context, execution, plan_node, binding, persisted_input)
        if attempt.candidate_manifest is None or attempt.candidate_digest is None:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Awaiting-verification attempt has no immutable candidate"
            )
        candidate = CapabilityExecutionCandidate.model_validate(attempt.candidate_manifest)
        if candidate.candidate_digest != attempt.candidate_digest:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted capability candidate digest changed"
            )
        registration = self._executors.resolve_for_execution(
            context,
            bound_capability=persisted_input.capability,
            bound_node_kind=DraftNodeKind.CAPABILITY,
        )
        fencing_token = (
            max(
                node.claim_fencing_token,
                attempt.claim_fencing_token,
            )
            + 1
        )
        expires_at = now + timedelta(seconds=lease_seconds)
        updated = self._replace_attempt(
            attempt,
            revision=attempt.revision + 1,
            claim_owner_id=owner_id,
            claim_fencing_token=fencing_token,
            claim_acquired_at=now,
            claim_expires_at=expires_at,
            updated_at=now,
        )
        self._apply_attempt(attempt_record, updated)
        node.claim_owner_id = owner_id
        node.claim_fencing_token = fencing_token
        node.claim_acquired_at = now
        node.claim_heartbeat_at = now
        node.claim_expires_at = expires_at
        node.revision += 1
        node.updated_at = now
        await session.flush()
        return _CapabilityWork(
            execution=execution,
            plan=plan,
            plan_node=plan_node,
            binding=binding,
            attempt_id=attempt.attempt_id,
            attempt=attempt.attempt,
            persistence_owner_id=owner_id,
            persistence_fencing_token=fencing_token,
            bound_input=persisted_input,
            context=context,
            registration=registration,
            candidate=candidate,
        )

    async def _claim_approved_attempt(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        attempt_record: TaskLoopNodeAttemptRecord,
        *,
        owner_id: str,
        now: datetime,
        lease_seconds: int,
    ) -> _CapabilityWork:
        attempt = self._attempt_from_record(attempt_record)
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == attempt.node_id)
            .with_for_update()
        )
        approval_record = await session.scalar(
            select(TaskLoopCapabilityApprovalRecord)
            .where(
                TaskLoopCapabilityApprovalRecord.attempt_id == attempt.attempt_id
            )
            .with_for_update()
        )
        if node is None or approval_record is None:
            raise CapabilityExecutionRuntimeNotFoundError(
                "Approved capability attempt lost its node or authority"
            )
        approval = self._approval_from_record(approval_record)
        plan_node = self._plan_node(plan, node.node_id)
        binding = await self._load_binding(session, execution, node, plan_node)
        dependencies = await self._load_dependencies(
            session,
            execution,
            plan,
            node,
            require_complete=True,
        )
        current_input = self._input_bindings.bind_node(
            node_binding=binding,
            dependencies=dependencies,
        )
        persisted_input = BoundCapabilityInput.model_validate(attempt.input_manifest)
        registration = self._executors.resolve(current_input.capability)
        if (
            attempt.status != "prepared"
            or attempt.execution_id != execution.execution_id
            or attempt.node_binding_id != binding.node_binding_id
            or attempt.run_id != execution.run_id
            or attempt.attempt != node.attempt_count
            or node.status != ExecutionNodeStatus.READY.value
            or attempt.input_digest != persisted_input.binding_digest
            or persisted_input != current_input
            or attempt.candidate_manifest is not None
            or approval.status != "approved"
            or approval.execution_id != execution.execution_id
            or approval.task_id != execution.task_id
            or approval.run_id != execution.run_id
            or approval.node_id != node.node_id
            or approval.node_binding_id != binding.node_binding_id
            or approval.attempt_id != attempt.attempt_id
            or approval.attempt != attempt.attempt
            or approval.plan_generation != execution.plan_generation
            or approval.input_binding_digest != current_input.binding_digest
            or approval.executor_manifest_digest
            != registration.manifest.manifest_digest
            or registration.manifest.approval_requirement
            is not CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Approved capability attempt changed its exact execution binding"
            )
        fencing_token = max(node.claim_fencing_token, attempt.claim_fencing_token) + 1
        expires_at = now + timedelta(seconds=lease_seconds)
        context = CapabilityExecutionContext.build(
            task_id=execution.task_id,
            run_id=execution.run_id,
            plan_id=execution.plan_id,
            plan_generation=execution.plan_generation,
            plan_manifest_digest=execution.plan_manifest_digest,
            node_id=node.node_id,
            node_kind=DraftNodeKind.CAPABILITY,
            node_spec_digest=node.node_spec_digest,
            node_binding_id=binding.node_binding_id,
            node_binding_digest=binding.binding_digest,
            effective_authority_digest=binding.effective_authority.authority_digest,
            runtime_eligibility_digest=binding.runtime_eligibility.eligibility_digest,
            node_attempt=attempt.attempt,
            claim_owner_id=owner_id,
            claim_fencing_token=fencing_token,
            capability=current_input.capability,
            step_input_digest=current_input.binding_digest,
            upstream_result_refs=current_input.dependency_result_refs,
            consumed_result_refs=current_input.consumed_result_refs,
            budget=plan_node.budget,
        )
        updated = self._replace_attempt(
            attempt,
            status="running",
            revision=attempt.revision + 1,
            claim_owner_id=owner_id,
            claim_fencing_token=fencing_token,
            claim_acquired_at=now,
            claim_expires_at=expires_at,
            context_manifest=context.model_dump(mode="json"),
            context_digest=context.context_digest,
            updated_at=now,
        )
        self._apply_attempt(attempt_record, updated)
        node.status = ExecutionNodeStatus.RUNNING.value
        node.claim_owner_id = owner_id
        node.claim_fencing_token = fencing_token
        node.claim_acquired_at = now
        node.claim_heartbeat_at = now
        node.claim_expires_at = expires_at
        node.revision += 1
        node.updated_at = now
        await session.flush()
        return _CapabilityWork(
            execution=execution,
            plan=plan,
            plan_node=plan_node,
            binding=binding,
            attempt_id=attempt.attempt_id,
            attempt=attempt.attempt,
            persistence_owner_id=owner_id,
            persistence_fencing_token=fencing_token,
            bound_input=current_input,
            context=context,
            registration=registration,
            approval=approval,
        )

    async def _persist_approval(
        self,
        work: _CapabilityWork,
        preview: WorkspacePatchPreview,
    ) -> CapabilityRuntimeOutcome:
        now = self._now()
        approval_model = work.registration.approval_model
        if (
            work.registration.manifest.approval_requirement
            is not CapabilityApprovalRequirement.EXACT_CONFIRMATION_DIGEST
            or approval_model is not WorkspacePatchPreview
            or preview.task_id != work.execution.task_id
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability approval preview changed its registered Schema or Task"
            )
        approval = TaskLoopCapabilityApproval.request(
            execution_id=work.execution.execution_id,
            task_id=work.execution.task_id,
            run_id=work.execution.run_id,
            node_id=work.plan_node.node_id,
            node_binding_id=work.binding.node_binding_id,
            attempt_id=work.attempt_id,
            attempt=work.attempt,
            plan_generation=work.execution.plan_generation,
            input_binding_digest=work.bound_input.binding_digest,
            executor_manifest_digest=work.registration.manifest.manifest_digest,
            preview=preview,
            requested_execution_revision=work.execution.revision + 1,
            created_at=now,
        )
        async with self._database.session() as session, session.begin():
            attempt_record, attempt, node = await self._locked_attempt_scope(
                session,
                work,
                expected_attempt_status="running",
                expected_node_status=ExecutionNodeStatus.RUNNING.value,
                now=now,
            )
            execution_record = await session.scalar(
                select(TaskLoopExecutionRecord)
                .where(
                    TaskLoopExecutionRecord.execution_id
                    == work.execution.execution_id
                )
                .with_for_update()
            )
            if execution_record is None:
                raise CapabilityExecutionRuntimeNotFoundError(
                    "Capability approval execution disappeared"
                )
            current = self._execution_from_record(execution_record)
            if current != work.execution or current.status != "active":
                raise CapabilityExecutionRuntimeStaleFenceError(
                    "Capability approval execution revision is stale"
                )
            existing = await session.scalar(
                select(TaskLoopCapabilityApprovalRecord).where(
                    TaskLoopCapabilityApprovalRecord.execution_id
                    == work.execution.execution_id,
                    TaskLoopCapabilityApprovalRecord.node_id == work.plan_node.node_id,
                )
            )
            if existing is not None:
                raise CapabilityExecutionRuntimeConflictError(
                    "Capability node already has an approval request"
                )
            transitioned = self._transition_execution(
                session,
                execution_record,
                current,
                status="awaiting_user",
                kind="awaiting_user",
                now=now,
            )
            if transitioned.revision != approval.requested_execution_revision:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Capability approval requested revision changed"
                )
            updated_attempt = self._replace_attempt(
                attempt,
                status="prepared",
                revision=attempt.revision + 1,
                claim_owner_id=None,
                claim_expires_at=None,
                updated_at=now,
            )
            self._apply_attempt(attempt_record, updated_attempt)
            node.status = ExecutionNodeStatus.WAITING_USER.value
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
            session.add(self._approval_record(approval))
            await session.flush()
        return CapabilityRuntimeOutcome(
            execution_id=work.execution.execution_id,
            run_id=work.execution.run_id,
            node_id=work.plan_node.node_id,
            attempt_id=work.attempt_id,
            attempt=work.attempt,
            status="awaiting_user",
        )

    async def approve_workspace_patch(
        self,
        task_id: str,
        confirmation_digest: str,
        *,
        expected_execution_revision: int,
    ) -> WorkspacePatchPreview:
        """Consume no effect; only resume the exact revision carrying the preview."""

        now = self._now()
        async with self._database.session() as session, session.begin():
            execution_record = await self._execution_record_for_task(session, task_id)
            execution = self._execution_from_record(execution_record)
            approval_records = tuple(
                (
                    await session.scalars(
                        select(TaskLoopCapabilityApprovalRecord)
                        .where(
                            TaskLoopCapabilityApprovalRecord.execution_id
                            == execution.execution_id,
                            TaskLoopCapabilityApprovalRecord.status == "pending",
                        )
                        .with_for_update()
                    )
                ).all()
            )
            if len(approval_records) != 1:
                raise CapabilityExecutionRuntimeConflictError(
                    "Task Loop has no unique pending workspace patch approval"
                )
            approval_record = approval_records[0]
            approval = self._approval_from_record(approval_record)
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == approval.node_id)
                .with_for_update()
            )
            attempt_record = await session.scalar(
                select(TaskLoopNodeAttemptRecord)
                .where(TaskLoopNodeAttemptRecord.attempt_id == approval.attempt_id)
                .with_for_update()
            )
            if node is None or attempt_record is None:
                raise CapabilityExecutionRuntimeNotFoundError(
                    "Task Loop workspace patch approval scope disappeared"
                )
            attempt = self._attempt_from_record(attempt_record)
            if (
                execution.status != "awaiting_user"
                or execution.revision != expected_execution_revision
                or approval.requested_execution_revision != execution.revision
                or approval.run_id != execution.run_id
                or approval.plan_generation != execution.plan_generation
                or node.run_id != execution.run_id
                or node.status != ExecutionNodeStatus.WAITING_USER.value
                or node.node_id != approval.node_id
                or attempt.status != "prepared"
                or attempt.execution_id != execution.execution_id
                or attempt.node_id != node.node_id
                or attempt.node_binding_id != approval.node_binding_id
                or attempt.input_digest != approval.input_binding_digest
            ):
                raise CapabilityExecutionRuntimeStaleFenceError(
                    "Task Loop workspace patch approval revision is stale"
                )
            try:
                approved = approval.approve(
                    confirmation_digest=confirmation_digest,
                    expected_execution_revision=expected_execution_revision,
                    approved_at=now,
                )
            except ValueError as error:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Workspace patch confirmation digest or revision changed"
                ) from error
            self._apply_approval(approval_record, approved)
            self._transition_execution(
                session,
                execution_record,
                execution,
                status="active",
                kind="resumed",
                now=now,
            )
            node.status = ExecutionNodeStatus.READY.value
            node.revision += 1
            node.updated_at = now
            await session.flush()
            return WorkspacePatchPreview.model_validate(approved.preview_manifest)

    async def _persist_candidate(
        self,
        work: _CapabilityWork,
        candidate: CapabilityExecutionCandidate,
    ) -> None:
        now = self._now()
        self._assert_candidate(work, candidate)
        async with self._database.session() as session, session.begin():
            attempt_record, attempt, node = await self._locked_attempt_scope(
                session,
                work,
                expected_attempt_status="running",
                expected_node_status=ExecutionNodeStatus.RUNNING.value,
                now=now,
            )
            if attempt.candidate_manifest is not None:
                raise CapabilityExecutionRuntimeConflictError(
                    "Capability candidate is already persisted"
                )
            if work.approval is not None:
                approval_record = await session.scalar(
                    select(TaskLoopCapabilityApprovalRecord)
                    .where(
                        TaskLoopCapabilityApprovalRecord.approval_id
                        == work.approval.approval_id
                    )
                    .with_for_update()
                )
                if approval_record is None:
                    raise CapabilityExecutionRuntimeProofRejectedError(
                        "Approved capability lost its authority record"
                    )
                current_approval = self._approval_from_record(approval_record)
                receipt = candidate.output_manifest.get("receipt")
                if (
                    current_approval != work.approval
                    or current_approval.status != "approved"
                    or not isinstance(receipt, dict)
                    or receipt.get("task_id") != work.execution.task_id
                    or receipt.get("confirmation_digest")
                    != current_approval.confirmation_digest
                ):
                    raise CapabilityExecutionRuntimeProofRejectedError(
                        "Approved capability result changed its exact preview authority"
                    )
                consumed = current_approval.consume(
                    result_digest=candidate.result_digest,
                    consumed_at=now,
                )
                self._apply_approval(approval_record, consumed)
            updated = self._replace_attempt(
                attempt,
                status="awaiting_verification",
                revision=attempt.revision + 1,
                candidate_manifest=candidate.model_dump(mode="json"),
                candidate_digest=candidate.candidate_digest,
                candidate_recorded_at=now,
                updated_at=now,
            )
            self._apply_attempt(attempt_record, updated)
            node.status = ExecutionNodeStatus.AWAITING_VERIFICATION.value
            node.revision += 1
            node.updated_at = now
            await session.flush()

    async def _persist_verified(
        self,
        work: _CapabilityWork,
        verified: VerifiedCapabilityOutput,
    ) -> CapabilityRuntimeOutcome:
        now = self._now()
        candidate = work.candidate
        if candidate is None or verified.candidate != candidate:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability verification changed its immutable candidate"
            )
        result_ref = VerifiedCapabilityResultRef.build(
            task_id=work.execution.task_id,
            run_id=work.execution.run_id,
            plan_generation=work.execution.plan_generation,
            producer_node_id=work.plan_node.node_id,
            producer_attempt=work.attempt,
            capability=work.bound_input.capability,
            result_kind=candidate.result_kind,
            result_schema_digest=candidate.output_schema_digest,
            result_digest=candidate.result_digest,
            verification_digest=verified.verification_digest,
        )
        result_ref_identity = {
            "attempt_id": work.attempt_id,
            "result_ref_digest": result_ref.result_ref_digest,
        }
        result_ref_id = f"tlr_{sha256_digest(result_ref_identity)}"
        result = TaskLoopVerifiedResult(
            result_ref_id=result_ref_id,
            attempt_id=work.attempt_id,
            execution_id=work.execution.execution_id,
            node_binding_id=work.binding.node_binding_id,
            node_binding_digest=work.binding.binding_digest,
            run_id=work.execution.run_id,
            node_id=work.plan_node.node_id,
            producer_kind="capability_executor",
            capability_manifest=work.bound_input.capability.model_dump(mode="json"),
            capability_digest=sha256_digest(work.bound_input.capability.model_dump(mode="json")),
            agent_binding_manifest=None,
            agent_binding_digest=None,
            executor_manifest_digest=work.registration.manifest.manifest_digest,
            agent_result_proof_digest=None,
            input_binding_digest=work.bound_input.binding_digest,
            context_digest=work.context.context_digest,
            candidate_digest=candidate.candidate_digest,
            result_kind=result_ref.result_kind.value,
            output_manifest=candidate.output_manifest,
            output_schema_digest=candidate.output_schema_digest,
            output_digest=candidate.result_digest,
            verification_manifest=verified.model_dump(mode="json"),
            verification_digest=verified.verification_digest,
            result_ref_manifest=result_ref.model_dump(mode="json"),
            result_ref_digest=result_ref.result_ref_digest,
            created_at=now,
        )
        async with self._database.session() as session, session.begin():
            attempt_record, attempt, node = await self._locked_attempt_scope(
                session,
                work,
                expected_attempt_status="awaiting_verification",
                expected_node_status=ExecutionNodeStatus.AWAITING_VERIFICATION.value,
                now=now,
            )
            persisted_candidate = CapabilityExecutionCandidate.model_validate(
                attempt.candidate_manifest
            )
            if (
                persisted_candidate != candidate
                or attempt.candidate_digest != candidate.candidate_digest
                or attempt.verification_manifest is not None
            ):
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Capability candidate changed before verification persistence"
                )
            existing_attempt_result = await session.scalar(
                select(TaskLoopVerifiedResultRecord).where(
                    TaskLoopVerifiedResultRecord.attempt_id == work.attempt_id
                )
            )
            existing_node_results = tuple(
                (
                    await session.scalars(
                        select(TaskLoopVerifiedResultRecord).where(
                            TaskLoopVerifiedResultRecord.execution_id
                            == work.execution.execution_id,
                            TaskLoopVerifiedResultRecord.run_id == work.execution.run_id,
                            TaskLoopVerifiedResultRecord.node_id == work.plan_node.node_id,
                        )
                    )
                ).all()
            )
            if existing_attempt_result is not None or existing_node_results:
                raise CapabilityExecutionRuntimeConflictError(
                    "Capability node already has a persistent verified ResultRef"
                )
            session.add(self._verified_result_record(result))
            await session.flush()

            updated = self._replace_attempt(
                attempt,
                status="verified",
                revision=attempt.revision + 1,
                claim_expires_at=None,
                verification_manifest=verified.model_dump(mode="json"),
                verification_digest=verified.verification_digest,
                verified_at=now,
                updated_at=now,
            )
            self._apply_attempt(attempt_record, updated)
            node.status = ExecutionNodeStatus.VERIFIED.value
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
            await session.flush()
            await self._unlock_verified_successors(
                session,
                work.execution,
                work.plan,
                node.node_id,
                now=now,
            )
            await session.flush()
        return CapabilityRuntimeOutcome(
            execution_id=work.execution.execution_id,
            run_id=work.execution.run_id,
            node_id=work.plan_node.node_id,
            attempt_id=work.attempt_id,
            attempt=work.attempt,
            status="verified",
            result_ref=result_ref,
        )

    async def _settle_execution_error(
        self,
        work: _CapabilityWork,
        error: Exception,
    ) -> CapabilityRuntimeOutcome:
        now = self._now()
        no_replay = (
            work.registration.manifest.recovery_policy
            is CapabilityRecoveryPolicy.NO_AUTOMATIC_REPLAY
        )
        status: Literal["failed", "outcome_unknown"] = "outcome_unknown" if no_replay else "failed"
        error_code = (
            "CAPABILITY_OUTCOME_UNKNOWN"
            if no_replay
            else self._safe_error_code(error, "CAPABILITY_EXECUTION_FAILED")
        )
        async with self._database.session() as session, session.begin():
            attempt_record, attempt, node = await self._locked_attempt_scope(
                session,
                work,
                expected_attempt_status="running",
                expected_node_status=ExecutionNodeStatus.RUNNING.value,
                now=now,
                require_unexpired=False,
            )
            if attempt.candidate_manifest is not None:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Execution failure cannot overwrite a persisted candidate"
                )
            error_digest = self._error_digest(work, error_code)
            updated = self._replace_attempt(
                attempt,
                status=status,
                revision=attempt.revision + 1,
                claim_expires_at=None,
                error_code=error_code,
                error_digest=error_digest,
                updated_at=now,
            )
            self._apply_attempt(attempt_record, updated)
            node.status = ExecutionNodeStatus.FAILED.value
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
            await session.flush()
        return CapabilityRuntimeOutcome(
            execution_id=work.execution.execution_id,
            run_id=work.execution.run_id,
            node_id=work.plan_node.node_id,
            attempt_id=work.attempt_id,
            attempt=work.attempt,
            status=status,
            error_code=error_code,
        )

    async def _release_verification_claim(
        self,
        work: _CapabilityWork,
        error: Exception,
    ) -> None:
        now = self._now()
        async with self._database.session() as session, session.begin():
            attempt_record, attempt, node = await self._locked_attempt_scope(
                session,
                work,
                expected_attempt_status="awaiting_verification",
                expected_node_status=ExecutionNodeStatus.AWAITING_VERIFICATION.value,
                now=now,
                require_unexpired=False,
            )
            if attempt.candidate_digest != work.candidate.candidate_digest:  # type: ignore[union-attr]
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Verification failure observed a changed candidate"
                )
            receipt = {
                "schema_version": "deskpilot.capability-verification-deferred.v1",
                "attempt_id": work.attempt_id,
                "candidate_digest": attempt.candidate_digest,
                "error_code": self._safe_error_code(
                    error,
                    "CAPABILITY_VERIFICATION_FAILED",
                ),
            }
            updated = self._replace_attempt(
                attempt,
                revision=attempt.revision + 1,
                claim_expires_at=now,
                receipt_manifest=receipt,
                receipt_digest=sha256_digest(receipt),
                updated_at=now,
            )
            self._apply_attempt(attempt_record, updated)
            node.claim_expires_at = now
            node.claim_heartbeat_at = now
            node.revision += 1
            node.updated_at = now
            await session.flush()

    async def _recover_expired_running(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        *,
        now: datetime,
    ) -> None:
        records = tuple(
            (
                await session.scalars(
                    select(TaskLoopNodeAttemptRecord)
                    .where(
                        TaskLoopNodeAttemptRecord.execution_id == execution.execution_id,
                        TaskLoopNodeAttemptRecord.status == "running",
                    )
                    .with_for_update(skip_locked=True)
                )
            ).all()
        )
        for record in records:
            attempt = self._attempt_from_record(record)
            expires_at = self._optional_aware(attempt.claim_expires_at)
            if expires_at is not None and expires_at > now:
                continue
            if attempt.candidate_manifest is not None:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Running attempt contains an untracked candidate transition"
                )
            node = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == attempt.node_id)
                .with_for_update()
            )
            if node is None:
                raise CapabilityExecutionRuntimeNotFoundError("Expired capability node disappeared")
            plan_node = self._plan_node(plan, node.node_id)
            binding = await self._load_binding(session, execution, node, plan_node)
            context = CapabilityExecutionContext.model_validate(attempt.context_manifest)
            bound_input = BoundCapabilityInput.model_validate(attempt.input_manifest)
            self._assert_context_scope(context, execution, plan_node, binding, bound_input)
            registration = self._executors.resolve_for_execution(
                context,
                bound_capability=bound_input.capability,
                bound_node_kind=DraftNodeKind.CAPABILITY,
            )
            approval_record = await session.scalar(
                select(TaskLoopCapabilityApprovalRecord)
                .where(TaskLoopCapabilityApprovalRecord.attempt_id == attempt.attempt_id)
                .with_for_update()
            )
            approval = (
                self._approval_from_record(approval_record)
                if approval_record is not None
                else None
            )
            receipt_reconcile = (
                registration.manifest.recovery_policy
                is CapabilityRecoveryPolicy.RECEIPT_RECONCILE
                and approval is not None
                and approval.status == "approved"
                and approval.execution_id == execution.execution_id
                and approval.node_id == node.node_id
                and approval.attempt_id == attempt.attempt_id
                and approval.input_binding_digest == bound_input.binding_digest
            )
            if receipt_reconcile:
                updated = self._replace_attempt(
                    attempt,
                    status="prepared",
                    revision=attempt.revision + 1,
                    claim_owner_id=None,
                    claim_acquired_at=None,
                    claim_expires_at=None,
                    error_code=None,
                    error_digest=None,
                    updated_at=now,
                )
                self._apply_attempt(record, updated)
                node.status = ExecutionNodeStatus.READY.value
                node.claim_owner_id = None
                node.claim_acquired_at = None
                node.claim_heartbeat_at = None
                node.claim_expires_at = None
                node.revision += 1
                node.updated_at = now
                continue
            safe_retry = (
                registration.manifest.recovery_policy
                is CapabilityRecoveryPolicy.DETERMINISTIC_RETRY
                and node.attempt_count < plan_node.budget.retries + 1
            )
            status: Literal["failed", "outcome_unknown"] = (
                "failed" if safe_retry else "outcome_unknown"
            )
            error_code = (
                "CAPABILITY_ATTEMPT_LEASE_EXPIRED"
                if safe_retry
                else "CAPABILITY_OUTCOME_UNKNOWN_AFTER_LEASE"
            )
            updated = self._replace_attempt(
                attempt,
                status=status,
                revision=attempt.revision + 1,
                claim_expires_at=None,
                error_code=error_code,
                error_digest=self._error_digest_values(
                    attempt.attempt_id,
                    attempt.context_digest,
                    error_code,
                ),
                updated_at=now,
            )
            self._apply_attempt(record, updated)
            node.status = (
                ExecutionNodeStatus.READY.value if safe_retry else ExecutionNodeStatus.FAILED.value
            )
            node.claim_owner_id = None
            node.claim_acquired_at = None
            node.claim_heartbeat_at = None
            node.claim_expires_at = None
            node.revision += 1
            node.updated_at = now
        await session.flush()

    async def _locked_attempt_scope(
        self,
        session: AsyncSession,
        work: _CapabilityWork,
        *,
        expected_attempt_status: str,
        expected_node_status: str,
        now: datetime,
        require_unexpired: bool = True,
    ) -> tuple[
        TaskLoopNodeAttemptRecord,
        TaskLoopNodeAttempt,
        TaskExecutionNodeRecord,
    ]:
        attempt_record = await session.scalar(
            select(TaskLoopNodeAttemptRecord)
            .where(TaskLoopNodeAttemptRecord.attempt_id == work.attempt_id)
            .with_for_update()
        )
        node = await session.scalar(
            select(TaskExecutionNodeRecord)
            .where(TaskExecutionNodeRecord.node_id == work.plan_node.node_id)
            .with_for_update()
        )
        if attempt_record is None or node is None:
            raise CapabilityExecutionRuntimeNotFoundError(
                "Capability attempt or execution node disappeared"
            )
        attempt = self._attempt_from_record(attempt_record)
        expires_at = self._optional_aware(attempt.claim_expires_at)
        if (
            attempt.status != expected_attempt_status
            or node.status != expected_node_status
            or attempt.execution_id != work.execution.execution_id
            or attempt.node_binding_id != work.binding.node_binding_id
            or attempt.run_id != work.execution.run_id
            or attempt.node_id != work.plan_node.node_id
            or attempt.attempt != work.attempt
            or attempt.claim_owner_id != work.persistence_owner_id
            or attempt.claim_fencing_token != work.persistence_fencing_token
            or node.run_id != work.execution.run_id
            or node.attempt_count != work.attempt
            or node.claim_owner_id != work.persistence_owner_id
            or node.claim_fencing_token != work.persistence_fencing_token
            or (require_unexpired and (expires_at is None or expires_at <= now))
        ):
            raise CapabilityExecutionRuntimeStaleFenceError(
                "Capability worker lease or fencing token is stale"
            )
        bound_input = BoundCapabilityInput.model_validate(attempt.input_manifest)
        context = CapabilityExecutionContext.model_validate(attempt.context_manifest)
        if bound_input != work.bound_input or context != work.context:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability attempt input or context changed after claim"
            )
        return attempt_record, attempt, node

    async def _execution_record_for_task(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> TaskLoopExecutionRecord:
        records = tuple(
            (
                await session.scalars(
                    select(TaskLoopExecutionRecord)
                    .where(TaskLoopExecutionRecord.task_id == task_id)
                    .with_for_update()
                )
            ).all()
        )
        if not records:
            raise CapabilityExecutionRuntimeNotFoundError(
                "Task has no activated model-planner execution"
            )
        if len(records) != 1:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Task has multiple model-planner executions"
            )
        return records[0]

    async def _locked_run(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
    ) -> TaskExecutionRunRecord:
        run = await session.scalar(
            select(TaskExecutionRunRecord)
            .where(TaskExecutionRunRecord.run_id == execution.run_id)
            .with_for_update()
        )
        if run is None:
            raise CapabilityExecutionRuntimeNotFoundError("Task Loop execution Run disappeared")
        if (
            run.task_id != execution.task_id
            or run.plan_generation != execution.plan_generation
            or run.plan_digest != execution.plan_manifest_digest
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Task Loop Run crossed its exact Plan lineage"
            )
        return run

    async def _load_plan(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
    ) -> ExecutablePlan:
        record = await session.get(
            TaskPlanGenerationRecord,
            (execution.task_id, execution.plan_generation),
        )
        if record is None:
            raise CapabilityExecutionRuntimeNotFoundError("Task Loop executable Plan disappeared")
        try:
            plan = ExecutablePlan.model_validate(record.manifest)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted executable Plan manifest is invalid"
            ) from error
        if (
            record.task_id != plan.task_id
            or record.generation != plan.plan_generation
            or record.plan_id != plan.plan_id
            or record.contract_version != plan.task_contract.version
            or record.contract_digest != plan.task_contract.digest
            or record.plan_manifest_digest != plan.plan_manifest_digest
            or record.status != "active"
            or plan.task_id != execution.task_id
            or plan.plan_id != execution.plan_id
            or plan.plan_generation != execution.plan_generation
            or plan.plan_manifest_digest != execution.plan_manifest_digest
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Executable Plan columns or Task Loop lineage changed"
            )
        return plan

    async def _load_binding(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        node: TaskExecutionNodeRecord,
        plan_node: ExecutablePlanNode,
    ) -> ModelPlannerNodeBinding:
        records = tuple(
            (
                await session.scalars(
                    select(ModelPlannerNodeBindingRecord).where(
                        ModelPlannerNodeBindingRecord.execution_id == execution.execution_id,
                        ModelPlannerNodeBindingRecord.composite_node_id == node.node_id,
                    )
                )
            ).all()
        )
        if len(records) != 1:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability node has no unique exact model-planner binding"
            )
        binding = self._binding_from_record(records[0])
        authority = binding.effective_authority
        eligibility = binding.runtime_eligibility
        capability = authority.capability
        if (
            binding.task_id != execution.task_id
            or binding.composite_plan_id != execution.plan_id
            or binding.composite_plan_manifest_digest != execution.plan_manifest_digest
            or binding.composite_node_id != node.node_id
            or binding.composite_node_spec_digest != node.node_spec_digest
            or binding.mapping.composite_local_key != node.local_key
            or binding.mapping.composite_node_id != node.node_id
            or authority.node_kind is not DraftNodeKind.CAPABILITY
            or authority.bound_agent is not None
            or capability is None
            or eligibility.runtime_kind != "capability_executor"
            or not eligibility.runtime_enabled
            or eligibility.capability != capability
            or eligibility.executor_manifest_digest is None
            or plan_node.node_id != node.node_id
            or plan_node.local_key != node.local_key
            or plan_node.kind is not DraftNodeKind.CAPABILITY
            or plan_node.node_spec_digest != node.node_spec_digest
            or plan_node.bound_agent is not None
            or plan_node.capability != capability
            or plan_node.budget != authority.budget
            or tuple(node.depends_on) != plan_node.depends_on
            or node.handoff_parent_node_id != plan_node.handoff_parent_node_id
            or node.bound_agent is not None
            or node.capability != capability.model_dump(mode="json")
            or tuple(node.acceptance_refs) != plan_node.acceptance_refs
            or node.budget != plan_node.budget.model_dump(mode="json")
            or node.runtime_enabled is not plan_node.runtime_enabled
            or not node.runtime_enabled
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability Plan node or effective authority changed"
            )
        registration = self._executors.resolve(capability)
        if registration.manifest.manifest_digest != eligibility.executor_manifest_digest:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability executor registration changed after activation"
            )
        return binding

    async def _load_dependencies(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        node: TaskExecutionNodeRecord,
        *,
        require_complete: bool,
    ) -> tuple[ResolvedVerifiedCapabilityResult, ...]:
        incoming = tuple(
            (
                await session.scalars(
                    select(TaskExecutionEdgeRecord)
                    .where(
                        TaskExecutionEdgeRecord.run_id == execution.run_id,
                        TaskExecutionEdgeRecord.to_node_id == node.node_id,
                    )
                    .order_by(TaskExecutionEdgeRecord.from_node_id)
                )
            ).all()
        )
        source_ids = tuple(item.from_node_id for item in incoming)
        expected_ids = tuple(sorted(node.depends_on))
        if (
            source_ids != expected_ids
            or len(source_ids) != len(set(source_ids))
            or any(
                item.requirement != "verified"
                or item.condition_manifest is not None
                or item.condition_digest is not None
                or item.decision_manifest is not None
                or item.decision_digest is not None
                for item in incoming
            )
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability dependency edges changed or acquired a server condition"
            )
        resolved: list[ResolvedVerifiedCapabilityResult] = []
        for source_id in source_ids:
            source_plan_node = self._plan_node(plan, source_id)
            source = await session.get(TaskExecutionNodeRecord, source_id)
            if source is None or source.run_id != execution.run_id:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Capability dependency crosses its exact Run"
                )
            self._assert_node_matches_plan(source, source_plan_node)
            if source.status != ExecutionNodeStatus.VERIFIED.value:
                if require_complete:
                    raise CapabilityExecutionRuntimeProofRejectedError(
                        "Runnable capability has an unverified predecessor"
                    )
                return ()
            result = await self._load_verified_result(
                session,
                execution,
                source,
            )
            if result is None:
                if require_complete:
                    raise CapabilityExecutionRuntimeProofRejectedError(
                        "Verified predecessor has no persistent verified ResultRef"
                    )
                return ()
            resolved.append(result)
        return tuple(resolved)

    async def _load_verified_result(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        source: TaskExecutionNodeRecord,
    ) -> ResolvedVerifiedCapabilityResult | None:
        records = tuple(
            (
                await session.scalars(
                    select(TaskLoopVerifiedResultRecord).where(
                        TaskLoopVerifiedResultRecord.execution_id == execution.execution_id,
                        TaskLoopVerifiedResultRecord.run_id == execution.run_id,
                        TaskLoopVerifiedResultRecord.node_id == source.node_id,
                    )
                )
            ).all()
        )
        if not records:
            return None
        if len(records) != 1:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Dependency node has more than one persistent verified ResultRef"
            )
        record = records[0]
        result = self._verified_result_from_record(record)
        try:
            result_ref = VerifiedCapabilityResultRef.model_validate(result.result_ref_manifest)
            capability = CapabilityRef.model_validate(result.capability_manifest)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted dependency ResultRef manifest is invalid"
            ) from error
        if (
            result.execution_id != execution.execution_id
            or result.run_id != execution.run_id
            or result.node_id != source.node_id
            or result_ref.task_id != execution.task_id
            or result_ref.run_id != execution.run_id
            or result_ref.plan_generation != execution.plan_generation
            or result_ref.producer_node_id != source.node_id
            or result_ref.capability != capability
            or result_ref.result_kind.value != result.result_kind
            or result_ref.result_schema_digest != result.output_schema_digest
            or result_ref.result_digest != result.output_digest
            or result_ref.verification_digest != result.verification_digest
            or result_ref.result_ref_digest != result.result_ref_digest
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persistent dependency ResultRef crosses task, run, node, or proof scope"
            )
        attempt_record = await session.get(
            TaskLoopNodeAttemptRecord,
            result.attempt_id,
        )
        if attempt_record is None:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persistent dependency ResultRef lost its producer attempt"
            )
        attempt = self._attempt_from_record(attempt_record)
        binding_record = await session.get(
            ModelPlannerNodeBindingRecord,
            result.node_binding_id,
        )
        if binding_record is None:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persistent dependency ResultRef lost its node binding"
            )
        binding = self._binding_from_record(binding_record)
        if (
            attempt.status != "verified"
            or attempt.execution_id != execution.execution_id
            or attempt.run_id != execution.run_id
            or attempt.node_id != source.node_id
            or attempt.attempt != result_ref.producer_attempt
            or attempt.node_binding_id != result.node_binding_id
            or attempt.input_digest != result.input_binding_digest
            or attempt.context_digest != result.context_digest
            or binding_record.execution_id != execution.execution_id
            or binding.composite_node_id != source.node_id
            or binding.binding_digest != result.node_binding_digest
            or binding.effective_authority.capability != result_ref.capability
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persistent ResultRef producer attempt changed"
            )
        if result.producer_kind == "capability_executor":
            self._assert_capability_result_proof(result, result_ref, attempt)
        return ResolvedVerifiedCapabilityResult(
            result_ref=result_ref,
            output_manifest=result.output_manifest,
            output_schema_digest=result.output_schema_digest,
        )

    def _assert_capability_result_proof(
        self,
        result: TaskLoopVerifiedResult,
        result_ref: VerifiedCapabilityResultRef,
        attempt: TaskLoopNodeAttempt,
    ) -> None:
        if (
            attempt.candidate_manifest is None
            or attempt.verification_manifest is None
            or attempt.candidate_digest is None
            or attempt.verification_digest is None
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability ResultRef producer has no candidate or verification proof"
            )
        try:
            candidate = CapabilityExecutionCandidate.model_validate(attempt.candidate_manifest)
            verified = VerifiedCapabilityOutput.model_validate(attempt.verification_manifest)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability ResultRef candidate or verification proof is invalid"
            ) from error
        registration = self._executors.resolve(result_ref.capability)
        if (
            result.executor_manifest_digest != registration.manifest.manifest_digest
            or result.candidate_digest != candidate.candidate_digest
            or attempt.candidate_digest != candidate.candidate_digest
            or verified.candidate != candidate
            or result.verification_manifest != verified.model_dump(mode="json")
            or result.verification_digest != verified.verification_digest
            or attempt.verification_digest != verified.verification_digest
            or result.input_binding_digest != candidate.input_binding_digest
            or result.context_digest != candidate.context_digest
            or result.result_kind != candidate.result_kind.value
            or result.output_manifest != candidate.output_manifest
            or result.output_schema_digest != candidate.output_schema_digest
            or result.output_digest != candidate.result_digest
            or result_ref.result_digest != candidate.result_digest
            or result_ref.result_schema_digest != candidate.output_schema_digest
            or result_ref.verification_digest != verified.verification_digest
            or candidate.output_schema_digest
            != sha256_digest(registration.output_model.model_json_schema())
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability ResultRef proof material changed"
            )

    async def _unlock_verified_successors(
        self,
        session: AsyncSession,
        execution: TaskLoopExecution,
        plan: ExecutablePlan,
        source_node_id: str,
        *,
        now: datetime,
    ) -> None:
        outgoing = tuple(
            (
                await session.scalars(
                    select(TaskExecutionEdgeRecord)
                    .where(
                        TaskExecutionEdgeRecord.run_id == execution.run_id,
                        TaskExecutionEdgeRecord.from_node_id == source_node_id,
                    )
                    .order_by(TaskExecutionEdgeRecord.to_node_id)
                )
            ).all()
        )
        for edge in outgoing:
            if (
                edge.requirement != "verified"
                or edge.condition_manifest is not None
                or edge.condition_digest is not None
                or edge.decision_manifest is not None
                or edge.decision_digest is not None
            ):
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Capability successor edge acquired an unsupported condition"
                )
            target = await session.scalar(
                select(TaskExecutionNodeRecord)
                .where(TaskExecutionNodeRecord.node_id == edge.to_node_id)
                .with_for_update()
            )
            if target is None:
                raise CapabilityExecutionRuntimeProofRejectedError(
                    "Capability successor disappeared"
                )
            target_plan_node = self._plan_node(plan, target.node_id)
            self._assert_node_matches_plan(target, target_plan_node)
            if target.status != ExecutionNodeStatus.PENDING.value:
                continue
            dependencies = await self._load_dependencies(
                session,
                execution,
                plan,
                target,
                require_complete=False,
            )
            if len(dependencies) != len(target.depends_on):
                continue
            target.status = ExecutionNodeStatus.READY.value
            target.revision += 1
            target.updated_at = now

    @staticmethod
    def _plan_node(plan: ExecutablePlan, node_id: str) -> ExecutablePlanNode:
        node = next((item for item in plan.nodes if item.node_id == node_id), None)
        if node is None:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Task Loop references a node outside its exact Plan"
            )
        return node

    @staticmethod
    def _assert_node_matches_plan(
        record: TaskExecutionNodeRecord,
        node: ExecutablePlanNode,
    ) -> None:
        if (
            record.node_id != node.node_id
            or record.local_key != node.local_key
            or record.node_kind != node.kind.value
            or record.node_spec_digest != node.node_spec_digest
            or tuple(record.depends_on) != node.depends_on
            or record.handoff_parent_node_id != node.handoff_parent_node_id
            or record.bound_agent
            != (node.bound_agent.model_dump(mode="json") if node.bound_agent is not None else None)
            or record.capability
            != (node.capability.model_dump(mode="json") if node.capability is not None else None)
            or tuple(record.acceptance_refs) != node.acceptance_refs
            or record.budget != node.budget.model_dump(mode="json")
            or record.runtime_enabled is not node.runtime_enabled
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Execution node columns changed from the exact Plan"
            )

    @staticmethod
    def _execution_from_record(record: TaskLoopExecutionRecord) -> TaskLoopExecution:
        try:
            execution = TaskLoopExecution.model_validate(record.manifest)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted Task Loop execution manifest is invalid"
            ) from error
        direct_fields = (
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
            "execution_digest",
        )
        if (
            any(getattr(record, field) != getattr(execution, field) for field in direct_fields)
            or record.manifest != execution.model_dump(mode="json")
            or CapabilityExecutionRuntime._aware(record.created_at) != execution.created_at
            or CapabilityExecutionRuntime._aware(record.updated_at) != execution.updated_at
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Task Loop execution columns changed from its manifest"
            )
        return execution

    @staticmethod
    def _binding_from_record(
        record: ModelPlannerNodeBindingRecord,
    ) -> ModelPlannerNodeBinding:
        try:
            binding = ModelPlannerNodeBinding.model_validate(record.manifest)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted model-planner node binding is invalid"
            ) from error
        direct_fields = (
            "node_binding_id",
            "task_id",
            "user_message_id",
            "draft_id",
            "step_binding_id",
            "step_binding_digest",
            "step_ordinal",
            "offer_id",
            "offer_key",
            "offer_digest",
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
            "parameter_bindings_digest",
            "bound_input_digest",
            "binding_digest",
        )
        if (
            any(getattr(record, field) != getattr(binding, field) for field in direct_fields)
            or record.recipe_manifest != binding.recipe.model_dump(mode="json")
            or record.recipe_digest != binding.recipe.route_manifest_digest
            or record.mapping_manifest != binding.mapping.model_dump(mode="json")
            or record.mapping_digest != binding.mapping.mapping_digest
            or record.parameter_bindings_manifest
            != [item.model_dump(mode="json") for item in binding.parameter_bindings]
            or record.bound_input_manifest != binding.bound_input_manifest
            or record.effective_authority_manifest
            != binding.effective_authority.model_dump(mode="json")
            or record.effective_authority_digest != binding.effective_authority.authority_digest
            or record.runtime_eligibility_manifest
            != binding.runtime_eligibility.model_dump(mode="json")
            or record.runtime_eligibility_digest != binding.runtime_eligibility.eligibility_digest
            or record.manifest != binding.model_dump(mode="json")
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Model-planner node-binding columns changed from its manifest"
            )
        return binding

    @staticmethod
    def _assert_context_scope(
        context: CapabilityExecutionContext,
        execution: TaskLoopExecution,
        node: ExecutablePlanNode,
        binding: ModelPlannerNodeBinding,
        bound_input: BoundCapabilityInput,
    ) -> None:
        if (
            context.task_id != execution.task_id
            or context.run_id != execution.run_id
            or context.plan_id != execution.plan_id
            or context.plan_generation != execution.plan_generation
            or context.plan_manifest_digest != execution.plan_manifest_digest
            or context.node_id != node.node_id
            or context.node_kind is not DraftNodeKind.CAPABILITY
            or context.node_spec_digest != node.node_spec_digest
            or context.node_binding_id != binding.node_binding_id
            or context.node_binding_digest != binding.binding_digest
            or context.effective_authority_digest != binding.effective_authority.authority_digest
            or context.runtime_eligibility_digest != binding.runtime_eligibility.eligibility_digest
            or context.node_attempt < 1
            or context.capability != bound_input.capability
            or context.step_input_digest != bound_input.binding_digest
            or context.upstream_result_refs != bound_input.dependency_result_refs
            or context.consumed_result_refs != bound_input.consumed_result_refs
            or context.budget != node.budget
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability execution context changed from its exact Plan binding"
            )

    @staticmethod
    def _assert_candidate(
        work: _CapabilityWork,
        candidate: CapabilityExecutionCandidate,
    ) -> None:
        if (
            candidate.task_id != work.execution.task_id
            or candidate.node_id != work.plan_node.node_id
            or candidate.context_digest != work.context.context_digest
            or candidate.capability != work.bound_input.capability
            or candidate.input_binding_digest != work.bound_input.binding_digest
            or candidate.arguments_digest != work.bound_input.arguments_digest
            or candidate.result_kind is not work.registration.manifest.produces
            or candidate.output_schema_digest
            != sha256_digest(work.registration.output_model.model_json_schema())
        ):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability executor returned a candidate for another exact binding"
            )

    @staticmethod
    def _transition_execution(
        session: AsyncSession,
        record: TaskLoopExecutionRecord,
        current: TaskLoopExecution,
        *,
        status: Literal["active", "awaiting_user"],
        kind: Literal["awaiting_user", "resumed"],
        now: datetime,
    ) -> TaskLoopExecution:
        effective_now = max(now, current.updated_at)
        transitioned, event = current.transition(
            status=status,
            kind=kind,
            updated_at=effective_now,
        )
        record.status = transitioned.status
        record.revision = transitioned.revision
        record.event_count = transitioned.event_count
        record.latest_event_id = transitioned.latest_event_id
        record.latest_event_digest = transitioned.latest_event_digest
        record.manifest = transitioned.model_dump(mode="json")
        record.execution_digest = transitioned.execution_digest
        record.updated_at = transitioned.updated_at
        session.add(
            TaskLoopExecutionEventRecord(
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
        )
        return transitioned

    @staticmethod
    def _approval_record(
        approval: TaskLoopCapabilityApproval,
    ) -> TaskLoopCapabilityApprovalRecord:
        return TaskLoopCapabilityApprovalRecord(
            **approval.model_dump(mode="python", exclude={"schema_version"}),
            manifest=approval.model_dump(mode="json"),
        )

    @staticmethod
    def _apply_approval(
        record: TaskLoopCapabilityApprovalRecord,
        approval: TaskLoopCapabilityApproval,
    ) -> None:
        values = approval.model_dump(mode="python")
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
            "approved_at",
            "consumed_at",
            "result_digest",
            "approval_digest",
            "created_at",
            "updated_at",
        ):
            setattr(record, field, values[field])
        record.manifest = approval.model_dump(mode="json")

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
            "approved_at": cls._optional_aware(record.approved_at),
            "consumed_at": cls._optional_aware(record.consumed_at),
            "created_at": cls._aware(record.created_at),
            "updated_at": cls._aware(record.updated_at),
        }
        try:
            approval = TaskLoopCapabilityApproval.model_validate(values)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted capability approval is invalid"
            ) from error
        if record.manifest != approval.model_dump(mode="json"):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability approval columns changed from its manifest"
            )
        return approval

    @classmethod
    def _build_attempt(cls, **values: Any) -> TaskLoopNodeAttempt:
        material: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-node-attempt.v1",
            "candidate_manifest": None,
            "candidate_digest": None,
            "candidate_recorded_at": None,
            "verification_manifest": None,
            "verification_digest": None,
            "verified_at": None,
            "receipt_manifest": None,
            "receipt_digest": None,
            "error_code": None,
            "error_digest": None,
            **values,
        }
        return TaskLoopNodeAttempt.model_validate(
            {**material, "attempt_digest": sha256_digest(material)}
        )

    @classmethod
    def _replace_attempt(
        cls,
        attempt: TaskLoopNodeAttempt,
        **changes: Any,
    ) -> TaskLoopNodeAttempt:
        material = attempt.model_dump(mode="python", exclude={"attempt_digest"})
        material.update(changes)
        return TaskLoopNodeAttempt.model_validate(
            {**material, "attempt_digest": sha256_digest(material)}
        )

    @staticmethod
    def _attempt_record(attempt: TaskLoopNodeAttempt) -> TaskLoopNodeAttemptRecord:
        return TaskLoopNodeAttemptRecord(
            **attempt.model_dump(mode="python", exclude={"schema_version"}),
            manifest=attempt.model_dump(mode="json"),
        )

    @staticmethod
    def _apply_attempt(
        record: TaskLoopNodeAttemptRecord,
        attempt: TaskLoopNodeAttempt,
    ) -> None:
        fields = (
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
            "attempt_digest",
            "created_at",
            "updated_at",
        )
        values = attempt.model_dump(mode="python")
        for field in fields:
            setattr(record, field, values[field])
        record.manifest = attempt.model_dump(mode="json")

    @classmethod
    def _attempt_from_record(
        cls,
        record: TaskLoopNodeAttemptRecord,
    ) -> TaskLoopNodeAttempt:
        values = {
            "schema_version": "deskpilot.task-loop-node-attempt.v1",
            "attempt_id": record.attempt_id,
            "execution_id": record.execution_id,
            "node_binding_id": record.node_binding_id,
            "run_id": record.run_id,
            "node_id": record.node_id,
            "attempt": record.attempt,
            "status": record.status,
            "revision": record.revision,
            "claim_owner_id": record.claim_owner_id,
            "claim_fencing_token": record.claim_fencing_token,
            "claim_acquired_at": cls._optional_aware(record.claim_acquired_at),
            "claim_expires_at": cls._optional_aware(record.claim_expires_at),
            "input_manifest": record.input_manifest,
            "input_digest": record.input_digest,
            "context_manifest": record.context_manifest,
            "context_digest": record.context_digest,
            "candidate_manifest": record.candidate_manifest,
            "candidate_digest": record.candidate_digest,
            "candidate_recorded_at": cls._optional_aware(record.candidate_recorded_at),
            "verification_manifest": record.verification_manifest,
            "verification_digest": record.verification_digest,
            "verified_at": cls._optional_aware(record.verified_at),
            "receipt_manifest": record.receipt_manifest,
            "receipt_digest": record.receipt_digest,
            "error_code": record.error_code,
            "error_digest": record.error_digest,
            "created_at": cls._aware(record.created_at),
            "updated_at": cls._aware(record.updated_at),
            "attempt_digest": record.attempt_digest,
        }
        try:
            attempt = TaskLoopNodeAttempt.model_validate(values)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted capability attempt is invalid"
            ) from error
        if record.manifest != attempt.model_dump(mode="json"):
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Capability attempt columns changed from its manifest"
            )
        return attempt

    @staticmethod
    def _verified_result_record(
        result: TaskLoopVerifiedResult,
    ) -> TaskLoopVerifiedResultRecord:
        return TaskLoopVerifiedResultRecord(
            **result.model_dump(mode="python", exclude={"schema_version"})
        )

    @classmethod
    def _verified_result_from_record(
        cls,
        record: TaskLoopVerifiedResultRecord,
    ) -> TaskLoopVerifiedResult:
        values = {
            "schema_version": "deskpilot.task-loop-verified-result.v1",
            "result_ref_id": record.result_ref_id,
            "attempt_id": record.attempt_id,
            "execution_id": record.execution_id,
            "node_binding_id": record.node_binding_id,
            "node_binding_digest": record.node_binding_digest,
            "run_id": record.run_id,
            "node_id": record.node_id,
            "producer_kind": record.producer_kind,
            "capability_manifest": record.capability_manifest,
            "capability_digest": record.capability_digest,
            "agent_binding_manifest": record.agent_binding_manifest,
            "agent_binding_digest": record.agent_binding_digest,
            "executor_manifest_digest": record.executor_manifest_digest,
            "agent_result_proof_digest": record.agent_result_proof_digest,
            "input_binding_digest": record.input_binding_digest,
            "context_digest": record.context_digest,
            "candidate_digest": record.candidate_digest,
            "result_kind": record.result_kind,
            "output_manifest": record.output_manifest,
            "output_schema_digest": record.output_schema_digest,
            "output_digest": record.output_digest,
            "verification_manifest": record.verification_manifest,
            "verification_digest": record.verification_digest,
            "result_ref_manifest": record.result_ref_manifest,
            "result_ref_digest": record.result_ref_digest,
            "created_at": cls._aware(record.created_at),
        }
        try:
            return TaskLoopVerifiedResult.model_validate(values)
        except ValidationError as error:
            raise CapabilityExecutionRuntimeProofRejectedError(
                "Persisted Task Loop verified result is invalid"
            ) from error

    @staticmethod
    def _attempt_id(execution_id: str, node_id: str, attempt: int) -> str:
        identity = {
            "execution_id": execution_id,
            "node_id": node_id,
            "attempt": attempt,
        }
        return f"tla_{sha256_digest(identity)}"

    @staticmethod
    def _safe_error_code(error: Exception, fallback: str) -> str:
        value = getattr(error, "code", fallback)
        if isinstance(value, str) and _ERROR_CODE_PATTERN.fullmatch(value):
            return value
        return fallback

    @staticmethod
    def _error_digest(work: _CapabilityWork, error_code: str) -> str:
        return CapabilityExecutionRuntime._error_digest_values(
            work.attempt_id,
            work.context.context_digest,
            error_code,
        )

    @staticmethod
    def _error_digest_values(
        attempt_id: str,
        context_digest: str,
        error_code: str,
    ) -> str:
        return sha256_digest(
            {
                "attempt_id": attempt_id,
                "context_digest": context_digest,
                "error_code": error_code,
            }
        )

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("Capability execution clock must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value

    @staticmethod
    def _optional_aware(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return CapabilityExecutionRuntime._aware(value)


__all__ = [
    "CapabilityExecutionEnginePort",
    "CapabilityExecutionRuntime",
    "CapabilityExecutionRuntimeConflictError",
    "CapabilityExecutionRuntimeError",
    "CapabilityExecutionRuntimeNotFoundError",
    "CapabilityExecutionRuntimeProofRejectedError",
    "CapabilityExecutionRuntimeStaleFenceError",
    "CapabilityRuntimeOutcome",
    "CapabilityRuntimeOutcomeStatus",
    "CapabilityVerificationDeferredError",
]
