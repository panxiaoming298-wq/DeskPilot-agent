"""Task command/query service with transactional event persistence."""

import asyncio
import hashlib
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.task_checkpoint_codec import (
    TaskCheckpointCodec,
    TaskCheckpointInvalidError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.approvals import (
    ApprovalDecision,
    ApprovalRead,
    ApprovalResolutionRead,
    ApprovalResourceRead,
    ApprovalStatus,
    DataEgress,
)
from deskpilot.domain.model_contracts import PrivacyMode
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import (
    ReconciliationAttemptRead,
    ReconciliationCompensationRead,
    ReconciliationEvidenceKind,
    ReconciliationEvidenceRefreshRead,
    ReconciliationOutcome,
    ReconciliationRead,
    ReconciliationReceiptEvidenceRead,
    ReconciliationResolutionRead,
    ReconciliationStatus,
    ToolIdempotencyReceiptRead,
)
from deskpilot.domain.schemas import (
    FileMoveCompensationRequest,
    TaskCreate,
    TaskEventRead,
    TaskHistoryRead,
    TaskRead,
    TaskStatus,
)
from deskpilot.domain.task_checkpoints import (
    DurableTaskCheckpoint,
    TaskCheckpointPayload,
    initial_tool_call_id,
)
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import ToolIdempotency, ToolRiskLevel
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ApprovalRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    TaskRecord,
    TaskRuntimeCheckpointRecord,
    ToolCallRecord,
    ToolCommitReceiptRecord,
    ToolIdempotencyReceiptRecord,
    ToolReconciliationEvidenceRecord,
    ToolReconciliationIdempotencyRecord,
    ToolReconciliationRecord,
    utc_now,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FILE_MOVE_DESTINATION_CAPABILITY,
    FILE_MOVE_SOURCE_CAPABILITY,
)

PRIVACY_MODE_ADAPTER: TypeAdapter[PrivacyMode] = TypeAdapter(PrivacyMode)


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task not found: {task_id}")
        self.task_id = task_id


class ToolCallStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"

    @property
    def is_terminal(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.CANCELLED,
            self.UNKNOWN,
        }


class ToolCallNotFoundError(LookupError):
    def __init__(self, task_id: str, call_id: str) -> None:
        super().__init__(f"Tool call not found for task {task_id}: {call_id}")
        self.task_id = task_id
        self.call_id = call_id


class ToolCallAlreadyExistsError(ValueError):
    def __init__(self, call_id: str) -> None:
        super().__init__(f"Tool call already exists: {call_id}")
        self.call_id = call_id


class ToolIdempotencyKeyAlreadyUsedError(ValueError):
    code = "TOOL_IDEMPOTENCY_KEY_ALREADY_USED"

    def __init__(self, existing_call_id: str) -> None:
        super().__init__("Tool idempotency key is already owned by another call")
        self.existing_call_id = existing_call_id


class InvalidToolCallTransitionError(ValueError):
    def __init__(
        self,
        call_id: str,
        current: ToolCallStatus,
        target: ToolCallStatus,
    ) -> None:
        super().__init__(f"Tool call {call_id} cannot transition from {current} to {target}")
        self.call_id = call_id
        self.current = current
        self.target = target


class ApprovalNotFoundError(LookupError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval not found: {approval_id}")
        self.approval_id = approval_id


class ApprovalStaleError(ValueError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval preview is stale: {approval_id}")
        self.approval_id = approval_id


class ApprovalExpiredError(ValueError):
    def __init__(self, approval_id: str) -> None:
        super().__init__(f"Approval has expired: {approval_id}")
        self.approval_id = approval_id


class ApprovalAlreadyResolvedError(ValueError):
    def __init__(
        self,
        approval_id: str,
        current: ApprovalStatus,
        requested: ApprovalStatus,
    ) -> None:
        super().__init__(f"Approval {approval_id} is {current.value}, not {requested.value}")
        self.approval_id = approval_id
        self.current = current
        self.requested = requested


class ApprovalTaskStateConflictError(ValueError):
    def __init__(self, approval_id: str, task_status: TaskStatus) -> None:
        super().__init__(
            f"Approval {approval_id} cannot be resolved while task is {task_status.value}"
        )
        self.approval_id = approval_id
        self.task_status = task_status


class ToolAuthorizationError(ValueError):
    code = "TOOL_AUTHORIZATION_INVALID"

    def __init__(self, call_id: str, detail: str) -> None:
        super().__init__(detail)
        self.call_id = call_id


class ReconciliationNotFoundError(LookupError):
    code = "RECONCILIATION_NOT_FOUND"

    def __init__(self, reconciliation_id: str) -> None:
        super().__init__(f"Reconciliation not found: {reconciliation_id}")
        self.reconciliation_id = reconciliation_id


class ReconciliationAlreadyResolvedError(ValueError):
    code = "RECONCILIATION_ALREADY_RESOLVED"

    def __init__(
        self,
        reconciliation_id: str,
        outcome: ReconciliationOutcome,
    ) -> None:
        super().__init__(f"Reconciliation {reconciliation_id} is already resolved")
        self.reconciliation_id = reconciliation_id
        self.outcome = outcome


class ReconciliationAttemptNotAllowedError(ValueError):
    code = "RECONCILIATION_ATTEMPT_NOT_ALLOWED"

    def __init__(
        self,
        reconciliation_id: str,
        *,
        status: ReconciliationStatus,
        outcome: ReconciliationOutcome | None,
    ) -> None:
        super().__init__(
            f"Reconciliation {reconciliation_id} does not prove that retry is safe"
        )
        self.reconciliation_id = reconciliation_id
        self.status = status
        self.outcome = outcome


class ReconciliationAttemptAlreadyCreatedError(ValueError):
    code = "RECONCILIATION_ATTEMPT_ALREADY_CREATED"

    def __init__(self, reconciliation_id: str, task_id: str) -> None:
        super().__init__(f"Reconciliation {reconciliation_id} already has a new attempt")
        self.reconciliation_id = reconciliation_id
        self.task_id = task_id


class ReconciliationCompensationNotAllowedError(ValueError):
    code = "RECONCILIATION_COMPENSATION_NOT_ALLOWED"

    def __init__(self, reconciliation_id: str, reason_code: str) -> None:
        super().__init__(
            f"Reconciliation {reconciliation_id} cannot create a compensation task"
        )
        self.reconciliation_id = reconciliation_id
        self.reason_code = reason_code


class ReconciliationCompensationAlreadyCreatedError(ValueError):
    code = "RECONCILIATION_COMPENSATION_ALREADY_CREATED"

    def __init__(self, reconciliation_id: str, task_id: str | None) -> None:
        super().__init__(
            f"Reconciliation {reconciliation_id} already has a compensation task"
        )
        self.reconciliation_id = reconciliation_id
        self.task_id = task_id


class ReconciliationIdempotencyConflictError(ValueError):
    code = "IDEMPOTENCY_KEY_REUSED"


@dataclass(frozen=True, slots=True)
class ToolCallRecoveryResult:
    requested_failed: int = 0
    running_unknown: int = 0
    tasks_failed: int = 0
    events_created: int = 0


@dataclass(frozen=True, slots=True)
class ApprovalRecoveryResult:
    approvals_cancelled: int = 0
    tasks_cancelled: int = 0
    events_created: int = 0


@dataclass(frozen=True, slots=True)
class TaskCheckpointLoadResult:
    checkpoints: tuple[DurableTaskCheckpoint, ...] = ()
    invalid_task_ids: tuple[str, ...] = ()


ALLOWED_TASK_TRANSITIONS: dict[TaskStatus, frozenset[TaskStatus]] = {
    TaskStatus.CREATED: frozenset(
        {TaskStatus.CLASSIFYING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.CLASSIFYING: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.RUNNING: frozenset(
        {
            TaskStatus.PAUSED,
            TaskStatus.WAITING_APPROVAL,
            TaskStatus.CANCELLED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.PAUSED: frozenset({TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}),
    TaskStatus.SUCCEEDED: frozenset(),
    TaskStatus.FAILED: frozenset(),
    TaskStatus.CANCELLED: frozenset(),
}


class InvalidTaskTransitionError(ValueError):
    def __init__(
        self,
        task_id: str,
        current: TaskStatus,
        target: TaskStatus,
    ) -> None:
        super().__init__(f"Task {task_id} cannot transition from {current} to {target}")
        self.task_id = task_id
        self.current = current
        self.target = target
        self.allowed = ALLOWED_TASK_TRANSITIONS[current]


class TaskService:
    """Owns task state transitions and the append-only task event stream."""

    def __init__(
        self,
        database: Database,
        api_prefix: str,
        outbox_notify: Callable[[], None] | None = None,
        checkpoint_codec: TaskCheckpointCodec | None = None,
    ) -> None:
        self._database = database
        self._api_prefix = api_prefix
        self._outbox_notify = outbox_notify
        self._checkpoint_codec = checkpoint_codec
        self._task_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._reconciliation_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )
        self._tool_idempotency_locks: defaultdict[str, asyncio.Lock] = defaultdict(
            asyncio.Lock
        )

    async def create_task(self, command: TaskCreate) -> TaskRead:
        task_id = f"tsk_{uuid4().hex}"
        trace_id = f"trc_{uuid4().hex}"
        timestamp = utc_now()
        record = TaskRecord(
            task_id=task_id,
            conversation_id=command.conversation_id,
            goal=command.goal.strip(),
            status=TaskStatus.CREATED.value,
            mode="fake_model",
            privacy_mode=command.privacy_mode,
            constraints=list(command.constraints),
            last_event_seq=1,
            created_at=timestamp,
            updated_at=timestamp,
        )
        event_record = TaskEventRecord(
            event_id=f"evt_{uuid4().hex}",
            task_id=task_id,
            seq=1,
            type="task.created",
            timestamp=timestamp,
            trace_id=trace_id,
            payload={
                "goal": record.goal,
                "mode": record.mode,
                "privacy_mode": record.privacy_mode,
            },
        )
        event = self._to_event(event_record)
        outbox_record = self._to_outbox(event)

        async with self._database.session() as session:
            async with session.begin():
                session.add(record)
                session.add(event_record)
                await session.flush()
                session.add(outbox_record)
                await self._write_task_checkpoint(
                    session,
                    record,
                    TaskCheckpointPayload(
                        task_id=task_id,
                        next_stage=0,
                        tool_call_id=initial_tool_call_id(task_id),
                        tool_request=command.tool_request,
                    ),
                )

        self._notify_outbox()
        return self._to_task(record)

    async def save_task_checkpoint(
        self,
        checkpoint: TaskCheckpointPayload,
    ) -> DurableTaskCheckpoint | None:
        """Advance a protected checkpoint and bind it to the current event sequence."""
        if self._checkpoint_codec is None:
            return None
        async with self._task_locks[checkpoint.task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, checkpoint.task_id)
                    if task is None:
                        raise TaskNotFoundError(checkpoint.task_id)
                    if TaskStatus(task.status).is_terminal:
                        await self._delete_task_checkpoint(session, checkpoint.task_id)
                        return None
                    record = await session.get(
                        TaskRuntimeCheckpointRecord,
                        checkpoint.task_id,
                    )
                    if record is not None and checkpoint.next_stage < record.next_stage:
                        raise RuntimeError("Task checkpoint cannot move to an earlier stage")
                    written = await self._write_task_checkpoint(
                        session,
                        task,
                        checkpoint,
                    )
                    if written is None:
                        return None
                    return self._to_durable_checkpoint(written, checkpoint)

    async def load_task_checkpoints(self) -> TaskCheckpointLoadResult:
        """Decrypt every active checkpoint; corrupt records are returned by task ID."""
        if self._checkpoint_codec is None:
            return TaskCheckpointLoadResult()
        async with self._database.session() as session:
            records = tuple(
                (
                    await session.scalars(
                        select(TaskRuntimeCheckpointRecord).order_by(
                            TaskRuntimeCheckpointRecord.created_at,
                            TaskRuntimeCheckpointRecord.task_id,
                        )
                    )
                ).all()
            )
        checkpoints: list[DurableTaskCheckpoint] = []
        invalid: list[str] = []
        for record in records:
            try:
                if hashlib.sha256(record.protected_payload).hexdigest() != record.payload_digest:
                    raise TaskCheckpointInvalidError(
                        "Task checkpoint ciphertext digest does not match"
                    )
                payload = self._checkpoint_codec.decode(
                    task_id=record.task_id,
                    scheme=record.protection_scheme,
                    payload=record.protected_payload,
                )
                if (
                    record.schema_version != payload.schema_version
                    or record.next_stage != payload.next_stage
                ):
                    raise TaskCheckpointInvalidError(
                        "Task checkpoint projection does not match its payload"
                    )
                checkpoints.append(self._to_durable_checkpoint(record, payload))
            except Exception:
                invalid.append(record.task_id)
        return TaskCheckpointLoadResult(
            checkpoints=tuple(checkpoints),
            invalid_task_ids=tuple(invalid),
        )

    async def fail_task_checkpoint(self, task_id: str, *, code: str) -> None:
        """Fail closed without exposing protected checkpoint failure details."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        await self._delete_task_checkpoint(session, task_id)
                        return
                    if not TaskStatus(task.status).is_terminal:
                        await self._append_event_record(
                            session,
                            task,
                            "task.failed",
                            {
                                "error_type": "TaskCheckpointRecoveryError",
                                "code": code,
                                "message": (
                                    "The durable task checkpoint could not be proven safe "
                                    "and no Tool call was replayed."
                                ),
                            },
                            new_status=TaskStatus.FAILED,
                        )
                    await self._delete_task_checkpoint(session, task_id)
        self._notify_outbox()

    async def get_task(self, task_id: str) -> TaskRead:
        async with self._database.session() as session:
            record = await session.get(TaskRecord, task_id)
            if record is None:
                raise TaskNotFoundError(task_id)
            return self._to_task(record)

    async def get_tool_call_status(
        self,
        task_id: str,
        call_id: str,
    ) -> ToolCallStatus:
        async with self._database.session() as session:
            call = await self._get_tool_call(session, task_id, call_id)
            return ToolCallStatus(call.status)

    async def list_tasks(
        self,
        *,
        status: TaskStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> TaskHistoryRead:
        """Return a bounded newest-first page without exposing task event payloads."""
        statement = select(TaskRecord)
        count_statement = select(func.count()).select_from(TaskRecord)
        if status is not None:
            statement = statement.where(TaskRecord.status == status.value)
            count_statement = count_statement.where(TaskRecord.status == status.value)
        statement = (
            statement.order_by(TaskRecord.created_at.desc(), TaskRecord.task_id.desc())
            .limit(limit)
            .offset(offset)
        )
        async with self._database.session() as session:
            records = tuple((await session.scalars(statement)).all())
            total = int((await session.scalar(count_statement)) or 0)
        return TaskHistoryRead(
            items=tuple(self._to_task(record) for record in records),
            total=total,
            limit=limit,
            offset=offset,
        )

    async def list_events(self, task_id: str, after_seq: int = 0) -> list[TaskEventRead]:
        await self.get_task(task_id)
        async with self._database.session() as session:
            statement = (
                select(TaskEventRecord)
                .where(
                    TaskEventRecord.task_id == task_id,
                    TaskEventRecord.seq > after_seq,
                )
                .order_by(TaskEventRecord.seq)
            )
            records = (await session.scalars(statement)).all()
            return [self._to_event(record) for record in records]

    async def get_approval(self, approval_id: str) -> ApprovalRead:
        async with self._database.session() as session:
            record = await session.get(ApprovalRecord, approval_id)
            if record is None:
                raise ApprovalNotFoundError(approval_id)
            return self._to_approval(record)

    async def list_approvals(
        self,
        *,
        status: ApprovalStatus | None = None,
        task_id: str | None = None,
    ) -> list[ApprovalRead]:
        statement = select(ApprovalRecord)
        if status is not None:
            statement = statement.where(ApprovalRecord.status == status.value)
        if task_id is not None:
            statement = statement.where(ApprovalRecord.task_id == task_id)
        statement = statement.order_by(
            ApprovalRecord.requested_at.desc(),
            ApprovalRecord.approval_id,
        )
        async with self._database.session() as session:
            records = list((await session.scalars(statement)).all())
        return [self._to_approval(record) for record in records]

    async def get_reconciliation(
        self,
        reconciliation_id: str,
    ) -> ReconciliationRead:
        async with self._database.session() as session:
            record = await session.get(ToolReconciliationRecord, reconciliation_id)
            if record is None:
                raise ReconciliationNotFoundError(reconciliation_id)
            return await self._to_reconciliation(session, record)

    async def list_reconciliations(
        self,
        *,
        status: ReconciliationStatus | None = None,
        task_id: str | None = None,
    ) -> list[ReconciliationRead]:
        statement = select(ToolReconciliationRecord)
        if status is not None:
            statement = statement.where(ToolReconciliationRecord.status == status.value)
        if task_id is not None:
            statement = statement.where(ToolReconciliationRecord.task_id == task_id)
        statement = statement.order_by(
            ToolReconciliationRecord.unknown_at.desc(),
            ToolReconciliationRecord.reconciliation_id,
        )
        async with self._database.session() as session:
            records = list((await session.scalars(statement)).all())
            return [
                await self._to_reconciliation(session, record) for record in records
            ]

    async def record_reconciliation_evidence(
        self,
        reconciliation_id: str,
        *,
        kind: ReconciliationEvidenceKind,
        queried_runner_id: str | None,
        commit_receipt: ToolCommitReceipt | None = None,
        error_code: str | None = None,
    ) -> ReconciliationEvidenceRefreshRead:
        """Content-address and append one signed Runner query observation."""
        if len(queried_runner_id or "") > 128:
            raise ValueError("Runner identity exceeds the durable evidence limit")
        if kind is ReconciliationEvidenceKind.COMMIT_RECEIPT:
            if commit_receipt is None or error_code is not None:
                raise ValueError("Commit receipt evidence requires only a receipt")
        elif kind is ReconciliationEvidenceKind.NO_RECEIPT:
            if commit_receipt is not None or error_code is not None:
                raise ValueError("No-receipt evidence cannot contain a payload")
        elif commit_receipt is not None or not error_code:
            raise ValueError("Query-failed evidence requires only an error code")
        if error_code is not None and len(error_code) > 100:
            raise ValueError("Reconciliation evidence error code is too long")

        digest = sha256_digest(
            {
                "kind": kind.value,
                "queried_runner_id": queried_runner_id,
                "commit_receipt": (
                    commit_receipt.model_dump(mode="json")
                    if commit_receipt is not None
                    else None
                ),
                "error_code": error_code,
            }
        )
        replayed = False
        async with self._reconciliation_locks[reconciliation_id]:
            async with self._database.session() as session:
                async with session.begin():
                    reconciliation = await session.get(
                        ToolReconciliationRecord,
                        reconciliation_id,
                    )
                    if reconciliation is None:
                        raise ReconciliationNotFoundError(reconciliation_id)
                    call = await self._get_tool_call(
                        session,
                        reconciliation.task_id,
                        reconciliation.call_id,
                    )
                    if ToolCallStatus(call.status) is not ToolCallStatus.UNKNOWN:
                        raise RuntimeError(
                            "Reconciliation evidence requires an unknown Tool call"
                        )

                    evidence_record = await session.scalar(
                        select(ToolReconciliationEvidenceRecord).where(
                            ToolReconciliationEvidenceRecord.reconciliation_id
                            == reconciliation_id,
                            ToolReconciliationEvidenceRecord.evidence_digest == digest,
                        )
                    )
                    if evidence_record is None:
                        timestamp = utc_now()
                        if commit_receipt is not None:
                            await self._persist_commit_receipt(
                                session,
                                call,
                                {
                                    "commit_receipt": commit_receipt.model_dump(
                                        mode="json"
                                    )
                                },
                                projected_at=timestamp,
                            )
                        evidence_record = ToolReconciliationEvidenceRecord(
                            evidence_id=f"rce_{uuid4().hex}",
                            reconciliation_id=reconciliation_id,
                            evidence_digest=digest,
                            kind=kind.value,
                            queried_runner_id=queried_runner_id,
                            receipt_id=(
                                commit_receipt.receipt_id
                                if commit_receipt is not None
                                else None
                            ),
                            error_code=error_code,
                            observed_at=timestamp,
                        )
                        session.add(evidence_record)
                        await session.flush()
                    else:
                        replayed = True

                    evidence = await self._to_reconciliation_evidence(
                        session,
                        evidence_record,
                    )
                    snapshot = await self._to_reconciliation(
                        session,
                        reconciliation,
                    )

        return ReconciliationEvidenceRefreshRead(
            reconciliation=snapshot,
            evidence=evidence,
            replayed=replayed,
        )

    async def resolve_reconciliation(
        self,
        reconciliation_id: str,
        *,
        outcome: ReconciliationOutcome,
        evidence_summary: str,
        idempotency_key: str,
        resolved_by: str = "local_user",
    ) -> ReconciliationResolutionRead:
        """Persist one immutable human verdict without rewriting the Tool ledger."""
        evidence = evidence_summary.strip()
        if not evidence:
            raise ValueError("Reconciliation evidence must not be empty")
        key_digest = self._digest_text(idempotency_key)
        operation = "tool_reconciliation.resolve"
        fingerprint = sha256_digest(
            {
                "operation": operation,
                "reconciliation_id": reconciliation_id,
                "outcome": outcome.value,
                "evidence_summary": evidence,
            }
        )
        async with self._reconciliation_locks[reconciliation_id]:
            async with self._database.session() as session:
                async with session.begin():
                    replay = await session.get(
                        ToolReconciliationIdempotencyRecord,
                        key_digest,
                    )
                    if replay is not None:
                        self._validate_reconciliation_idempotency(
                            replay,
                            operation=operation,
                            fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                        )
                        record = await session.get(
                            ToolReconciliationRecord,
                            reconciliation_id,
                        )
                        if record is None:
                            raise ReconciliationNotFoundError(reconciliation_id)
                        return ReconciliationResolutionRead(
                            reconciliation=await self._to_reconciliation(
                                session,
                                record,
                            ),
                            replayed=True,
                        )

                    record = await session.get(
                        ToolReconciliationRecord,
                        reconciliation_id,
                    )
                    if record is None:
                        raise ReconciliationNotFoundError(reconciliation_id)
                    current_status = ReconciliationStatus(record.status)
                    if current_status is ReconciliationStatus.RESOLVED:
                        if record.outcome is None:
                            raise RuntimeError("Resolved reconciliation has no outcome")
                        raise ReconciliationAlreadyResolvedError(
                            reconciliation_id,
                            ReconciliationOutcome(record.outcome),
                        )

                    timestamp = utc_now()
                    record.status = ReconciliationStatus.RESOLVED.value
                    record.outcome = outcome.value
                    record.evidence_summary = evidence
                    record.resolved_by = resolved_by
                    record.resolved_at = timestamp
                    record.updated_at = timestamp
                    session.add(
                        ToolReconciliationIdempotencyRecord(
                            key_digest=key_digest,
                            operation=operation,
                            request_fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                            created_at=timestamp,
                        )
                    )
                    snapshot = await self._to_reconciliation(session, record)

        return ReconciliationResolutionRead(
            reconciliation=snapshot,
            replayed=False,
        )

    async def create_reconciliation_attempt(
        self,
        reconciliation_id: str,
        *,
        idempotency_key: str,
    ) -> ReconciliationAttemptRead:
        """Create a fresh task only after evidence proves the old call had no effect."""
        key_digest = self._digest_text(idempotency_key)
        operation = "tool_reconciliation.create_attempt"
        fingerprint = sha256_digest(
            {
                "operation": operation,
                "reconciliation_id": reconciliation_id,
            }
        )
        created = False
        async with self._reconciliation_locks[reconciliation_id]:
            async with self._database.session() as session:
                async with session.begin():
                    replay = await session.get(
                        ToolReconciliationIdempotencyRecord,
                        key_digest,
                    )
                    if replay is not None:
                        self._validate_reconciliation_idempotency(
                            replay,
                            operation=operation,
                            fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                        )
                        if replay.created_task_id is None:
                            raise RuntimeError("Attempt receipt has no created task")
                        record = await session.get(
                            ToolReconciliationRecord,
                            reconciliation_id,
                        )
                        task = await session.get(TaskRecord, replay.created_task_id)
                        if record is None:
                            raise ReconciliationNotFoundError(reconciliation_id)
                        if task is None:
                            raise TaskNotFoundError(replay.created_task_id)
                        return ReconciliationAttemptRead(
                            reconciliation=await self._to_reconciliation(
                                session,
                                record,
                            ),
                            task=self._to_task(task),
                            replayed=True,
                        )

                    record = await session.get(
                        ToolReconciliationRecord,
                        reconciliation_id,
                    )
                    if record is None:
                        raise ReconciliationNotFoundError(reconciliation_id)
                    original_call = await session.get(
                        ToolCallRecord,
                        record.call_id,
                    )
                    if original_call is None:
                        raise ToolCallNotFoundError(record.task_id, record.call_id)
                    status = ReconciliationStatus(record.status)
                    outcome = (
                        ReconciliationOutcome(record.outcome)
                        if record.outcome is not None
                        else None
                    )
                    if (
                        status is not ReconciliationStatus.RESOLVED
                        or outcome is not ReconciliationOutcome.CONFIRMED_NO_EFFECT
                        or record.compensation_created_at is not None
                        or not self._supports_explicit_attempt(original_call)
                    ):
                        raise ReconciliationAttemptNotAllowedError(
                            reconciliation_id,
                            status=status,
                            outcome=outcome,
                        )
                    if record.new_attempt_task_id is not None:
                        raise ReconciliationAttemptAlreadyCreatedError(
                            reconciliation_id,
                            record.new_attempt_task_id,
                        )

                    original_task = await session.get(TaskRecord, record.task_id)
                    if original_task is None:
                        raise TaskNotFoundError(record.task_id)

                    timestamp = utc_now()
                    task_id = f"tsk_{uuid4().hex}"
                    trace_id = f"trc_{uuid4().hex}"
                    task = TaskRecord(
                        task_id=task_id,
                        conversation_id=original_task.conversation_id,
                        goal=original_task.goal,
                        status=TaskStatus.CREATED.value,
                        mode=original_task.mode,
                        privacy_mode=original_task.privacy_mode,
                        constraints=list(original_task.constraints or []),
                        last_event_seq=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    event_record = TaskEventRecord(
                        event_id=f"evt_{uuid4().hex}",
                        task_id=task_id,
                        seq=1,
                        type="task.created",
                        timestamp=timestamp,
                        trace_id=trace_id,
                        payload={
                            "goal": task.goal,
                            "mode": task.mode,
                            "privacy_mode": task.privacy_mode,
                            "retry_of": {
                                "reconciliation_id": reconciliation_id,
                                "task_id": record.task_id,
                                "call_id": record.call_id,
                                "source_attempt": original_call.attempt,
                            },
                        },
                    )
                    event = self._to_event(event_record)
                    session.add(task)
                    session.add(event_record)
                    await session.flush()
                    session.add(self._to_outbox(event))
                    await self._write_task_checkpoint(
                        session,
                        task,
                        TaskCheckpointPayload(
                            task_id=task_id,
                            next_stage=0,
                            tool_call_id=initial_tool_call_id(task_id),
                        ),
                    )

                    record.new_attempt_task_id = task_id
                    record.new_attempt_created_at = timestamp
                    record.updated_at = timestamp
                    session.add(
                        ToolReconciliationIdempotencyRecord(
                            key_digest=key_digest,
                            operation=operation,
                            request_fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                            created_task_id=task_id,
                            created_at=timestamp,
                        )
                    )
                    reconciliation = await self._to_reconciliation(session, record)
                    task_snapshot = self._to_task(task)
                    created = True

        if created:
            self._notify_outbox()
        return ReconciliationAttemptRead(
            reconciliation=reconciliation,
            task=task_snapshot,
            replayed=False,
        )

    async def get_reconciliation_compensation_request(
        self,
        reconciliation_id: str,
    ) -> FileMoveCompensationRequest:
        """Derive the only permitted reverse move from durable approval/receipt facts."""
        async with self._database.session() as session:
            record = await session.get(ToolReconciliationRecord, reconciliation_id)
            if record is None:
                raise ReconciliationNotFoundError(reconciliation_id)
            return await self._compensation_request(session, record)

    async def replay_reconciliation_compensation(
        self,
        reconciliation_id: str,
        *,
        idempotency_key: str,
    ) -> ReconciliationCompensationRead | None:
        """Return an existing response before any fresh filesystem preflight."""
        key_digest = self._digest_text(idempotency_key)
        operation = "tool_reconciliation.create_compensation"
        fingerprint = sha256_digest(
            {"operation": operation, "reconciliation_id": reconciliation_id}
        )
        async with self._database.session() as session:
            replay = await session.get(ToolReconciliationIdempotencyRecord, key_digest)
            if replay is None:
                return None
            self._validate_reconciliation_idempotency(
                replay,
                operation=operation,
                fingerprint=fingerprint,
                reconciliation_id=reconciliation_id,
            )
            if replay.created_task_id is None:
                raise RuntimeError("Compensation receipt has no created task")
            record = await session.get(ToolReconciliationRecord, reconciliation_id)
            task = await session.get(TaskRecord, replay.created_task_id)
            if record is None:
                raise ReconciliationNotFoundError(reconciliation_id)
            if task is None:
                raise TaskNotFoundError(replay.created_task_id)
            if record.compensation_task_id != replay.created_task_id:
                raise RuntimeError("Compensation lineage does not match its idempotency receipt")
            return ReconciliationCompensationRead(
                reconciliation=await self._to_reconciliation(session, record),
                task=self._to_task(task),
                replayed=True,
            )

    async def create_reconciliation_compensation(
        self,
        reconciliation_id: str,
        *,
        request: FileMoveCompensationRequest,
        idempotency_key: str,
    ) -> ReconciliationCompensationRead:
        """Create one receipt-bound reverse task without accepting client paths."""
        key_digest = self._digest_text(idempotency_key)
        operation = "tool_reconciliation.create_compensation"
        fingerprint = sha256_digest(
            {"operation": operation, "reconciliation_id": reconciliation_id}
        )
        created = False
        async with self._reconciliation_locks[reconciliation_id]:
            async with self._database.session() as session:
                async with session.begin():
                    replay = await session.get(
                        ToolReconciliationIdempotencyRecord,
                        key_digest,
                    )
                    if replay is not None:
                        self._validate_reconciliation_idempotency(
                            replay,
                            operation=operation,
                            fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                        )
                        if replay.created_task_id is None:
                            raise RuntimeError("Compensation receipt has no created task")
                        record = await session.get(
                            ToolReconciliationRecord,
                            reconciliation_id,
                        )
                        task = await session.get(TaskRecord, replay.created_task_id)
                        if record is None:
                            raise ReconciliationNotFoundError(reconciliation_id)
                        if task is None:
                            raise TaskNotFoundError(replay.created_task_id)
                        return ReconciliationCompensationRead(
                            reconciliation=await self._to_reconciliation(
                                session,
                                record,
                            ),
                            task=self._to_task(task),
                            replayed=True,
                        )

                    record = await session.get(
                        ToolReconciliationRecord,
                        reconciliation_id,
                    )
                    if record is None:
                        raise ReconciliationNotFoundError(reconciliation_id)
                    if record.compensation_created_at is not None:
                        raise ReconciliationCompensationAlreadyCreatedError(
                            reconciliation_id,
                            record.compensation_task_id,
                        )
                    expected = await self._compensation_request(session, record)
                    if request != expected:
                        raise ReconciliationCompensationNotAllowedError(
                            reconciliation_id,
                            "COMPENSATION_REQUEST_BINDING_MISMATCH",
                        )

                    original_task = await session.get(TaskRecord, record.task_id)
                    if original_task is None:
                        raise TaskNotFoundError(record.task_id)
                    timestamp = utc_now()
                    task_id = f"tsk_{uuid4().hex}"
                    trace_id = f"trc_{uuid4().hex}"
                    constraints = list(
                        dict.fromkeys(
                            [
                                *(original_task.constraints or []),
                                "compensation",
                                "single_file",
                                "no_overwrite",
                            ]
                        )
                    )
                    task = TaskRecord(
                        task_id=task_id,
                        conversation_id=original_task.conversation_id,
                        goal="撤销已确认提交的单文件移动",
                        status=TaskStatus.CREATED.value,
                        mode=original_task.mode,
                        privacy_mode=original_task.privacy_mode,
                        constraints=constraints,
                        last_event_seq=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    event_record = TaskEventRecord(
                        event_id=f"evt_{uuid4().hex}",
                        task_id=task_id,
                        seq=1,
                        type="task.created",
                        timestamp=timestamp,
                        trace_id=trace_id,
                        payload={
                            "goal": task.goal,
                            "mode": task.mode,
                            "privacy_mode": task.privacy_mode,
                            "compensation_of": {
                                "reconciliation_id": reconciliation_id,
                                "task_id": record.task_id,
                                "call_id": record.call_id,
                                "receipt_id": expected.receipt_id,
                            },
                        },
                    )
                    event = self._to_event(event_record)
                    session.add(task)
                    session.add(event_record)
                    await session.flush()
                    session.add(self._to_outbox(event))
                    await self._write_task_checkpoint(
                        session,
                        task,
                        TaskCheckpointPayload(
                            task_id=task_id,
                            next_stage=0,
                            tool_call_id=initial_tool_call_id(task_id),
                            tool_request=request,
                        ),
                    )

                    record.compensation_task_id = task_id
                    record.compensation_receipt_id = expected.receipt_id
                    record.compensation_created_at = timestamp
                    record.updated_at = timestamp
                    session.add(
                        ToolReconciliationIdempotencyRecord(
                            key_digest=key_digest,
                            operation=operation,
                            request_fingerprint=fingerprint,
                            reconciliation_id=reconciliation_id,
                            created_task_id=task_id,
                            created_at=timestamp,
                        )
                    )
                    reconciliation = await self._to_reconciliation(session, record)
                    task_snapshot = self._to_task(task)
                    created = True

        if created:
            self._notify_outbox()
        return ReconciliationCompensationRead(
            reconciliation=reconciliation,
            task=task_snapshot,
            replayed=False,
        )

    async def _compensation_request(
        self,
        session: AsyncSession,
        record: ToolReconciliationRecord,
    ) -> FileMoveCompensationRequest:
        def not_allowed(reason_code: str) -> ReconciliationCompensationNotAllowedError:
            return ReconciliationCompensationNotAllowedError(
                record.reconciliation_id,
                reason_code,
            )

        call = await session.get(ToolCallRecord, record.call_id)
        if call is None:
            raise ToolCallNotFoundError(record.task_id, record.call_id)
        if (
            ToolCallStatus(call.status) is not ToolCallStatus.UNKNOWN
            or call.tool_name != FILE_MOVE_CONTRACT.name
            or call.tool_version != FILE_MOVE_CONTRACT.version
            or call.contract_digest != FILE_MOVE_CONTRACT.digest
        ):
            raise not_allowed("COMPENSATION_REQUIRES_UNKNOWN_FILE_MOVE")
        if record.new_attempt_task_id is not None:
            raise not_allowed("COMPENSATION_CONFLICTS_WITH_NEW_ATTEMPT")

        evidence = await session.scalar(
            select(ToolReconciliationEvidenceRecord)
            .where(
                ToolReconciliationEvidenceRecord.reconciliation_id
                == record.reconciliation_id,
                ToolReconciliationEvidenceRecord.kind
                == ReconciliationEvidenceKind.COMMIT_RECEIPT.value,
            )
            .order_by(
                ToolReconciliationEvidenceRecord.observed_at.desc(),
                ToolReconciliationEvidenceRecord.evidence_id.desc(),
            )
        )
        if evidence is None or evidence.receipt_id is None:
            raise not_allowed("COMPENSATION_REQUIRES_COMMIT_RECEIPT")
        receipt_record = await session.get(ToolCommitReceiptRecord, evidence.receipt_id)
        if receipt_record is None:
            raise not_allowed("COMPENSATION_RECEIPT_UNAVAILABLE")
        receipt = self._to_commit_receipt(receipt_record)
        try:
            self._validate_commit_receipt_binding(call, receipt)
        except ToolAuthorizationError as error:
            raise not_allowed("COMPENSATION_RECEIPT_BINDING_INVALID") from error

        approval = await session.get(ApprovalRecord, receipt.approval_id)
        if approval is None:
            raise not_allowed("COMPENSATION_APPROVAL_UNAVAILABLE")
        if (
            approval.call_id != call.call_id
            or approval.task_id != call.task_id
            or approval.tool_name != call.tool_name
            or approval.tool_version != call.tool_version
            or approval.contract_digest != call.contract_digest
            or approval.arguments_digest != call.arguments_digest
            or approval.preview_hash != receipt.preview_hash
            or approval.status != ApprovalStatus.APPROVED.value
            or approval.decision != ApprovalDecision.APPROVED.value
            or approval.consumed_at is None
            or not approval.reversible
            or set(approval.capabilities or [])
            != set(FILE_MOVE_CONTRACT.security.capabilities)
            or dict(approval.expected_resource_versions or {})
            != receipt.resource_versions_before
        ):
            raise not_allowed("COMPENSATION_APPROVAL_BINDING_INVALID")

        resources = tuple(
            ApprovalResourceRead.model_validate(item)
            for item in (approval.resource_scope or [])
        )
        source_resources = tuple(
            item
            for item in resources
            if item.operations == (FILE_MOVE_SOURCE_CAPABILITY,)
        )
        destination_resources = tuple(
            item
            for item in resources
            if item.operations == (FILE_MOVE_DESTINATION_CAPABILITY,)
        )
        if (
            len(resources) != 2
            or len(source_resources) != 1
            or len(destination_resources) != 1
        ):
            raise not_allowed("COMPENSATION_RESOURCE_SCOPE_INVALID")
        original_source = source_resources[0]
        original_destination = destination_resources[0]
        if (
            original_source.kind != "filesystem_path"
            or original_destination.kind != "filesystem_path"
            or original_source.version
            != receipt.resource_versions_before["source"]
            or original_destination.version is not None
            or receipt.resource_versions_before["destination"] != "absent"
            or receipt.resource_versions_after["source"] != "absent"
        ):
            raise not_allowed("COMPENSATION_RESOURCE_VERSION_INVALID")
        reverse_source_version = receipt.resource_versions_after["destination"]
        if reverse_source_version == "absent":
            raise not_allowed("COMPENSATION_COMMITTED_VERSION_INVALID")

        return FileMoveCompensationRequest(
            source=original_destination.label,
            destination=original_source.label,
            reconciliation_id=record.reconciliation_id,
            original_task_id=record.task_id,
            original_call_id=record.call_id,
            receipt_id=receipt.receipt_id,
            expected_source_version=reverse_source_version,
        )

    async def apply_policy_decision(
        self,
        task_id: str,
        call_id: str,
        *,
        request: ToolAuthorizationRequest,
        decision: PolicyDecision,
        title: str,
        purpose: str,
        consequences: tuple[str, ...],
        data_egress: DataEgress,
        expected_resource_versions: dict[str, str],
    ) -> ApprovalRead | None:
        """Commit policy truth and any approval/task effects in one transaction."""
        if request.task_id != task_id or request.call_id != call_id:
            raise ToolAuthorizationError(call_id, "Policy request identity does not match call")
        if decision.request_digest != request.request_digest:
            raise ToolAuthorizationError(call_id, "Policy decision request digest is stale")
        if decision.resource_scope_digest != request.resource_scope_digest:
            raise ToolAuthorizationError(call_id, "Policy resource scope digest is stale")
        if sha256_digest(expected_resource_versions) != request.expected_resource_versions_digest:
            raise ToolAuthorizationError(call_id, "Policy resource versions digest is stale")
        if data_egress.enabled != request.data_egress:
            raise ToolAuthorizationError(
                call_id,
                "Approval data egress preview does not match the policy request",
            )
        if data_egress.enabled:
            raise ToolAuthorizationError(
                call_id,
                "Data egress destinations are not bound by this authorization version",
            )

        approval_snapshot: ApprovalRead | None = None
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    current_task_status = TaskStatus(task.status)
                    if current_task_status is not TaskStatus.RUNNING:
                        raise InvalidTaskTransitionError(
                            task_id,
                            current_task_status,
                            TaskStatus.WAITING_APPROVAL,
                        )
                    call = await self._get_tool_call(session, task_id, call_id)
                    if ToolCallStatus(call.status) is not ToolCallStatus.REQUESTED:
                        raise InvalidToolCallTransitionError(
                            call_id,
                            ToolCallStatus(call.status),
                            ToolCallStatus.RUNNING,
                        )
                    if (
                        call.step_id != request.step_id
                        or call.tool_name != request.tool_name
                        or call.tool_version != request.tool_version
                        or call.contract_digest != request.contract_digest
                        or call.arguments_digest != request.arguments_digest
                    ):
                        raise ToolAuthorizationError(
                            call_id,
                            "Policy facts do not match the durable Tool call binding",
                        )

                    policy_event = await self._append_event_record(
                        session,
                        task,
                        "policy.evaluated",
                        {
                            "call_id": call_id,
                            "decision_id": decision.decision_id,
                            "effect": decision.effect.value,
                            "effective_risk": decision.effective_risk.value,
                            "policy_revision": decision.policy_revision,
                            "rule_id": decision.rule_id,
                            "reason_code": decision.reason_code,
                            "request_digest": decision.request_digest,
                            "resource_scope_digest": decision.resource_scope_digest,
                        },
                        new_status=None,
                    )
                    call.policy_decision_id = decision.decision_id
                    call.policy_revision = decision.policy_revision
                    call.policy_effect = decision.effect.value
                    call.resource_scope_digest = decision.resource_scope_digest
                    call.policy_event_id = policy_event.event_id

                    if decision.effect is PolicyEffect.ALLOW:
                        pass
                    elif decision.effect is PolicyEffect.DENY:
                        timestamp = utc_now()
                        call.status = ToolCallStatus.FAILED.value
                        call.resolution_source = "policy"
                        call.error_code = decision.reason_code
                        call.finished_at = timestamp
                        call.updated_at = timestamp
                        tool_event = await self._append_event_record(
                            session,
                            task,
                            "tool.failed",
                            self._terminal_tool_event_payload(
                                call,
                                status=ToolCallStatus.FAILED,
                                result=None,
                                retryable=False,
                            ),
                            new_status=None,
                        )
                        call.terminal_event_id = tool_event.event_id
                        await self._append_event_record(
                            session,
                            task,
                            "task.failed",
                            {
                                "error_type": "PolicyDeniedError",
                                "code": "POLICY_DENIED",
                                "message": "Policy denied the Tool call before dispatch.",
                                "call_id": call_id,
                                "policy_reason_code": decision.reason_code,
                            },
                            new_status=TaskStatus.FAILED,
                        )
                    elif decision.effect is PolicyEffect.REQUIRE_APPROVAL:
                        if decision.approval_ttl_seconds is None:
                            raise ToolAuthorizationError(
                                call_id,
                                "Approval decision has no validity window",
                            )
                        requested_at = utc_now()
                        expires_at = requested_at + timedelta(seconds=decision.approval_ttl_seconds)
                        approval_id = f"apr_{uuid4().hex}"
                        resources = tuple(
                            ApprovalResourceRead(
                                kind=resource.kind,
                                label=resource.display_name or resource.identifier,
                                operations=resource.operations,
                                version=resource.version_digest,
                            )
                            for resource in request.resources
                        )
                        preview_material: dict[str, Any] = {
                            "approval_id": approval_id,
                            "task_id": task_id,
                            "step_id": request.step_id,
                            "call_id": call_id,
                            "tool_name": request.tool_name,
                            "tool_version": request.tool_version,
                            "contract_digest": request.contract_digest,
                            "arguments_digest": request.arguments_digest,
                            "request_digest": request.request_digest,
                            "resource_scope_digest": request.resource_scope_digest,
                            "expected_resource_versions_digest": (
                                request.expected_resource_versions_digest
                            ),
                            "effective_risk": decision.effective_risk.value,
                            "capabilities": list(request.capabilities),
                            "policy_revision": decision.policy_revision,
                            "decision_id": decision.decision_id,
                            "title": title,
                            "purpose": purpose,
                            "resources": [
                                resource.model_dump(mode="json") for resource in resources
                            ],
                            "consequences": list(consequences),
                            "reversible": request.reversible,
                            "data_egress": data_egress.model_dump(mode="json"),
                            "requested_at": requested_at.isoformat(),
                            "expires_at": expires_at.isoformat(),
                        }
                        preview_hash = sha256_digest(preview_material)
                        approval = ApprovalRecord(
                            approval_id=approval_id,
                            task_id=task_id,
                            call_id=call_id,
                            tool_name=request.tool_name,
                            tool_version=request.tool_version,
                            risk_level=decision.effective_risk.value,
                            policy_decision=decision.effect.value,
                            decision_id=decision.decision_id,
                            policy_rule_id=decision.rule_id,
                            policy_revision=decision.policy_revision,
                            reason_code=decision.reason_code,
                            contract_digest=request.contract_digest,
                            arguments_digest=request.arguments_digest,
                            binding_digest=request.request_digest,
                            title=title,
                            purpose=purpose,
                            capabilities=list(request.capabilities),
                            resource_scope=[
                                resource.model_dump(mode="json") for resource in resources
                            ],
                            consequences=list(consequences),
                            reversible=request.reversible,
                            data_egress=data_egress.model_dump(mode="json"),
                            expected_resource_versions=dict(expected_resource_versions),
                            preview_hash=preview_hash,
                            status=ApprovalStatus.PENDING.value,
                            decision=None,
                            requested_at=requested_at,
                            expires_at=expires_at,
                            updated_at=requested_at,
                        )
                        session.add(approval)
                        await session.flush()
                        await self._append_event_record(
                            session,
                            task,
                            "approval.required",
                            {
                                "approval_id": approval_id,
                                "call_id": call_id,
                                "preview_hash": preview_hash,
                                "title": title,
                                "risk_level": decision.effective_risk.value,
                                "expires_at": expires_at.isoformat(),
                            },
                            new_status=None,
                        )
                        await self._append_event_record(
                            session,
                            task,
                            "task.status_changed",
                            {
                                "from": current_task_status.value,
                                "to": TaskStatus.WAITING_APPROVAL.value,
                                "command": "policy",
                                "requested_by": "system",
                            },
                            new_status=TaskStatus.WAITING_APPROVAL,
                        )
                        approval_snapshot = self._to_approval(approval)
                    else:
                        raise AssertionError(f"Unsupported policy effect: {decision.effect}")

        self._notify_outbox()
        return approval_snapshot

    async def resolve_approval(
        self,
        approval_id: str,
        *,
        decision: ApprovalStatus,
        preview_hash: str,
        scope: str = "once",
        reason: str | None = None,
        resolved_by: str = "local_user",
    ) -> ApprovalResolutionRead:
        if decision not in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            raise ValueError("Approval decision must be approved or rejected")
        if scope != "once":
            raise ValueError("Only one-shot approvals are supported")

        async with self._database.session() as lookup_session:
            lookup = await lookup_session.get(ApprovalRecord, approval_id)
            if lookup is None:
                raise ApprovalNotFoundError(approval_id)
            task_id = lookup.task_id

        event_created = False
        expired = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    approval = await session.get(ApprovalRecord, approval_id)
                    if approval is None:
                        raise ApprovalNotFoundError(approval_id)
                    if approval.preview_hash != preview_hash:
                        raise ApprovalStaleError(approval_id)

                    current = ApprovalStatus(approval.status)
                    if current is ApprovalStatus.EXPIRED:
                        raise ApprovalExpiredError(approval_id)
                    if current is decision:
                        task = await session.get(TaskRecord, task_id)
                        if task is None:
                            raise TaskNotFoundError(task_id)
                        return ApprovalResolutionRead(
                            approval=self._to_approval(approval),
                            task=self._to_task(task),
                            replayed=True,
                        )
                    if current is not ApprovalStatus.PENDING:
                        raise ApprovalAlreadyResolvedError(
                            approval_id,
                            current,
                            decision,
                        )

                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    task_status = TaskStatus(task.status)
                    if task_status is not TaskStatus.WAITING_APPROVAL:
                        raise ApprovalTaskStateConflictError(
                            approval_id,
                            task_status,
                        )

                    timestamp = utc_now()
                    if self._as_utc(approval.expires_at) <= timestamp:
                        resolved = await session.scalar(
                            update(ApprovalRecord)
                            .where(
                                ApprovalRecord.approval_id == approval_id,
                                ApprovalRecord.status == ApprovalStatus.PENDING.value,
                            )
                            .values(
                                status=ApprovalStatus.EXPIRED.value,
                                resolved_at=timestamp,
                                resolved_by="system",
                                resolution_reason="Approval validity window elapsed",
                                updated_at=timestamp,
                            )
                            .returning(ApprovalRecord)
                        )
                        if resolved is None:
                            raise ApprovalAlreadyResolvedError(
                                approval_id,
                                ApprovalStatus(approval.status),
                                decision,
                            )
                        approval = resolved
                        await self._append_event_record(
                            session,
                            task,
                            "approval.expired",
                            {
                                "approval_id": approval_id,
                                "call_id": approval.call_id,
                                "status": ApprovalStatus.EXPIRED.value,
                                "reason_code": "APPROVAL_EXPIRED",
                            },
                            new_status=None,
                        )
                        await self._cancel_requested_call_for_approval(
                            session,
                            task,
                            approval,
                            timestamp=timestamp,
                            error_code="APPROVAL_EXPIRED",
                            resolution_source="policy",
                        )
                        await self._append_event_record(
                            session,
                            task,
                            "task.cancelled",
                            {
                                "from": task_status.value,
                                "to": TaskStatus.CANCELLED.value,
                                "command": "approval_expired",
                                "requested_by": "system",
                            },
                            new_status=TaskStatus.CANCELLED,
                        )
                        expired = True
                        event_created = True
                    else:
                        resolved = await session.scalar(
                            update(ApprovalRecord)
                            .where(
                                ApprovalRecord.approval_id == approval_id,
                                ApprovalRecord.status == ApprovalStatus.PENDING.value,
                                ApprovalRecord.preview_hash == preview_hash,
                            )
                            .values(
                                status=decision.value,
                                decision=decision.value,
                                scope=scope if decision is ApprovalStatus.APPROVED else None,
                                resolved_at=timestamp,
                                resolved_by=resolved_by,
                                resolution_reason=reason,
                                updated_at=timestamp,
                            )
                            .returning(ApprovalRecord)
                        )
                        if resolved is None:
                            concurrent = await session.get(ApprovalRecord, approval_id)
                            concurrent_status = (
                                ApprovalStatus(concurrent.status)
                                if concurrent is not None
                                else ApprovalStatus.CANCELLED
                            )
                            if concurrent_status is ApprovalStatus.EXPIRED:
                                raise ApprovalExpiredError(approval_id)
                            raise ApprovalAlreadyResolvedError(
                                approval_id,
                                concurrent_status,
                                decision,
                            )
                        approval = resolved
                        await self._append_event_record(
                            session,
                            task,
                            "approval.resolved",
                            {
                                "approval_id": approval_id,
                                "call_id": approval.call_id,
                                "status": decision.value,
                                "decision": decision.value,
                                "reason_code": (
                                    "APPROVAL_GRANTED"
                                    if decision is ApprovalStatus.APPROVED
                                    else "APPROVAL_REJECTED"
                                ),
                            },
                            new_status=None,
                        )
                        if decision is ApprovalStatus.APPROVED:
                            await self._append_event_record(
                                session,
                                task,
                                "task.status_changed",
                                {
                                    "from": task_status.value,
                                    "to": TaskStatus.RUNNING.value,
                                    "command": "approval_granted",
                                    "requested_by": resolved_by,
                                },
                                new_status=TaskStatus.RUNNING,
                            )
                            await self._rebind_task_checkpoint(session, task)
                        else:
                            await self._cancel_requested_call_for_approval(
                                session,
                                task,
                                approval,
                                timestamp=timestamp,
                                error_code="APPROVAL_REJECTED",
                                resolution_source="policy",
                            )
                            await self._append_event_record(
                                session,
                                task,
                                "task.cancelled",
                                {
                                    "from": task_status.value,
                                    "to": TaskStatus.CANCELLED.value,
                                    "command": "approval_rejected",
                                    "requested_by": resolved_by,
                                },
                                new_status=TaskStatus.CANCELLED,
                            )
                        event_created = True

                    snapshot = ApprovalResolutionRead(
                        approval=self._to_approval(approval),
                        task=self._to_task(task),
                        replayed=False,
                    )

        if event_created:
            self._notify_outbox()
        if expired:
            raise ApprovalExpiredError(approval_id)
        return snapshot

    async def expire_approval(self, approval_id: str) -> ApprovalResolutionRead:
        """Expire a still-pending request and cancel its unexecuted task branch."""
        approval = await self.get_approval(approval_id)
        if approval.status is ApprovalStatus.EXPIRED:
            return ApprovalResolutionRead(
                approval=approval,
                task=await self.get_task(approval.task_id),
                replayed=True,
            )
        if approval.status is not ApprovalStatus.PENDING:
            raise ApprovalAlreadyResolvedError(
                approval_id,
                approval.status,
                ApprovalStatus.EXPIRED,
            )
        if approval.expires_at > utc_now():
            return ApprovalResolutionRead(
                approval=approval,
                task=await self.get_task(approval.task_id),
                replayed=True,
            )

        try:
            await self.resolve_approval(
                approval_id,
                decision=ApprovalStatus.REJECTED,
                preview_hash=approval.preview_hash,
                reason="Approval validity window elapsed",
                resolved_by="system",
            )
        except ApprovalExpiredError:
            pass
        latest = await self.get_approval(approval_id)
        return ApprovalResolutionRead(
            approval=latest,
            task=await self.get_task(latest.task_id),
            replayed=False,
        )

    async def record_tool_requested(
        self,
        task_id: str,
        *,
        call_id: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        contract_digest: str,
        arguments: dict[str, Any],
        idempotency: ToolIdempotency,
        idempotency_key: str | None = None,
        attempt: int = 1,
        risk: str | None = None,
    ) -> TaskEventRead:
        """Persist a validated call request without retaining raw arguments or keys."""
        if attempt < 1:
            raise ValueError("Tool call attempt must be at least 1")
        if idempotency is ToolIdempotency.KEY_REQUIRED and idempotency_key is None:
            raise ValueError("Tool Contract requires an idempotency key")

        arguments_digest = sha256_digest(arguments)
        idempotency_key_digest = (
            self._digest_text(idempotency_key) if idempotency_key is not None else None
        )
        idempotency_scope = (
            f"{tool_name}\0{tool_version}\0{idempotency_key_digest}"
            if idempotency is ToolIdempotency.KEY_REQUIRED
            else call_id
        )
        timestamp = utc_now()
        async with (
            self._task_locks[task_id],
            self._tool_idempotency_locks[idempotency_scope],
        ):
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    self._ensure_task_accepts_events(task)
                    if await session.get(ToolCallRecord, call_id) is not None:
                        raise ToolCallAlreadyExistsError(call_id)
                    if idempotency is ToolIdempotency.KEY_REQUIRED:
                        if idempotency_key_digest is None:
                            raise RuntimeError("Required idempotency digest is missing")
                        existing_receipt = await session.scalar(
                            select(ToolIdempotencyReceiptRecord).where(
                                ToolIdempotencyReceiptRecord.tool_name == tool_name,
                                ToolIdempotencyReceiptRecord.tool_version == tool_version,
                                ToolIdempotencyReceiptRecord.key_digest
                                == idempotency_key_digest,
                            )
                        )
                        if existing_receipt is not None:
                            raise ToolIdempotencyKeyAlreadyUsedError(
                                existing_receipt.call_id
                            )

                    call = ToolCallRecord(
                        call_id=call_id,
                        task_id=task_id,
                        step_id=step_id,
                        attempt=attempt,
                        tool_name=tool_name,
                        tool_version=tool_version,
                        contract_digest=contract_digest,
                        arguments_digest=arguments_digest,
                        idempotency=idempotency.value,
                        idempotency_key_digest=idempotency_key_digest,
                        status=ToolCallStatus.REQUESTED.value,
                        requested_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(call)
                    if idempotency is ToolIdempotency.KEY_REQUIRED:
                        if idempotency_key_digest is None:
                            raise RuntimeError("Required idempotency digest is missing")
                        # No ORM relationship joins these security ledgers, so
                        # materialize the referenced call before its receipt.
                        await session.flush()
                        session.add(
                            ToolIdempotencyReceiptRecord(
                                receipt_id=f"tir_{uuid4().hex}",
                                call_id=call_id,
                                tool_name=tool_name,
                                tool_version=tool_version,
                                key_digest=idempotency_key_digest,
                                arguments_digest=arguments_digest,
                                created_at=timestamp,
                            )
                        )
                    payload: dict[str, Any] = {
                        "step_id": step_id,
                        "call_id": call_id,
                        "tool": tool_name,
                        "tool_version": tool_version,
                        "contract_digest": contract_digest,
                        "arguments_digest": arguments_digest,
                        "idempotency": idempotency.value,
                        "attempt": attempt,
                    }
                    if idempotency_key_digest is not None:
                        payload["idempotency_key_digest"] = idempotency_key_digest
                    if risk is not None:
                        payload["risk"] = risk
                    event = await self._append_event_record(
                        session,
                        task,
                        "tool.requested",
                        payload,
                        new_status=None,
                    )

        self._notify_outbox()
        return event

    async def start_tool_call(
        self,
        task_id: str,
        call_id: str,
        *,
        runner_id: str,
        authorization: ToolAuthorizationGrant,
        arguments: dict[str, Any],
        expected_resource_versions: dict[str, str],
    ) -> TaskEventRead:
        """Consume exact policy/approval truth at the uncertain Runner boundary."""
        if not runner_id:
            raise ValueError("Runner ID must not be empty")
        if authorization.task_id != task_id or authorization.call_id != call_id:
            raise ToolAuthorizationError(
                call_id,
                "Authorization grant belongs to another task or call",
            )

        expired_approval_id: str | None = None
        event: TaskEventRead | None = None
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    self._ensure_task_accepts_events(task)
                    call = await self._get_tool_call(session, task_id, call_id)
                    current = ToolCallStatus(call.status)
                    if current is not ToolCallStatus.REQUESTED:
                        raise InvalidToolCallTransitionError(
                            call_id,
                            current,
                            ToolCallStatus.RUNNING,
                        )

                    if (
                        authorization.step_id != call.step_id
                        or authorization.task_id != call.task_id
                        or authorization.tool_name != call.tool_name
                        or authorization.tool_version != call.tool_version
                        or authorization.contract_digest != call.contract_digest
                        or authorization.arguments_digest != call.arguments_digest
                        or authorization.arguments_digest != sha256_digest(arguments)
                        or authorization.expected_resource_versions_digest
                        != sha256_digest(expected_resource_versions)
                        or authorization.decision_id != call.policy_decision_id
                        or authorization.policy_revision != call.policy_revision
                        or authorization.resource_scope_digest != call.resource_scope_digest
                        or call.policy_event_id is None
                    ):
                        raise ToolAuthorizationError(
                            call_id,
                            "Authorization grant does not match durable policy truth",
                        )
                    policy_event = await session.get(
                        TaskEventRecord,
                        call.policy_event_id,
                    )
                    if (
                        policy_event is None
                        or policy_event.task_id != task_id
                        or policy_event.type != "policy.evaluated"
                        or policy_event.payload.get("decision_id") != authorization.decision_id
                        or policy_event.payload.get("rule_id") != authorization.rule_id
                        or policy_event.payload.get("reason_code") != authorization.reason_code
                        or policy_event.payload.get("effective_risk")
                        != authorization.effective_risk.value
                        or policy_event.payload.get("policy_revision")
                        != authorization.policy_revision
                        or policy_event.payload.get("request_digest")
                        != authorization.request_digest
                        or policy_event.payload.get("resource_scope_digest")
                        != authorization.resource_scope_digest
                    ):
                        raise ToolAuthorizationError(
                            call_id,
                            "Authorization grant does not match the policy audit event",
                        )

                    approval: ApprovalRecord | None = None
                    if call.policy_effect == PolicyEffect.REQUIRE_APPROVAL.value:
                        if authorization.approval_id is None:
                            raise ToolAuthorizationError(
                                call_id,
                                "This Tool call requires a user approval grant",
                            )
                        approval = await session.get(
                            ApprovalRecord,
                            authorization.approval_id,
                        )
                        if (
                            approval is None
                            or approval.task_id != task_id
                            or approval.call_id != call_id
                            or approval.decision != ApprovalStatus.APPROVED.value
                            or approval.decision_id != authorization.decision_id
                            or approval.policy_revision != authorization.policy_revision
                            or approval.contract_digest != authorization.contract_digest
                            or approval.arguments_digest != authorization.arguments_digest
                            or approval.binding_digest != authorization.request_digest
                            or approval.preview_hash != authorization.preview_hash
                            or approval.policy_rule_id != authorization.rule_id
                            or approval.reason_code != authorization.reason_code
                            or approval.risk_level != authorization.effective_risk.value
                            or approval.resolved_at is None
                            or authorization.approved_at != self._as_utc(approval.resolved_at)
                            or authorization.grant_expires_at != self._as_utc(approval.expires_at)
                        ):
                            raise ToolAuthorizationError(
                                call_id,
                                "Approval binding does not match the authorization grant",
                            )
                        if ApprovalStatus(approval.status) is not ApprovalStatus.APPROVED:
                            raise ToolAuthorizationError(
                                call_id,
                                "Approval has not been granted",
                            )
                        if approval.consumed_at is not None:
                            raise ToolAuthorizationError(
                                call_id,
                                "One-shot approval grant has already been consumed",
                            )
                        timestamp = utc_now()
                        if (
                            self._as_utc(approval.expires_at) <= timestamp
                            or authorization.grant_expires_at is None
                            or authorization.grant_expires_at <= timestamp
                        ):
                            approval.status = ApprovalStatus.EXPIRED.value
                            approval.updated_at = timestamp
                            await self._append_event_record(
                                session,
                                task,
                                "approval.expired",
                                {
                                    "approval_id": approval.approval_id,
                                    "call_id": call_id,
                                    "status": ApprovalStatus.EXPIRED.value,
                                    "decision": approval.decision,
                                    "reason_code": "APPROVAL_EXPIRED",
                                },
                                new_status=None,
                            )
                            await self._cancel_requested_call_for_approval(
                                session,
                                task,
                                approval,
                                timestamp=timestamp,
                                error_code="APPROVAL_EXPIRED",
                                resolution_source="policy",
                            )
                            current_task_status = TaskStatus(task.status)
                            self._ensure_transition(
                                task_id,
                                current_task_status,
                                TaskStatus.CANCELLED,
                            )
                            await self._append_event_record(
                                session,
                                task,
                                "task.cancelled",
                                {
                                    "from": current_task_status.value,
                                    "to": TaskStatus.CANCELLED.value,
                                    "command": "approval_expired",
                                    "requested_by": "system",
                                },
                                new_status=TaskStatus.CANCELLED,
                            )
                            expired_approval_id = approval.approval_id
                        else:
                            approval.consumed_at = timestamp
                            approval.updated_at = timestamp
                    elif call.policy_effect == PolicyEffect.ALLOW.value:
                        if authorization.approval_id is not None:
                            raise ToolAuthorizationError(
                                call_id,
                                "Auto-allowed call must not carry an approval",
                            )
                    else:
                        raise ToolAuthorizationError(
                            call_id,
                            "Durable policy did not authorize this Tool call",
                        )

                    if expired_approval_id is None:
                        timestamp = utc_now()
                        call.status = ToolCallStatus.RUNNING.value
                        call.runner_id = runner_id
                        call.authorization_id = authorization.authorization_id
                        call.started_at = timestamp
                        call.updated_at = timestamp
                        event_payload = self._tool_event_identity(call)
                        event_payload.update(
                            {
                                "authorization_id": authorization.authorization_id,
                                "decision_id": authorization.decision_id,
                                "policy_revision": authorization.policy_revision,
                                "approval_id": authorization.approval_id,
                            }
                        )
                        event = await self._append_event_record(
                            session,
                            task,
                            "tool.started",
                            event_payload,
                            new_status=None,
                        )

        self._notify_outbox()
        if expired_approval_id is not None:
            raise ApprovalExpiredError(expired_approval_id)
        if event is None:
            raise RuntimeError("Tool authorization committed without a start event")
        return event

    async def finish_tool_call(
        self,
        task_id: str,
        call_id: str,
        *,
        status: ToolCallStatus,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        resolution_source: str = "runner",
        fail_task: bool = True,
    ) -> tuple[TaskEventRead, ...]:
        """Persist one terminal Runner result; repeated or late results are no-ops."""
        if not status.is_terminal:
            raise ValueError("Tool call finish status must be terminal")
        if status is ToolCallStatus.SUCCEEDED and result is None:
            raise ValueError("A succeeded tool call requires a result")
        if status is not ToolCallStatus.SUCCEEDED and result is not None:
            raise ValueError("Only a succeeded tool call may persist a result")

        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    call = await self._get_tool_call(session, task_id, call_id)
                    current = ToolCallStatus(call.status)
                    if current.is_terminal:
                        return ()
                    requested_terminal_allowed = current is ToolCallStatus.REQUESTED and status in {
                        ToolCallStatus.FAILED,
                        ToolCallStatus.CANCELLED,
                    }
                    if current is not ToolCallStatus.RUNNING and not requested_terminal_allowed:
                        raise InvalidToolCallTransitionError(call_id, current, status)
                    self._ensure_task_accepts_events(task)

                    timestamp = utc_now()
                    call.status = status.value
                    call.resolution_source = resolution_source
                    call.error_code = self._resolved_error_code(status, error_code)
                    call.finished_at = timestamp
                    call.updated_at = timestamp
                    if status is ToolCallStatus.SUCCEEDED and result is not None:
                        await self._persist_commit_receipt(
                            session,
                            call,
                            result,
                            projected_at=timestamp,
                        )
                    if status is ToolCallStatus.UNKNOWN:
                        await self._ensure_tool_reconciliation(
                            session,
                            call,
                            timestamp=timestamp,
                        )
                    tool_event = await self._append_event_record(
                        session,
                        task,
                        self._terminal_tool_event_type(status),
                        self._terminal_tool_event_payload(
                            call,
                            status=status,
                            result=result,
                            retryable=retryable,
                        ),
                        new_status=None,
                    )
                    call.terminal_event_id = tool_event.event_id
                    events = [tool_event]

                    should_fail_task = status is ToolCallStatus.UNKNOWN or (
                        fail_task
                        and status
                        in {
                            ToolCallStatus.FAILED,
                            ToolCallStatus.CANCELLED,
                        }
                    )
                    if should_fail_task:
                        current_task_status = TaskStatus(task.status)
                        self._ensure_transition(
                            task_id,
                            current_task_status,
                            TaskStatus.FAILED,
                        )
                        task_failure_code = {
                            ToolCallStatus.FAILED: "TOOL_CALL_FAILED",
                            ToolCallStatus.CANCELLED: "TOOL_CALL_CANCELLED",
                            ToolCallStatus.UNKNOWN: "TOOL_RESULT_UNKNOWN",
                        }[status]
                        task_failure_type = (
                            "ToolResultUnknownError"
                            if status is ToolCallStatus.UNKNOWN
                            else "ToolCallFailedError"
                        )
                        events.append(
                            await self._append_event_record(
                                session,
                                task,
                                "task.failed",
                                {
                                    "error_type": task_failure_type,
                                    "code": task_failure_code,
                                    "message": (
                                        "Tool execution did not produce a usable result; "
                                        "the call was not replayed."
                                    ),
                                    "call_id": call.call_id,
                                    "tool_error_code": call.error_code,
                                },
                                new_status=TaskStatus.FAILED,
                            )
                        )

        self._notify_outbox()
        return tuple(events)

    @staticmethod
    async def _persist_commit_receipt(
        session: AsyncSession,
        call: ToolCallRecord,
        result: dict[str, Any],
        *,
        projected_at: datetime,
    ) -> None:
        raw_receipt = result.get("commit_receipt")
        if raw_receipt is None:
            if call.tool_name == "file.move" and call.tool_version == "1.0.0":
                raise ToolAuthorizationError(
                    call.call_id,
                    "Brokered file.move success omitted its durable commit receipt",
                )
            return
        receipt = ToolCommitReceipt.model_validate(raw_receipt)
        TaskService._validate_commit_receipt_binding(call, receipt)
        existing = await session.get(ToolCommitReceiptRecord, receipt.receipt_id)
        if existing is not None:
            if existing.call_id != call.call_id:
                raise ToolAuthorizationError(
                    call.call_id,
                    "Commit receipt identity is already bound to another Tool call",
                )
            return
        existing_for_call = await session.scalar(
            select(ToolCommitReceiptRecord).where(
                ToolCommitReceiptRecord.call_id == call.call_id
            )
        )
        if existing_for_call is not None:
            raise ToolAuthorizationError(
                call.call_id,
                "Tool call is already bound to another commit receipt",
            )
        session.add(
            ToolCommitReceiptRecord(
                receipt_id=receipt.receipt_id,
                call_id=receipt.call_id,
                tool_name=receipt.tool_name,
                tool_version=receipt.tool_version,
                authorization_id=receipt.authorization_id,
                approval_id=receipt.approval_id,
                preview_hash=receipt.preview_hash,
                prepare_digest=receipt.prepare_digest,
                idempotency_key_digest=receipt.idempotency_key_digest,
                resource_versions_before=dict(receipt.resource_versions_before),
                resource_versions_after=dict(receipt.resource_versions_after),
                commit_started_at=receipt.commit_started_at,
                receipt_recorded_at=receipt.receipt_recorded_at,
                projected_at=projected_at,
            )
        )

    @staticmethod
    def _validate_commit_receipt_binding(
        call: ToolCallRecord,
        receipt: ToolCommitReceipt,
    ) -> None:
        if (
            receipt.call_id != call.call_id
            or receipt.tool_name != call.tool_name
            or receipt.tool_version != call.tool_version
            or receipt.authorization_id != call.authorization_id
            or receipt.idempotency_key_digest != call.idempotency_key_digest
        ):
            raise ToolAuthorizationError(
                call.call_id,
                "Commit receipt does not match the durable Tool call binding",
            )

    async def recover_incomplete_tool_calls(
        self,
        *,
        recoverable_requested_call_ids: frozenset[str] = frozenset(),
    ) -> ToolCallRecoveryResult:
        """Idempotently reconcile calls left non-terminal by a process restart."""
        incomplete_statuses = (
            ToolCallStatus.REQUESTED.value,
            ToolCallStatus.RUNNING.value,
        )
        async with self._database.session() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(ToolCallRecord.task_id)
                        .where(ToolCallRecord.status.in_(incomplete_statuses))
                        .distinct()
                        .order_by(ToolCallRecord.task_id)
                    )
                ).all()
            )

        requested_failed = 0
        running_unknown = 0
        tasks_failed = 0
        events_created = 0
        notify_outbox = False
        for task_id in task_ids:
            async with self._task_locks[task_id]:
                async with self._database.session() as session:
                    async with session.begin():
                        task = await session.get(TaskRecord, task_id)
                        if task is None:
                            continue
                        calls = list(
                            (
                                await session.scalars(
                                    select(ToolCallRecord)
                                    .where(
                                        ToolCallRecord.task_id == task_id,
                                        ToolCallRecord.status.in_(incomplete_statuses),
                                    )
                                    .order_by(
                                        ToolCallRecord.requested_at,
                                        ToolCallRecord.call_id,
                                    )
                                )
                            ).all()
                        )
                        calls = [
                            call
                            for call in calls
                            if not (
                                ToolCallStatus(call.status) is ToolCallStatus.REQUESTED
                                and call.call_id in recoverable_requested_call_ids
                            )
                        ]
                        if not calls:
                            continue

                        task_is_terminal = TaskStatus(task.status).is_terminal
                        task_requested_failed = 0
                        task_running_unknown = 0
                        task_events_created = 0
                        recovered_call_ids: list[str] = []
                        for call in calls:
                            previous = ToolCallStatus(call.status)
                            terminal = (
                                ToolCallStatus.FAILED
                                if previous is ToolCallStatus.REQUESTED
                                else ToolCallStatus.UNKNOWN
                            )
                            call.status = terminal.value
                            call.resolution_source = "startup_recovery"
                            call.error_code = (
                                "TOOL_CALL_NOT_DISPATCHED_AFTER_RESTART"
                                if previous is ToolCallStatus.REQUESTED
                                else "TOOL_RESULT_UNCERTAIN_AFTER_RESTART"
                            )
                            timestamp = utc_now()
                            call.finished_at = timestamp
                            call.updated_at = timestamp
                            if terminal is ToolCallStatus.UNKNOWN:
                                await self._ensure_tool_reconciliation(
                                    session,
                                    call,
                                    timestamp=timestamp,
                                )
                            recovered_call_ids.append(call.call_id)

                            if previous is ToolCallStatus.REQUESTED:
                                task_requested_failed += 1
                            else:
                                task_running_unknown += 1

                            if task_is_terminal:
                                continue
                            event = await self._append_event_record(
                                session,
                                task,
                                self._terminal_tool_event_type(terminal),
                                self._terminal_tool_event_payload(
                                    call,
                                    status=terminal,
                                    result=None,
                                    retryable=False,
                                ),
                                new_status=None,
                            )
                            call.terminal_event_id = event.event_id
                            task_events_created += 1

                        if not task_is_terminal:
                            current_task_status = TaskStatus(task.status)
                            self._ensure_transition(
                                task_id,
                                current_task_status,
                                TaskStatus.FAILED,
                            )
                            task_error_code = (
                                "TOOL_RESULT_UNKNOWN"
                                if task_running_unknown
                                else "TOOL_CALL_INTERRUPTED_BEFORE_DISPATCH"
                            )
                            await self._append_event_record(
                                session,
                                task,
                                "task.failed",
                                {
                                    "error_type": "ToolCallRecoveryError",
                                    "code": task_error_code,
                                    "message": (
                                        "Incomplete Tool Runner calls were reconciled "
                                        "during startup and were not replayed."
                                    ),
                                    "call_ids": recovered_call_ids,
                                    "requested_failed": task_requested_failed,
                                    "running_unknown": task_running_unknown,
                                },
                                new_status=TaskStatus.FAILED,
                            )
                            task_events_created += 1
                            tasks_failed += 1

                        requested_failed += task_requested_failed
                        running_unknown += task_running_unknown
                        events_created += task_events_created
                        notify_outbox = notify_outbox or task_events_created > 0

        if notify_outbox:
            self._notify_outbox()
        return ToolCallRecoveryResult(
            requested_failed=requested_failed,
            running_unknown=running_unknown,
            tasks_failed=tasks_failed,
            events_created=events_created,
        )

    async def recover_pending_approvals(
        self,
        *,
        recoverable_task_ids: frozenset[str] = frozenset(),
    ) -> ApprovalRecoveryResult:
        """Fail closed when an unconsumed approval lost its runtime checkpoint."""
        recoverable_statuses = (
            ApprovalStatus.PENDING.value,
            ApprovalStatus.APPROVED.value,
        )
        async with self._database.session() as session:
            task_ids = list(
                (
                    await session.scalars(
                        select(ApprovalRecord.task_id)
                        .where(
                            ApprovalRecord.status.in_(recoverable_statuses),
                            ApprovalRecord.consumed_at.is_(None),
                        )
                        .distinct()
                        .order_by(ApprovalRecord.task_id)
                    )
                ).all()
            )

        approvals_cancelled = 0
        tasks_cancelled = 0
        events_created = 0
        for task_id in task_ids:
            if task_id in recoverable_task_ids:
                continue
            async with self._task_locks[task_id]:
                async with self._database.session() as session:
                    async with session.begin():
                        task = await session.get(TaskRecord, task_id)
                        approvals = list(
                            (
                                await session.scalars(
                                    select(ApprovalRecord)
                                    .where(
                                        ApprovalRecord.task_id == task_id,
                                        ApprovalRecord.status.in_(recoverable_statuses),
                                        ApprovalRecord.consumed_at.is_(None),
                                    )
                                    .order_by(ApprovalRecord.requested_at)
                                )
                            ).all()
                        )
                        if not approvals:
                            continue

                        timestamp = utc_now()
                        task_is_terminal = task is None or TaskStatus(task.status).is_terminal
                        for approval in approvals:
                            previous_status = ApprovalStatus(approval.status)
                            approval.status = ApprovalStatus.CANCELLED.value
                            if previous_status is ApprovalStatus.PENDING:
                                approval.resolved_at = timestamp
                                approval.resolved_by = "system"
                                approval.resolution_reason = (
                                    "API restarted before this approval was resolved"
                                )
                            approval.updated_at = timestamp
                            approvals_cancelled += 1
                            if task_is_terminal or task is None:
                                continue

                            await self._append_event_record(
                                session,
                                task,
                                (
                                    "approval.resolved"
                                    if previous_status is ApprovalStatus.PENDING
                                    else "approval.invalidated"
                                ),
                                {
                                    "approval_id": approval.approval_id,
                                    "call_id": approval.call_id,
                                    "status": ApprovalStatus.CANCELLED.value,
                                    "decision": approval.decision,
                                    "reason_code": "APPROVAL_RUNTIME_LOST",
                                },
                                new_status=None,
                            )
                            events_created += 1
                            call = await self._get_tool_call(
                                session,
                                task_id,
                                approval.call_id,
                            )
                            if ToolCallStatus(call.status) is ToolCallStatus.REQUESTED:
                                call.status = ToolCallStatus.CANCELLED.value
                                call.resolution_source = "startup_recovery"
                                call.error_code = "APPROVAL_RUNTIME_LOST"
                                call.finished_at = timestamp
                                call.updated_at = timestamp
                                tool_event = await self._append_event_record(
                                    session,
                                    task,
                                    "tool.cancelled",
                                    self._terminal_tool_event_payload(
                                        call,
                                        status=ToolCallStatus.CANCELLED,
                                        result=None,
                                        retryable=False,
                                    ),
                                    new_status=None,
                                )
                                call.terminal_event_id = tool_event.event_id
                                events_created += 1

                        if not task_is_terminal and task is not None:
                            current = TaskStatus(task.status)
                            self._ensure_transition(
                                task_id,
                                current,
                                TaskStatus.CANCELLED,
                            )
                            await self._append_event_record(
                                session,
                                task,
                                "task.cancelled",
                                {
                                    "from": current.value,
                                    "to": TaskStatus.CANCELLED.value,
                                    "command": "approval_recovery",
                                    "requested_by": "system",
                                    "reason": (
                                        "Pending approval cannot safely resume after API restart"
                                    ),
                                },
                                new_status=TaskStatus.CANCELLED,
                            )
                            tasks_cancelled += 1
                            events_created += 1

        if events_created:
            self._notify_outbox()
        return ApprovalRecoveryResult(
            approvals_cancelled=approvals_cancelled,
            tasks_cancelled=tasks_cancelled,
            events_created=events_created,
        )

    async def append_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        new_status: TaskStatus | None = None,
    ) -> TaskEventRead:
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    current = TaskStatus(task.status)
                    if current.is_terminal:
                        raise InvalidTaskTransitionError(
                            task_id,
                            current,
                            new_status or current,
                        )
                    if new_status is not None:
                        self._ensure_transition(task_id, current, new_status)
                    event = await self._append_event_record(
                        session,
                        task,
                        event_type,
                        payload,
                        new_status=new_status,
                    )

            self._notify_outbox()
            return event

    async def transition_task(
        self,
        task_id: str,
        target: TaskStatus,
        *,
        command: str,
        reason: str | None = None,
        requested_by: str = "user",
    ) -> TaskRead:
        event_created = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)

                    current = TaskStatus(task.status)
                    if (command == "pause" and current is TaskStatus.PAUSED) or (
                        command == "cancel" and current is TaskStatus.CANCELLED
                    ):
                        snapshot = self._to_task(task)
                    else:
                        self._ensure_transition(task_id, current, target)
                        payload: dict[str, Any] = {
                            "from": current.value,
                            "to": target.value,
                            "command": command,
                            "requested_by": requested_by,
                        }
                        if reason is not None:
                            payload["reason"] = reason
                        event_type = (
                            "task.cancelled"
                            if target is TaskStatus.CANCELLED
                            else "task.status_changed"
                        )
                        await self._append_event_record(
                            session,
                            task,
                            event_type,
                            payload,
                            new_status=target,
                        )
                        if command in {"pause", "resume"} and not target.is_terminal:
                            await self._rebind_task_checkpoint(session, task)
                        snapshot = self._to_task(task)
                        event_created = True

        if event_created:
            self._notify_outbox()
        return snapshot

    async def cancel_task(
        self,
        task_id: str,
        *,
        reason: str | None = None,
        requested_by: str = "user",
    ) -> TaskRead:
        """Cancel a task and invalidate any unconsumed one-shot grant atomically."""
        event_created = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    current = TaskStatus(task.status)
                    if current is TaskStatus.CANCELLED:
                        return self._to_task(task)
                    self._ensure_transition(task_id, current, TaskStatus.CANCELLED)

                    invalidatable = list(
                        (
                            await session.scalars(
                                select(ApprovalRecord)
                                .where(
                                    ApprovalRecord.task_id == task_id,
                                    ApprovalRecord.status.in_(
                                        (
                                            ApprovalStatus.PENDING.value,
                                            ApprovalStatus.APPROVED.value,
                                        )
                                    ),
                                    ApprovalRecord.consumed_at.is_(None),
                                )
                                .order_by(ApprovalRecord.requested_at)
                            )
                        ).all()
                    )
                    if invalidatable:
                        timestamp = utc_now()
                        for approval in invalidatable:
                            previous_status = ApprovalStatus(approval.status)
                            approval.status = ApprovalStatus.CANCELLED.value
                            if previous_status is ApprovalStatus.PENDING:
                                approval.resolved_at = timestamp
                                approval.resolved_by = requested_by
                                approval.resolution_reason = reason or "Task cancelled by user"
                            approval.updated_at = timestamp
                            await self._append_event_record(
                                session,
                                task,
                                (
                                    "approval.resolved"
                                    if previous_status is ApprovalStatus.PENDING
                                    else "approval.invalidated"
                                ),
                                {
                                    "approval_id": approval.approval_id,
                                    "call_id": approval.call_id,
                                    "status": ApprovalStatus.CANCELLED.value,
                                    "decision": approval.decision,
                                    "reason_code": "TASK_CANCELLED",
                                },
                                new_status=None,
                            )
                            call = await self._get_tool_call(
                                session,
                                task_id,
                                approval.call_id,
                            )
                            if ToolCallStatus(call.status) is ToolCallStatus.REQUESTED:
                                call.status = ToolCallStatus.CANCELLED.value
                                call.resolution_source = "control_plane"
                                call.error_code = "APPROVAL_CANCELLED_WITH_TASK"
                                call.finished_at = timestamp
                                call.updated_at = timestamp
                                tool_event = await self._append_event_record(
                                    session,
                                    task,
                                    "tool.cancelled",
                                    self._terminal_tool_event_payload(
                                        call,
                                        status=ToolCallStatus.CANCELLED,
                                        result=None,
                                        retryable=False,
                                    ),
                                    new_status=None,
                                )
                                call.terminal_event_id = tool_event.event_id

                    payload: dict[str, Any] = {
                        "from": current.value,
                        "to": TaskStatus.CANCELLED.value,
                        "command": "cancel",
                        "requested_by": requested_by,
                    }
                    if reason is not None:
                        payload["reason"] = reason
                    await self._append_event_record(
                        session,
                        task,
                        "task.cancelled",
                        payload,
                        new_status=TaskStatus.CANCELLED,
                    )
                    snapshot = self._to_task(task)
                    event_created = True

        if event_created:
            self._notify_outbox()
        return snapshot

    async def _append_event_record(
        self,
        session: AsyncSession,
        task: TaskRecord,
        event_type: str,
        payload: dict[str, Any],
        *,
        new_status: TaskStatus | None,
    ) -> TaskEventRead:
        task.last_event_seq += 1
        timestamp = utc_now()
        task.updated_at = timestamp
        if new_status is not None:
            task.status = new_status.value

        previous_event = await session.scalar(
            select(TaskEventRecord)
            .where(TaskEventRecord.task_id == task.task_id)
            .order_by(TaskEventRecord.seq.desc())
            .limit(1)
        )
        trace_id = previous_event.trace_id if previous_event else f"trc_{uuid4().hex}"
        event_record = TaskEventRecord(
            event_id=f"evt_{uuid4().hex}",
            task_id=task.task_id,
            seq=task.last_event_seq,
            type=event_type,
            timestamp=timestamp,
            trace_id=trace_id,
            payload=payload,
        )
        event = self._to_event(event_record)
        session.add(event_record)
        await session.flush()
        session.add(self._to_outbox(event))
        if new_status is not None and new_status.is_terminal:
            await self._delete_task_checkpoint(session, task.task_id)
        return event

    @staticmethod
    def _ensure_task_accepts_events(task: TaskRecord) -> None:
        current = TaskStatus(task.status)
        if current.is_terminal:
            raise InvalidTaskTransitionError(task.task_id, current, current)

    @staticmethod
    async def _get_tool_call(
        session: AsyncSession,
        task_id: str,
        call_id: str,
    ) -> ToolCallRecord:
        call = await session.get(ToolCallRecord, call_id)
        if call is None or call.task_id != task_id:
            raise ToolCallNotFoundError(task_id, call_id)
        return call

    async def _cancel_requested_call_for_approval(
        self,
        session: AsyncSession,
        task: TaskRecord,
        approval: ApprovalRecord,
        *,
        timestamp: datetime,
        error_code: str,
        resolution_source: str,
    ) -> TaskEventRead:
        call = await self._get_tool_call(session, task.task_id, approval.call_id)
        current = ToolCallStatus(call.status)
        if current is not ToolCallStatus.REQUESTED:
            raise InvalidToolCallTransitionError(
                call.call_id,
                current,
                ToolCallStatus.CANCELLED,
            )
        call.status = ToolCallStatus.CANCELLED.value
        call.resolution_source = resolution_source
        call.error_code = error_code
        call.finished_at = timestamp
        call.updated_at = timestamp
        event = await self._append_event_record(
            session,
            task,
            "tool.cancelled",
            self._terminal_tool_event_payload(
                call,
                status=ToolCallStatus.CANCELLED,
                result=None,
                retryable=False,
            ),
            new_status=None,
        )
        call.terminal_event_id = event.event_id
        return event

    @staticmethod
    def _tool_event_identity(call: ToolCallRecord) -> dict[str, Any]:
        return {
            "step_id": call.step_id,
            "call_id": call.call_id,
            "tool": call.tool_name,
            "tool_version": call.tool_version,
            "contract_digest": call.contract_digest,
            "attempt": call.attempt,
            "runner_id": call.runner_id,
        }

    @staticmethod
    def _resolved_error_code(
        status: ToolCallStatus,
        error_code: str | None,
    ) -> str | None:
        if status is ToolCallStatus.SUCCEEDED:
            return None
        if error_code is not None:
            return error_code
        return {
            ToolCallStatus.FAILED: "TOOL_EXECUTION_FAILED",
            ToolCallStatus.CANCELLED: "TOOL_CANCELLED",
            ToolCallStatus.UNKNOWN: "TOOL_RESULT_UNKNOWN",
        }[status]

    @staticmethod
    def _terminal_tool_event_type(status: ToolCallStatus) -> str:
        return {
            ToolCallStatus.SUCCEEDED: "tool.completed",
            ToolCallStatus.FAILED: "tool.failed",
            ToolCallStatus.CANCELLED: "tool.cancelled",
            ToolCallStatus.UNKNOWN: "tool.unknown",
        }[status]

    @classmethod
    def _terminal_tool_event_payload(
        cls,
        call: ToolCallRecord,
        *,
        status: ToolCallStatus,
        result: dict[str, Any] | None,
        retryable: bool,
    ) -> dict[str, Any]:
        payload = cls._tool_event_identity(call)
        payload.update(
            {
                "status": status.value,
                "resolution_source": call.resolution_source,
            }
        )
        if status is ToolCallStatus.SUCCEEDED:
            payload["result"] = result
        else:
            payload.update(
                {
                    "code": call.error_code,
                    "retryable": (retryable if status is ToolCallStatus.FAILED else False),
                }
            )
        if status is ToolCallStatus.UNKNOWN:
            payload["requires_reconciliation"] = True
        return payload

    @staticmethod
    async def _ensure_tool_reconciliation(
        session: AsyncSession,
        call: ToolCallRecord,
        *,
        timestamp: datetime,
    ) -> None:
        existing = await session.scalar(
            select(ToolReconciliationRecord).where(
                ToolReconciliationRecord.call_id == call.call_id
            )
        )
        if existing is not None:
            return
        session.add(
            ToolReconciliationRecord(
                reconciliation_id=f"rec_{uuid4().hex}",
                task_id=call.task_id,
                call_id=call.call_id,
                status=ReconciliationStatus.PENDING.value,
                unknown_at=timestamp,
                updated_at=timestamp,
            )
        )

    @staticmethod
    def _validate_reconciliation_idempotency(
        record: ToolReconciliationIdempotencyRecord,
        *,
        operation: str,
        fingerprint: str,
        reconciliation_id: str,
    ) -> None:
        if (
            record.operation != operation
            or record.request_fingerprint != fingerprint
            or record.reconciliation_id != reconciliation_id
        ):
            raise ReconciliationIdempotencyConflictError(
                "Idempotency key was already used for another reconciliation request"
            )

    async def _to_reconciliation(
        self,
        session: AsyncSession,
        record: ToolReconciliationRecord,
    ) -> ReconciliationRead:
        call = await session.get(ToolCallRecord, record.call_id)
        if call is None:
            raise ToolCallNotFoundError(record.task_id, record.call_id)
        receipt_record = await session.scalar(
            select(ToolIdempotencyReceiptRecord).where(
                ToolIdempotencyReceiptRecord.call_id == record.call_id
            )
        )
        receipt = (
            ToolIdempotencyReceiptRead(
                receipt_id=receipt_record.receipt_id,
                call_id=receipt_record.call_id,
                tool_name=receipt_record.tool_name,
                tool_version=receipt_record.tool_version,
                key_digest=receipt_record.key_digest,
                arguments_digest=receipt_record.arguments_digest,
                created_at=self._as_utc(receipt_record.created_at),
            )
            if receipt_record is not None
            else None
        )
        evidence_records = list(
            (
                await session.scalars(
                    select(ToolReconciliationEvidenceRecord)
                    .where(
                        ToolReconciliationEvidenceRecord.reconciliation_id
                        == record.reconciliation_id
                    )
                    .order_by(
                        ToolReconciliationEvidenceRecord.observed_at.desc(),
                        ToolReconciliationEvidenceRecord.evidence_id.desc(),
                    )
                )
            ).all()
        )
        receipt_evidence = tuple(
            [
                await self._to_reconciliation_evidence(session, item)
                for item in evidence_records
            ]
        )
        status = ReconciliationStatus(record.status)
        outcome = (
            ReconciliationOutcome(record.outcome)
            if record.outcome is not None
            else None
        )
        can_create_attempt = (
            status is ReconciliationStatus.RESOLVED
            and outcome is ReconciliationOutcome.CONFIRMED_NO_EFFECT
            and record.new_attempt_task_id is None
            and record.compensation_created_at is None
            and self._supports_explicit_attempt(call)
        )
        has_commit_receipt = any(
            item.kind is ReconciliationEvidenceKind.COMMIT_RECEIPT
            for item in receipt_evidence
        )
        can_create_compensation = (
            call.tool_name == FILE_MOVE_CONTRACT.name
            and call.tool_version == FILE_MOVE_CONTRACT.version
            and call.contract_digest == FILE_MOVE_CONTRACT.digest
            and has_commit_receipt
            and record.new_attempt_task_id is None
            and record.compensation_created_at is None
        )
        return ReconciliationRead(
            reconciliation_id=record.reconciliation_id,
            task_id=record.task_id,
            call_id=record.call_id,
            step_id=call.step_id,
            attempt=call.attempt,
            tool_name=call.tool_name,
            tool_version=call.tool_version,
            contract_digest=call.contract_digest,
            arguments_digest=call.arguments_digest,
            idempotency=ToolIdempotency(call.idempotency),
            runner_id=call.runner_id,
            call_error_code=call.error_code,
            call_resolution_source=call.resolution_source,
            call_requested_at=self._as_utc(call.requested_at),
            call_started_at=(
                self._as_utc(call.started_at) if call.started_at is not None else None
            ),
            call_finished_at=(
                self._as_utc(call.finished_at) if call.finished_at is not None else None
            ),
            status=status,
            outcome=outcome,
            evidence_summary=record.evidence_summary,
            resolved_by=record.resolved_by,
            unknown_at=self._as_utc(record.unknown_at),
            resolved_at=(
                self._as_utc(record.resolved_at)
                if record.resolved_at is not None
                else None
            ),
            can_create_attempt=can_create_attempt,
            new_attempt_task_id=record.new_attempt_task_id,
            new_attempt_created_at=(
                self._as_utc(record.new_attempt_created_at)
                if record.new_attempt_created_at is not None
                else None
            ),
            can_create_compensation=can_create_compensation,
            compensation_task_id=record.compensation_task_id,
            compensation_receipt_id=record.compensation_receipt_id,
            compensation_created_at=(
                self._as_utc(record.compensation_created_at)
                if record.compensation_created_at is not None
                else None
            ),
            idempotency_receipt=receipt,
            receipt_evidence=receipt_evidence,
            updated_at=self._as_utc(record.updated_at),
        )

    async def _to_reconciliation_evidence(
        self,
        session: AsyncSession,
        record: ToolReconciliationEvidenceRecord,
    ) -> ReconciliationReceiptEvidenceRead:
        receipt_record = (
            await session.get(ToolCommitReceiptRecord, record.receipt_id)
            if record.receipt_id is not None
            else None
        )
        if record.receipt_id is not None and receipt_record is None:
            raise RuntimeError("Reconciliation evidence lost its commit receipt")
        return ReconciliationReceiptEvidenceRead(
            evidence_id=record.evidence_id,
            kind=ReconciliationEvidenceKind(record.kind),
            queried_runner_id=record.queried_runner_id,
            commit_receipt=(
                self._to_commit_receipt(receipt_record)
                if receipt_record is not None
                else None
            ),
            error_code=record.error_code,
            observed_at=self._as_utc(record.observed_at),
        )

    def _to_commit_receipt(
        self,
        record: ToolCommitReceiptRecord,
    ) -> ToolCommitReceipt:
        return ToolCommitReceipt(
            receipt_id=record.receipt_id,
            call_id=record.call_id,
            tool_name=record.tool_name,
            tool_version=record.tool_version,
            authorization_id=record.authorization_id,
            approval_id=record.approval_id,
            preview_hash=record.preview_hash,
            prepare_digest=record.prepare_digest,
            idempotency_key_digest=record.idempotency_key_digest,
            resource_versions_before=dict(record.resource_versions_before),
            resource_versions_after=dict(record.resource_versions_after),
            commit_started_at=self._as_utc(record.commit_started_at),
            receipt_recorded_at=self._as_utc(record.receipt_recorded_at),
        )

    async def _write_task_checkpoint(
        self,
        session: AsyncSession,
        task: TaskRecord,
        checkpoint: TaskCheckpointPayload,
    ) -> TaskRuntimeCheckpointRecord | None:
        codec = self._checkpoint_codec
        if codec is None:
            return None
        if checkpoint.task_id != task.task_id:
            raise ValueError("Task checkpoint belongs to another task")
        protected = codec.encode(checkpoint)
        timestamp = utc_now()
        record = await session.get(TaskRuntimeCheckpointRecord, task.task_id)
        if record is None:
            record = TaskRuntimeCheckpointRecord(
                task_id=task.task_id,
                schema_version=checkpoint.schema_version,
                next_stage=checkpoint.next_stage,
                event_seq=task.last_event_seq,
                revision=1,
                protection_scheme=protected.scheme,
                protected_payload=protected.payload,
                payload_digest=hashlib.sha256(protected.payload).hexdigest(),
                created_at=timestamp,
                updated_at=timestamp,
            )
            session.add(record)
        else:
            record.schema_version = checkpoint.schema_version
            record.next_stage = checkpoint.next_stage
            record.event_seq = task.last_event_seq
            record.revision += 1
            record.protection_scheme = protected.scheme
            record.protected_payload = protected.payload
            record.payload_digest = hashlib.sha256(protected.payload).hexdigest()
            record.updated_at = timestamp
        await session.flush()
        return record

    @staticmethod
    def _to_durable_checkpoint(
        record: TaskRuntimeCheckpointRecord,
        payload: TaskCheckpointPayload,
    ) -> DurableTaskCheckpoint:
        return DurableTaskCheckpoint(
            payload=payload,
            event_seq=record.event_seq,
            revision=record.revision,
        )

    @staticmethod
    async def _delete_task_checkpoint(
        session: AsyncSession,
        task_id: str,
    ) -> None:
        record = await session.get(TaskRuntimeCheckpointRecord, task_id)
        if record is not None:
            await session.delete(record)

    @staticmethod
    async def _rebind_task_checkpoint(
        session: AsyncSession,
        task: TaskRecord,
    ) -> None:
        record = await session.get(TaskRuntimeCheckpointRecord, task.task_id)
        if record is not None:
            record.event_seq = task.last_event_seq
            record.revision += 1
            record.updated_at = utc_now()

    @staticmethod
    def _supports_explicit_attempt(call: ToolCallRecord) -> bool:
        """Only expose retries whose request can be rebuilt without hidden arguments."""
        return (
            call.tool_name == DISK_USAGE_CONTRACT.name
            and call.tool_version == DISK_USAGE_CONTRACT.version
            and call.contract_digest == DISK_USAGE_CONTRACT.digest
        )

    @staticmethod
    def _digest_text(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _ensure_transition(
        task_id: str,
        current: TaskStatus,
        target: TaskStatus,
    ) -> None:
        if target not in ALLOWED_TASK_TRANSITIONS[current]:
            raise InvalidTaskTransitionError(task_id, current, target)

    @staticmethod
    def _to_outbox(event: TaskEventRead) -> OutboxMessageRecord:
        return OutboxMessageRecord(
            message_id=f"obx_{uuid4().hex}",
            task_id=event.task_id,
            event_id=event.event_id,
            event_seq=event.seq,
            topic="task.event",
            payload=event.model_dump(mode="json"),
            attempt_count=0,
            available_at=event.timestamp,
            created_at=event.timestamp,
        )

    def _notify_outbox(self) -> None:
        if self._outbox_notify is not None:
            self._outbox_notify()

    def _to_task(self, record: TaskRecord) -> TaskRead:
        return TaskRead(
            task_id=record.task_id,
            conversation_id=record.conversation_id,
            goal=record.goal,
            status=TaskStatus(record.status),
            mode=record.mode,
            privacy_mode=PRIVACY_MODE_ADAPTER.validate_python(record.privacy_mode),
            constraints=list(record.constraints or []),
            last_event_seq=record.last_event_seq,
            event_stream=f"{self._api_prefix}/ws/tasks/{record.task_id}",
            created_at=self._as_utc(record.created_at),
            updated_at=self._as_utc(record.updated_at),
        )

    @classmethod
    def _to_approval(cls, record: ApprovalRecord) -> ApprovalRead:
        return ApprovalRead(
            approval_id=record.approval_id,
            task_id=record.task_id,
            call_id=record.call_id,
            tool_name=record.tool_name,
            tool_version=record.tool_version,
            status=ApprovalStatus(record.status),
            decision=(ApprovalDecision(record.decision) if record.decision is not None else None),
            risk_level=ToolRiskLevel(record.risk_level),
            title=record.title,
            purpose=record.purpose,
            decision_id=record.decision_id,
            policy_rule_id=record.policy_rule_id,
            policy_revision=record.policy_revision,
            reason_code=record.reason_code,
            reversible=record.reversible,
            capabilities=tuple(record.capabilities or []),
            resource_scope=tuple(
                ApprovalResourceRead.model_validate(resource)
                for resource in (record.resource_scope or [])
            ),
            consequences=tuple(record.consequences or []),
            data_egress=DataEgress.model_validate(record.data_egress),
            preview_hash=record.preview_hash,
            requested_at=cls._as_utc(record.requested_at),
            expires_at=cls._as_utc(record.expires_at),
            resolved_at=(
                cls._as_utc(record.resolved_at) if record.resolved_at is not None else None
            ),
            resolution_reason=record.resolution_reason,
            consumed_at=(
                cls._as_utc(record.consumed_at) if record.consumed_at is not None else None
            ),
            updated_at=cls._as_utc(record.updated_at),
        )

    @staticmethod
    def _to_event(record: TaskEventRecord) -> TaskEventRead:
        return TaskEventRead(
            event_id=record.event_id,
            task_id=record.task_id,
            seq=record.seq,
            type=record.type,
            timestamp=TaskService._as_utc(record.timestamp),
            trace_id=record.trace_id,
            payload=dict(record.payload or {}),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
