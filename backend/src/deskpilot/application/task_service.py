"""Task command/query service with transactional event persistence."""

import asyncio
import hashlib
import logging
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
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
from deskpilot.domain.effect_graph import (
    EFFECT_DAG_MAX_DEPENDENCIES,
    EFFECT_DAG_MAX_NODES,
    EFFECT_DAG_MAX_PREDECESSORS,
    EFFECT_DAG_SCHEMA_VERSION,
    EFFECT_GRAPH_SCHEMA_VERSION,
    CompensationStrategy,
    EffectAttemptKind,
    EffectAttemptRead,
    EffectAttemptStatus,
    EffectBranchDecisionProof,
    EffectBranchDecisionRead,
    EffectCompensationPlanRead,
    EffectCompensationWaveRead,
    EffectDagAdmissionProof,
    EffectDagNodeDefinition,
    EffectEdgeKind,
    EffectEdgeRead,
    EffectExecutionMode,
    EffectGraphLeaseRead,
    EffectGraphRead,
    EffectGraphStatus,
    EffectNodeClaimRead,
    EffectNodeDefinition,
    EffectNodeLeaseRead,
    EffectNodeRead,
    EffectNodeStatus,
    EffectPredecessorProof,
    EffectReadyNodeProof,
    EffectReadySetCheckpointRead,
    EffectState,
    EffectTransitionRead,
    ToolEffectRead,
    effect_branch_decision_id,
    effect_call_id,
    effect_edge_id,
    effect_graph_id,
    effect_node_id,
    tool_effect_id,
)
from deskpilot.domain.model_contracts import PrivacyMode
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    ToolAuthorizationGrant,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import (
    GraphRecoveryAction,
    GraphRecoveryStatus,
    ReconciliationAttemptRead,
    ReconciliationCompensationRead,
    ReconciliationEvidenceKind,
    ReconciliationEvidenceRefreshRead,
    ReconciliationGraphRecoveryRead,
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
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.effect_ready_queries import (
    build_effect_ready_page_statement,
)
from deskpilot.infrastructure.models import (
    ApprovalRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    TaskRecord,
    TaskRuntimeCheckpointRecord,
    ToolCallRecord,
    ToolCommitReceiptRecord,
    ToolEffectAttemptRecord,
    ToolEffectBranchDecisionRecord,
    ToolEffectCompensationPlanRecord,
    ToolEffectDagAdmissionRecord,
    ToolEffectDagReadyNodeRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectEdgeRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
    ToolEffectReadySetCheckpointRecord,
    ToolEffectRecord,
    ToolEffectTransitionRecord,
    ToolIdempotencyReceiptRecord,
    ToolReconciliationEvidenceRecord,
    ToolReconciliationIdempotencyRecord,
    ToolReconciliationRecord,
    utc_now,
)
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_node_claim_statement,
    build_postgresql_node_lock_statement,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT
from deskpilot.tools.files import (
    FILE_MOVE_CONTRACT,
    FILE_MOVE_DESTINATION_CAPABILITY,
    FILE_MOVE_SOURCE_CAPABILITY,
)

logger = logging.getLogger(__name__)
PRIVACY_MODE_ADAPTER: TypeAdapter[PrivacyMode] = TypeAdapter(PrivacyMode)
IDEMPOTENT_COMMAND_RESULT = TypeVar("IDEMPOTENT_COMMAND_RESULT")
BRANCH_TOKEN_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class TaskNotFoundError(LookupError):
    def __init__(self, task_id: str) -> None:
        super().__init__(f"Task not found: {task_id}")
        self.task_id = task_id


class TaskRevisionConflictError(RuntimeError):
    code = "TASK_REVISION_CONFLICT"

    def __init__(
        self,
        task_id: str,
        *,
        expected_last_event_seq: int,
        current_last_event_seq: int,
        current_status: TaskStatus,
    ) -> None:
        super().__init__(f"Task revision is stale: {task_id}")
        self.task_id = task_id
        self.expected_last_event_seq = expected_last_event_seq
        self.current_last_event_seq = current_last_event_seq
        self.current_status = current_status


class EffectGraphNotFoundError(LookupError):
    code = "EFFECT_GRAPH_NOT_FOUND"

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Tool effect graph not found for task: {task_id}")
        self.task_id = task_id


class InvalidEffectTransitionError(ValueError):
    code = "EFFECT_TRANSITION_INVALID"


class EffectGraphLeaseUnavailableError(RuntimeError):
    code = "EFFECT_GRAPH_LEASE_UNAVAILABLE"

    def __init__(self, task_id: str) -> None:
        super().__init__(f"Tool effect graph lease is unavailable: {task_id}")
        self.task_id = task_id


class EffectGraphFenceRejectedError(RuntimeError):
    code = "EFFECT_GRAPH_FENCE_REJECTED"

    def __init__(self, graph_id: str) -> None:
        super().__init__(f"Tool effect graph fencing token was rejected: {graph_id}")
        self.graph_id = graph_id


class EffectReadySetProofRejectedError(RuntimeError):
    code = "EFFECT_READY_SET_PROOF_REJECTED"

    def __init__(self, graph_id: str) -> None:
        super().__init__(f"Tool effect DAG ready-set proof was rejected: {graph_id}")
        self.graph_id = graph_id


class EffectDagAdmissionProofRejectedError(RuntimeError):
    code = "EFFECT_DAG_ADMISSION_PROOF_REJECTED"

    def __init__(self, graph_id: str) -> None:
        super().__init__(f"Tool effect DAG admission proof was rejected: {graph_id}")
        self.graph_id = graph_id


class EffectBranchDecisionConflictError(RuntimeError):
    code = "EFFECT_BRANCH_DECISION_CONFLICT"

    def __init__(self, source_node_id: str, decision_key: str) -> None:
        super().__init__(
            f"Tool effect DAG branch decision is immutable: {source_node_id}/{decision_key}"
        )
        self.source_node_id = source_node_id
        self.decision_key = decision_key


class EffectBranchDecisionProofRejectedError(RuntimeError):
    code = "EFFECT_BRANCH_DECISION_PROOF_REJECTED"

    def __init__(self, decision_id: str) -> None:
        super().__init__(f"Tool effect DAG branch decision proof was rejected: {decision_id}")
        self.decision_id = decision_id


class EffectNodeFenceRejectedError(RuntimeError):
    code = "EFFECT_NODE_FENCE_REJECTED"

    def __init__(self, node_id: str) -> None:
        super().__init__(f"Tool effect node fencing token was rejected: {node_id}")
        self.node_id = node_id


class EffectGraphCancelRequestedError(RuntimeError):
    code = "EFFECT_GRAPH_CANCEL_REQUESTED"

    def __init__(self, graph_id: str) -> None:
        super().__init__(f"Tool effect graph cancellation was already requested: {graph_id}")
        self.graph_id = graph_id


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
        super().__init__(f"Reconciliation {reconciliation_id} does not prove that retry is safe")
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
        super().__init__(f"Reconciliation {reconciliation_id} cannot create a compensation task")
        self.reconciliation_id = reconciliation_id
        self.reason_code = reason_code


class ReconciliationCompensationAlreadyCreatedError(ValueError):
    code = "RECONCILIATION_COMPENSATION_ALREADY_CREATED"

    def __init__(self, reconciliation_id: str, task_id: str | None) -> None:
        super().__init__(f"Reconciliation {reconciliation_id} already has a compensation task")
        self.reconciliation_id = reconciliation_id
        self.task_id = task_id


class ReconciliationIdempotencyConflictError(ValueError):
    code = "IDEMPOTENCY_KEY_REUSED"


class ReconciliationGraphRecoveryNotAllowedError(ValueError):
    code = "RECONCILIATION_GRAPH_RECOVERY_NOT_ALLOWED"

    def __init__(self, reconciliation_id: str, reason_code: str) -> None:
        super().__init__(f"Reconciliation cannot recover its graph: {reconciliation_id}")
        self.reconciliation_id = reconciliation_id
        self.reason_code = reason_code


class ReconciliationGraphRecoveryAlreadyAppliedError(ValueError):
    code = "RECONCILIATION_GRAPH_RECOVERY_ALREADY_APPLIED"

    def __init__(self, reconciliation_id: str, action: GraphRecoveryAction) -> None:
        super().__init__(f"Reconciliation graph recovery was already applied: {reconciliation_id}")
        self.reconciliation_id = reconciliation_id
        self.action = action


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
            TaskStatus.WAITING_RECONCILIATION,
            TaskStatus.CANCELLED,
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
        }
    ),
    TaskStatus.WAITING_APPROVAL: frozenset(
        {TaskStatus.RUNNING, TaskStatus.CANCELLED, TaskStatus.FAILED}
    ),
    TaskStatus.WAITING_RECONCILIATION: frozenset(
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
        self._reconciliation_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._tool_idempotency_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    @staticmethod
    async def _retry_idempotency_race(
        command: Callable[[], Awaitable[IDEMPOTENT_COMMAND_RESULT]],
    ) -> IDEMPOTENT_COMMAND_RESULT:
        """Retry serialization/unique races so durable receipts decide the result."""
        for attempt in range(3):
            try:
                return await command()
            except (IntegrityError, OperationalError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise RuntimeError("Idempotency race retry was exhausted")

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
                        previous = self._checkpoint_codec.decode(
                            task_id=record.task_id,
                            scheme=record.protection_scheme,
                            payload=record.protected_payload,
                        )
                        graph_advanced = (
                            checkpoint.graph_id is not None
                            and checkpoint.graph_id == previous.graph_id
                            and (
                                checkpoint.current_node_id != previous.current_node_id
                                or checkpoint.execution_mode is not previous.execution_mode
                            )
                        )
                        if not graph_advanced:
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
                logger.exception(
                    "Rejected protected task checkpoint %s",
                    record.task_id,
                )
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

    async def create_effect_graph(
        self,
        task_id: str,
        definitions: tuple[EffectNodeDefinition, ...],
    ) -> EffectGraphRead:
        """Persist one trusted graph version and its public creation event atomically."""
        if not definitions:
            raise ValueError("A Tool effect graph requires at least one node")
        graph_identity = effect_graph_id(task_id)
        timestamp = utc_now()
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    existing = await session.scalar(
                        select(ToolEffectGraphRecord).where(
                            ToolEffectGraphRecord.task_id == task_id
                        )
                    )
                    if existing is not None:
                        return await self._to_effect_graph(session, existing)
                    node_identities = tuple(
                        effect_node_id(graph_identity, definition.node_key)
                        for definition in definitions
                    )
                    event = await self._append_event_record(
                        session,
                        task,
                        "effect_graph.created",
                        {
                            "graph_id": graph_identity,
                            "schema_version": EFFECT_GRAPH_SCHEMA_VERSION,
                            "nodes": [
                                {
                                    "node_id": node_identity,
                                    "node_key": definition.node_key,
                                    "ordinal": ordinal,
                                    "step_id": definition.step_id,
                                    "tool": definition.tool_name,
                                    "tool_version": definition.tool_version,
                                    "contract_digest": definition.contract_digest,
                                    "compensation_strategy": (
                                        definition.compensation_strategy.value
                                    ),
                                }
                                for ordinal, (node_identity, definition) in enumerate(
                                    zip(node_identities, definitions, strict=True)
                                )
                            ],
                        },
                        new_status=None,
                    )
                    graph = ToolEffectGraphRecord(
                        graph_id=graph_identity,
                        task_id=task_id,
                        schema_version=EFFECT_GRAPH_SCHEMA_VERSION,
                        status=EffectGraphStatus.ACTIVE.value,
                        execution_mode=EffectExecutionMode.FORWARD.value,
                        current_node_id=node_identities[0],
                        failure_node_id=None,
                        revision=1,
                        last_event_seq=event.seq,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(graph)
                    await session.flush()
                    for ordinal, (node_identity, definition) in enumerate(
                        zip(node_identities, definitions, strict=True)
                    ):
                        session.add(
                            ToolEffectNodeRecord(
                                node_id=node_identity,
                                graph_id=graph_identity,
                                node_key=definition.node_key,
                                ordinal=ordinal,
                                step_id=definition.step_id,
                                tool_name=definition.tool_name,
                                tool_version=definition.tool_version,
                                contract_digest=definition.contract_digest,
                                compensation_strategy=(definition.compensation_strategy.value),
                                status=EffectNodeStatus.PENDING.value,
                                revision=1,
                                last_event_seq=event.seq,
                                created_at=timestamp,
                                updated_at=timestamp,
                            )
                        )
                    await session.flush()
                    for ordinal in range(1, len(node_identities)):
                        previous = node_identities[ordinal - 1]
                        current = node_identities[ordinal]
                        for from_node, to_node, kind in (
                            (previous, current, EffectEdgeKind.SUCCESS),
                            (current, previous, EffectEdgeKind.COMPENSATION_ORDER),
                        ):
                            session.add(
                                ToolEffectEdgeRecord(
                                    edge_id=effect_edge_id(
                                        graph_identity,
                                        from_node,
                                        to_node,
                                        kind,
                                    ),
                                    graph_id=graph_identity,
                                    from_node_id=from_node,
                                    to_node_id=to_node,
                                    kind=kind.value,
                                )
                            )
                    await session.flush()
                    snapshot = await self._to_effect_graph(session, graph)
        self._notify_outbox()
        return snapshot

    async def get_effect_graph(self, task_id: str) -> EffectGraphRead:
        async with self._database.session() as session:
            task_exists = await session.get(TaskRecord, task_id)
            if task_exists is None:
                raise TaskNotFoundError(task_id)
            graph = await session.scalar(
                select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
            )
            if graph is None:
                raise EffectGraphNotFoundError(task_id)
            return await self._to_effect_graph(session, graph)

    async def create_effect_dag(
        self,
        task_id: str,
        definitions: tuple[EffectDagNodeDefinition, ...],
    ) -> EffectGraphRead:
        """Persist one immutable v2 DAG after proving its dependency set is acyclic."""
        self._validate_effect_dag(definitions)
        graph_identity = effect_graph_id(task_id, EFFECT_DAG_SCHEMA_VERSION)
        timestamp = utc_now()
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    existing = await session.scalar(
                        select(ToolEffectGraphRecord).where(
                            ToolEffectGraphRecord.task_id == task_id
                        )
                    )
                    if existing is not None:
                        if existing.schema_version != EFFECT_DAG_SCHEMA_VERSION:
                            raise InvalidEffectTransitionError(
                                "Task already owns another Tool effect graph version"
                            )
                        return await self._to_effect_graph(session, existing)
                    node_ids = {
                        definition.node_key: effect_node_id(graph_identity, definition.node_key)
                        for definition in definitions
                    }
                    event = await self._append_event_record(
                        session,
                        task,
                        "effect_graph.created",
                        {
                            "graph_id": graph_identity,
                            "schema_version": EFFECT_DAG_SCHEMA_VERSION,
                            "nodes": [
                                {
                                    "node_id": node_ids[definition.node_key],
                                    "node_key": definition.node_key,
                                    "ordinal": ordinal,
                                    "step_id": definition.step_id,
                                    "tool": definition.tool_name,
                                    "tool_version": definition.tool_version,
                                    "contract_digest": definition.contract_digest,
                                    "compensation_strategy": (
                                        definition.compensation_strategy.value
                                    ),
                                    "depends_on": list(definition.depends_on),
                                    "conditional_depends_on": [
                                        condition.model_dump(mode="json")
                                        for condition in (definition.conditional_depends_on)
                                    ],
                                }
                                for ordinal, definition in enumerate(definitions)
                            ],
                        },
                        new_status=None,
                    )
                    graph = ToolEffectGraphRecord(
                        graph_id=graph_identity,
                        task_id=task_id,
                        schema_version=EFFECT_DAG_SCHEMA_VERSION,
                        status=EffectGraphStatus.ACTIVE.value,
                        execution_mode=EffectExecutionMode.FORWARD.value,
                        current_node_id=None,
                        failure_node_id=None,
                        revision=1,
                        last_event_seq=event.seq,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(graph)
                    await session.flush()
                    for ordinal, definition in enumerate(definitions):
                        session.add(
                            ToolEffectNodeRecord(
                                node_id=node_ids[definition.node_key],
                                graph_id=graph_identity,
                                node_key=definition.node_key,
                                ordinal=ordinal,
                                step_id=definition.step_id,
                                tool_name=definition.tool_name,
                                tool_version=definition.tool_version,
                                contract_digest=definition.contract_digest,
                                compensation_strategy=(definition.compensation_strategy.value),
                                status=EffectNodeStatus.PENDING.value,
                                revision=1,
                                last_event_seq=event.seq,
                                created_at=timestamp,
                                updated_at=timestamp,
                            )
                        )
                    await session.flush()
                    for definition in definitions:
                        current = node_ids[definition.node_key]
                        for dependency_key in definition.depends_on:
                            predecessor = node_ids[dependency_key]
                            for from_node, to_node, kind in (
                                (predecessor, current, EffectEdgeKind.SUCCESS),
                                (
                                    current,
                                    predecessor,
                                    EffectEdgeKind.COMPENSATION_ORDER,
                                ),
                            ):
                                session.add(
                                    ToolEffectEdgeRecord(
                                        edge_id=effect_edge_id(
                                            graph_identity,
                                            from_node,
                                            to_node,
                                            kind,
                                        ),
                                        graph_id=graph_identity,
                                        from_node_id=from_node,
                                        to_node_id=to_node,
                                        kind=kind.value,
                                    )
                                )
                        for condition in definition.conditional_depends_on:
                            predecessor = node_ids[condition.predecessor_key]
                            session.add(
                                ToolEffectEdgeRecord(
                                    edge_id=effect_edge_id(
                                        graph_identity,
                                        predecessor,
                                        current,
                                        EffectEdgeKind.CONDITIONAL,
                                        condition.decision_key,
                                        condition.expected_outcome,
                                    ),
                                    graph_id=graph_identity,
                                    from_node_id=predecessor,
                                    to_node_id=current,
                                    kind=EffectEdgeKind.CONDITIONAL.value,
                                    decision_key=condition.decision_key,
                                    expected_outcome=condition.expected_outcome,
                                )
                            )
                            session.add(
                                ToolEffectEdgeRecord(
                                    edge_id=effect_edge_id(
                                        graph_identity,
                                        current,
                                        predecessor,
                                        EffectEdgeKind.COMPENSATION_ORDER,
                                    ),
                                    graph_id=graph_identity,
                                    from_node_id=current,
                                    to_node_id=predecessor,
                                    kind=EffectEdgeKind.COMPENSATION_ORDER.value,
                                )
                            )
                    await session.flush()
                    await self._rebuild_effect_dag_ready_projection(
                        session,
                        graph=graph,
                    )
                    snapshot = await self._to_effect_graph(session, graph)
        self._notify_outbox()
        return snapshot

    async def record_effect_dag_branch_decision(
        self,
        task_id: str,
        source_node_id: str,
        *,
        decision_key: str,
        outcome: str,
        evidence_digest: str,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectBranchDecisionRead:
        """Persist one immutable, evidence-bound decision for conditional edges."""
        if BRANCH_TOKEN_PATTERN.fullmatch(decision_key) is None:
            raise ValueError("Effect DAG branch decision key is invalid")
        if BRANCH_TOKEN_PATTERN.fullmatch(outcome) is None:
            raise ValueError("Effect DAG branch outcome is invalid")
        if len(evidence_digest) != 64 or any(
            character not in "0123456789abcdef" for character in evidence_digest
        ):
            raise ValueError("Effect DAG branch evidence digest is invalid")
        notify = False
        result: EffectBranchDecisionRead
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    nodes, edges = await self._load_effect_nodes_and_edges(session, graph.graph_id)
                    nodes_by_id = {node.node_id: node for node in nodes}
                    source = nodes_by_id.get(source_node_id)
                    if source is None:
                        raise InvalidEffectTransitionError(
                            "Effect DAG branch source does not belong to the graph"
                        )
                    conditional_edges = tuple(
                        edge
                        for edge in edges
                        if edge.kind == EffectEdgeKind.CONDITIONAL.value
                        and edge.from_node_id == source_node_id
                        and edge.decision_key == decision_key
                    )
                    if not conditional_edges:
                        raise InvalidEffectTransitionError(
                            "Effect DAG branch decision has no conditional edges"
                        )
                    if outcome not in {edge.expected_outcome for edge in conditional_edges}:
                        raise InvalidEffectTransitionError(
                            "Effect DAG branch outcome is not declared by the graph"
                        )
                    existing = await session.scalar(
                        select(ToolEffectBranchDecisionRecord).where(
                            ToolEffectBranchDecisionRecord.graph_id == graph.graph_id,
                            ToolEffectBranchDecisionRecord.source_node_id == source_node_id,
                            ToolEffectBranchDecisionRecord.decision_key == decision_key,
                        )
                    )
                    if existing is not None:
                        if (
                            existing.outcome != outcome
                            or existing.evidence_digest != evidence_digest
                        ):
                            raise EffectBranchDecisionConflictError(source_node_id, decision_key)
                        result = self._to_effect_branch_decision(
                            existing,
                            source=source,
                            edges=edges,
                        )
                    else:
                        if (
                            EffectGraphStatus(graph.status) is not EffectGraphStatus.ACTIVE
                            or graph.cancel_requested_at is not None
                            or EffectNodeStatus(source.status) is not EffectNodeStatus.SUCCEEDED
                        ):
                            raise InvalidEffectTransitionError(
                                "Effect DAG branch source must be succeeded in an active graph"
                            )
                        proof_digest = self._effect_branch_decision_proof_digest(
                            graph_id=graph.graph_id,
                            source=source,
                            decision_key=decision_key,
                            outcome=outcome,
                            evidence_digest=evidence_digest,
                            source_node_revision=source.revision,
                            source_event_seq=source.last_event_seq,
                            edges=edges,
                        )
                        decision_id = effect_branch_decision_id(proof_digest)
                        event = await self._append_event_record(
                            session,
                            task,
                            "effect.branch.decided",
                            {
                                "graph_id": graph.graph_id,
                                "decision_id": decision_id,
                                "source_node_id": source_node_id,
                                "source_node_key": source.node_key,
                                "decision_key": decision_key,
                                "outcome": outcome,
                                "evidence_digest": evidence_digest,
                                "proof_digest": proof_digest,
                            },
                            new_status=None,
                        )
                        database_now = await database_utc_now(session)
                        previous_graph_event_seq = graph.last_event_seq
                        revision = graph.revision
                        graph_result = await session.execute(
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.revision == revision,
                                ToolEffectGraphRecord.lease_owner_id == lease_owner_id,
                                ToolEffectGraphRecord.fencing_token == fencing_token,
                                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                            )
                            .values(
                                revision=revision + 1,
                                last_event_seq=event.seq,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if int(getattr(graph_result, "rowcount", 0)) != 1:
                            raise EffectGraphFenceRejectedError(graph.graph_id)
                        graph.revision = revision + 1
                        graph.last_event_seq = event.seq
                        record = ToolEffectBranchDecisionRecord(
                            decision_id=decision_id,
                            graph_id=graph.graph_id,
                            source_node_id=source_node_id,
                            decision_key=decision_key,
                            outcome=outcome,
                            evidence_digest=evidence_digest,
                            source_node_revision=source.revision,
                            source_event_seq=source.last_event_seq,
                            proof_digest=proof_digest,
                            event_id=event.event_id,
                            event_seq=event.seq,
                            created_at=database_now,
                        )
                        session.add(record)
                        await session.flush()
                        await self._apply_effect_dag_ready_branch_decision(
                            session,
                            graph=graph,
                            decision=record,
                            event=event,
                            expected_event_seq=previous_graph_event_seq,
                        )
                        result = self._to_effect_branch_decision(
                            record,
                            source=source,
                            edges=edges,
                        )
                        notify = True
        if notify:
            self._notify_outbox()
        return result

    async def checkpoint_effect_dag_ready_set(
        self,
        task_id: str,
        *,
        lease_owner_id: str,
        fencing_token: int,
        page_size: int = 100,
        cursor: str | None = None,
    ) -> EffectReadySetCheckpointRead:
        """Persist one bounded page from the incremental database ready projection."""
        if not 1 <= page_size <= 1_000:
            raise ValueError("Effect DAG ready-set page size is invalid")
        if cursor is not None and (not cursor.startswith("ter_") or len(cursor) != 68):
            raise ValueError("Effect DAG ready-set cursor is invalid")
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    database_now = await database_utc_now(session)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    projection = await self._ensure_effect_dag_ready_projection(
                        session,
                        graph=graph,
                    )
                    database_time, after_ordinal = await self._resolve_effect_ready_cursor(
                        session,
                        graph=graph,
                        projection=projection,
                        cursor=cursor,
                        database_time=database_now,
                    )
                    projection_revision = projection.revision
                    projection = await self._reconcile_expired_effect_ready_memberships(
                        session,
                        graph=graph,
                        projection=projection,
                        database_time=database_time,
                    )
                    if cursor is not None and projection.revision != projection_revision:
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    ready_page, _, total_ready, has_more = await self._read_effect_dag_ready_page(
                        session,
                        graph=graph,
                        projection=projection,
                        database_time=database_time,
                        page_size=page_size,
                        after_ordinal=after_ordinal,
                    )
                    ready_set_digest = self._effect_ready_projection_snapshot_digest(
                        graph=graph,
                        projection=projection,
                        total_ready=total_ready,
                    )
                    (
                        proof_digest,
                        last_ordinal,
                    ) = self._build_effect_ready_projection_page(
                        graph=graph,
                        projection=projection,
                        ready_page=ready_page,
                        ready_set_digest=ready_set_digest,
                        page_size=page_size,
                        cursor=cursor,
                        after_ordinal=after_ordinal,
                        total_ready=total_ready,
                        has_more=has_more,
                        database_time=database_time,
                    )
                    checkpoint_id = f"ter_{proof_digest}"
                    checkpoint = await session.get(
                        ToolEffectReadySetCheckpointRecord, checkpoint_id
                    )
                    if checkpoint is None:
                        checkpoint = ToolEffectReadySetCheckpointRecord(
                            checkpoint_id=checkpoint_id,
                            graph_id=graph.graph_id,
                            graph_revision=graph.revision,
                            event_seq=graph.last_event_seq,
                            ready_node_ids=[node.node_id for node in ready_page],
                            predecessor_proof={
                                "schema_version": "deskpilot.effect-ready-set.v6",
                                "graph_fencing_token": graph.fencing_token,
                                "projection_revision": projection.revision,
                                "projection_digest": projection.content_digest,
                                "database_time": database_time.isoformat(),
                                "ready_set_digest": ready_set_digest,
                                "cursor": cursor,
                                "page_size": page_size,
                                "after_ordinal": after_ordinal,
                                "last_ordinal": last_ordinal,
                                "total_ready": total_ready,
                                "has_more": has_more,
                                "ready_nodes": [
                                    node.model_dump(mode="json") for node in ready_page
                                ],
                            },
                            proof_digest=proof_digest,
                            created_at=database_now,
                        )
                        session.add(checkpoint)
                        await session.flush()
                    return self._to_ready_set_checkpoint(checkpoint)

    async def claim_effect_dag_nodes(
        self,
        task_id: str,
        node_ids: tuple[str, ...],
        *,
        ready_proof_digest: str,
        claim_owner_id: str,
        claim_ttl_seconds: float,
        lease_owner_id: str,
        fencing_token: int,
        admission_proofs: Mapping[str, EffectDagAdmissionProof] | None = None,
    ) -> tuple[EffectNodeClaimRead, ...]:
        """Claim a proven ready subset atomically and issue per-node fences."""
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError("Effect DAG claim requires distinct node IDs")
        if not 1 <= len(claim_owner_id) <= 96:
            raise ValueError("Effect node claim owner ID is invalid")
        if not 1 <= claim_ttl_seconds <= 3_600:
            raise ValueError("Effect node claim TTL must be between 1 and 3600 seconds")
        claimed: list[EffectNodeClaimRead] = []
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    database_now = await database_utc_now(session)
                    expires_at = database_now + timedelta(seconds=claim_ttl_seconds)
                    checkpoint = await session.scalar(
                        select(ToolEffectReadySetCheckpointRecord).where(
                            ToolEffectReadySetCheckpointRecord.graph_id == graph.graph_id,
                            ToolEffectReadySetCheckpointRecord.proof_digest == ready_proof_digest,
                        )
                    )
                    if checkpoint is None:
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    checkpoint_read = self._to_ready_set_checkpoint(checkpoint)
                    projection = await session.get(
                        ToolEffectDagReadyStateRecord,
                        graph.graph_id,
                    )
                    if (
                        projection is None
                        or projection.membership_version != 1
                        or graph.fencing_token != checkpoint_read.graph_fencing_token
                        or graph.last_event_seq != checkpoint_read.event_seq
                        or projection.event_seq != checkpoint_read.event_seq
                        or projection.revision != checkpoint_read.projection_revision
                        or projection.content_digest != checkpoint_read.projection_digest
                    ):
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    (
                        cursor_database_time,
                        cursor_after_ordinal,
                    ) = await self._resolve_effect_ready_cursor(
                        session,
                        graph=graph,
                        projection=projection,
                        cursor=checkpoint_read.cursor,
                        database_time=checkpoint_read.database_time,
                    )
                    if (
                        cursor_database_time != checkpoint_read.database_time
                        or cursor_after_ordinal != checkpoint_read.after_ordinal
                    ):
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    (
                        current_page,
                        page_nodes,
                        total_ready,
                        current_has_more,
                    ) = await self._read_effect_dag_ready_page(
                        session,
                        graph=graph,
                        projection=projection,
                        database_time=checkpoint_read.database_time,
                        page_size=checkpoint_read.page_size,
                        after_ordinal=checkpoint_read.after_ordinal,
                    )
                    current_ready_set_digest = self._effect_ready_projection_snapshot_digest(
                        graph=graph,
                        projection=projection,
                        total_ready=total_ready,
                    )
                    (
                        current_page_digest,
                        current_last_ordinal,
                    ) = self._build_effect_ready_projection_page(
                        graph=graph,
                        projection=projection,
                        ready_page=current_page,
                        ready_set_digest=current_ready_set_digest,
                        page_size=checkpoint_read.page_size,
                        cursor=checkpoint_read.cursor,
                        after_ordinal=checkpoint_read.after_ordinal,
                        total_ready=total_ready,
                        has_more=current_has_more,
                        database_time=checkpoint_read.database_time,
                    )
                    if (
                        current_page_digest != ready_proof_digest
                        or checkpoint_read.ready_set_digest != current_ready_set_digest
                        or checkpoint_read.ready_nodes != current_page
                        or checkpoint_read.has_more != current_has_more
                        or checkpoint_read.last_ordinal != current_last_ordinal
                        or checkpoint_read.total_ready != total_ready
                    ):
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    ready_by_id = {proof.node_id: proof for proof in current_page}
                    nodes_by_id = {node.node_id: node for node in page_nodes}
                    if any(node_id not in ready_by_id for node_id in node_ids):
                        raise EffectReadySetProofRejectedError(graph.graph_id)
                    await self._fence_effect_dag_admission_proofs(
                        session,
                        graph_id=graph.graph_id,
                        node_ids=node_ids,
                        claim_owner_id=claim_owner_id,
                        admission_proofs=admission_proofs,
                        database_now=database_now,
                    )
                    graph_revision = graph.revision
                    graph_result = await session.execute(
                        update(ToolEffectGraphRecord)
                        .where(
                            ToolEffectGraphRecord.graph_id == graph.graph_id,
                            ToolEffectGraphRecord.revision == graph_revision,
                            ToolEffectGraphRecord.lease_owner_id == lease_owner_id,
                            ToolEffectGraphRecord.fencing_token == fencing_token,
                            ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                        )
                        .values(
                            revision=graph_revision + 1,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(getattr(graph_result, "rowcount", 0)) != 1:
                        raise EffectGraphFenceRejectedError(graph.graph_id)
                    graph.revision = graph_revision + 1
                    postgresql_claims: dict[str, tuple[int, int]] | None = None
                    if session.bind is not None and session.bind.dialect.name == "postgresql":
                        locked_ids = tuple(
                            (
                                await session.scalars(
                                    build_postgresql_node_lock_statement(
                                        graph_id=graph.graph_id,
                                        node_ids=node_ids,
                                        database_now=database_now,
                                    )
                                )
                            ).all()
                        )
                        if set(locked_ids) != set(node_ids):
                            raise EffectNodeFenceRejectedError(node_ids[0])
                        rows = (
                            await session.execute(
                                build_postgresql_node_claim_statement(
                                    graph_id=graph.graph_id,
                                    node_ids=node_ids,
                                    owner_id=claim_owner_id,
                                    database_now=database_now,
                                    expires_at=expires_at,
                                )
                            )
                        ).mappings()
                        postgresql_claims = {
                            str(row["node_id"]): (
                                int(row["revision"]),
                                int(row["claim_fencing_token"]),
                            )
                            for row in rows
                        }
                        if set(postgresql_claims) != set(node_ids):
                            raise EffectNodeFenceRejectedError(node_ids[0])
                    for node_id in node_ids:
                        node = nodes_by_id[node_id]
                        previous_status = EffectNodeStatus(node.status)
                        if postgresql_claims is None:
                            node_result = await session.execute(
                                update(ToolEffectNodeRecord)
                                .where(
                                    ToolEffectNodeRecord.node_id == node_id,
                                    ToolEffectNodeRecord.revision == node.revision,
                                    ToolEffectNodeRecord.claim_fencing_token
                                    == node.claim_fencing_token,
                                    (
                                        ToolEffectNodeRecord.claim_owner_id.is_(None)
                                        | ToolEffectNodeRecord.claim_expires_at.is_(None)
                                        | (
                                            ToolEffectNodeRecord.claim_expires_at
                                            <= func.current_timestamp()
                                        )
                                    ),
                                    ToolEffectNodeRecord.status.in_(
                                        (
                                            EffectNodeStatus.PENDING.value,
                                            EffectNodeStatus.ACTIVE.value,
                                        )
                                    ),
                                )
                                .values(
                                    status=EffectNodeStatus.ACTIVE.value,
                                    revision=node.revision + 1,
                                    claim_owner_id=claim_owner_id,
                                    claim_acquired_at=database_now,
                                    claim_heartbeat_at=database_now,
                                    claim_expires_at=expires_at,
                                    claim_fencing_token=(node.claim_fencing_token + 1),
                                    updated_at=database_now,
                                )
                                .execution_options(synchronize_session=False)
                            )
                            if int(getattr(node_result, "rowcount", 0)) != 1:
                                raise EffectNodeFenceRejectedError(node_id)
                            new_revision = node.revision + 1
                            new_node_fence = node.claim_fencing_token + 1
                        else:
                            new_revision, new_node_fence = postgresql_claims[node_id]
                            if (
                                new_revision != node.revision + 1
                                or new_node_fence != node.claim_fencing_token + 1
                            ):
                                raise EffectNodeFenceRejectedError(node_id)
                        node.revision = new_revision
                        node.claim_owner_id = claim_owner_id
                        node.claim_acquired_at = database_now
                        node.claim_expires_at = expires_at
                        node.claim_fencing_token = new_node_fence
                        event = await self._append_event_record(
                            session,
                            task,
                            (
                                "effect.node.reclaimed"
                                if previous_status is EffectNodeStatus.ACTIVE
                                else "effect.node.claimed"
                            ),
                            {
                                "graph_id": graph.graph_id,
                                "node_id": node.node_id,
                                "node_key": node.node_key,
                                "claim_owner_id": claim_owner_id,
                                "claim_fencing_token": new_node_fence,
                                "ready_proof_digest": ready_proof_digest,
                                "claim_expires_at": expires_at.isoformat(),
                            },
                            new_status=None,
                        )
                        await self._record_effect_transition(
                            session,
                            graph=graph,
                            node=node,
                            event=event,
                            transition_kind=(
                                "node_reclaimed"
                                if previous_status is EffectNodeStatus.ACTIVE
                                else "node_claimed"
                            ),
                            target_node_status=EffectNodeStatus.ACTIVE,
                            target_graph_status=EffectGraphStatus.ACTIVE,
                            attempt_id=None,
                            bump_revisions=False,
                        )
                        claimed.append(
                            EffectNodeClaimRead(
                                graph_id=graph.graph_id,
                                node_id=node.node_id,
                                node_key=node.node_key,
                                owner_id=claim_owner_id,
                                fencing_token=new_node_fence,
                                acquired_at=database_now,
                                heartbeat_at=database_now,
                                expires_at=expires_at,
                                ready_proof_digest=ready_proof_digest,
                            )
                        )
                    await session.flush()
        self._notify_outbox()
        return tuple(claimed)

    async def transition_claimed_effect_node(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_statuses: frozenset[EffectNodeStatus],
        target_status: EffectNodeStatus,
        transition_kind: str,
        event_type: str,
        claim_owner_id: str,
        node_fencing_token: int,
        lease_owner_id: str,
        fencing_token: int,
        graph_status: EffectGraphStatus | None = None,
    ) -> None:
        """Commit a v2 node transition only for the live graph and node fences."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task, graph, node = await self._get_effect_state(session, task_id, node_id)
                    self._ensure_effect_dag(graph)
                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                        node_claim_owner_id=claim_owner_id,
                        node_claim_fencing_token=node_fencing_token,
                    )
                    current = EffectNodeStatus(node.status)
                    if current not in expected_statuses:
                        raise InvalidEffectTransitionError(
                            f"Effect node {node_id} cannot transition from {current.value}"
                        )
                    target_graph = graph_status or EffectGraphStatus(graph.status)
                    event = await self._append_event_record(
                        session,
                        task,
                        event_type,
                        {
                            "graph_id": graph.graph_id,
                            "node_id": node.node_id,
                            "node_key": node.node_key,
                            "transition": transition_kind,
                            "from": current.value,
                            "to": target_status.value,
                            "graph_status": target_graph.value,
                            "claim_owner_id": claim_owner_id,
                            "claim_fencing_token": node_fencing_token,
                        },
                        new_status=None,
                    )
                    if target_status is not EffectNodeStatus.ACTIVE:
                        node.claim_owner_id = None
                        node.claim_acquired_at = None
                        node.claim_heartbeat_at = None
                        node.claim_expires_at = None
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=event,
                        transition_kind=transition_kind,
                        target_node_status=target_status,
                        target_graph_status=target_graph,
                        attempt_id=None,
                        bump_revisions=False,
                    )
                    await session.flush()
        self._notify_outbox()

    async def renew_effect_dag_node_claim(
        self,
        task_id: str,
        node_id: str,
        *,
        claim_owner_id: str,
        node_fencing_token: int,
        claim_ttl_seconds: float,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectNodeLeaseRead:
        """Extend one live node claim using database time without changing its fence."""
        if not 1 <= claim_ttl_seconds <= 3_600:
            raise ValueError("Effect node claim TTL must be between 1 and 3600 seconds")
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    _, graph, node = await self._get_effect_state(session, task_id, node_id)
                    self._ensure_effect_dag(graph)
                    database_now = await database_utc_now(session)
                    expires_at = database_now + timedelta(seconds=claim_ttl_seconds)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    result = await session.execute(
                        update(ToolEffectNodeRecord)
                        .where(
                            ToolEffectNodeRecord.node_id == node_id,
                            ToolEffectNodeRecord.status.in_(
                                (
                                    EffectNodeStatus.ACTIVE.value,
                                    EffectNodeStatus.RUNNING.value,
                                    EffectNodeStatus.COMPENSATING.value,
                                )
                            ),
                            ToolEffectNodeRecord.claim_owner_id == claim_owner_id,
                            ToolEffectNodeRecord.claim_fencing_token == node_fencing_token,
                            ToolEffectNodeRecord.claim_expires_at > func.current_timestamp(),
                        )
                        .values(
                            claim_heartbeat_at=database_now,
                            claim_expires_at=expires_at,
                            updated_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(getattr(result, "rowcount", 0)) != 1:
                        raise EffectNodeFenceRejectedError(node_id)
                    acquired_at = node.claim_acquired_at
                    if acquired_at is None:
                        raise EffectNodeFenceRejectedError(node_id)
                    return EffectNodeLeaseRead(
                        graph_id=graph.graph_id,
                        node_id=node_id,
                        owner_id=claim_owner_id,
                        fencing_token=node_fencing_token,
                        acquired_at=self._as_utc(acquired_at),
                        heartbeat_at=database_now,
                        expires_at=expires_at,
                    )

    async def request_effect_dag_cancel(
        self,
        task_id: str,
        *,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectGraphRead:
        """Persist cancellation intent; the reducer owns node propagation."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    if graph.cancel_requested_at is None:
                        database_now = await database_utc_now(session)
                        previous_graph_event_seq = graph.last_event_seq
                        revision = graph.revision
                        result = await session.execute(
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.revision == revision,
                                ToolEffectGraphRecord.lease_owner_id == lease_owner_id,
                                ToolEffectGraphRecord.fencing_token == fencing_token,
                                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                            )
                            .values(
                                revision=revision + 1,
                                cancel_requested_at=database_now,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if int(getattr(result, "rowcount", 0)) != 1:
                            raise EffectGraphFenceRejectedError(graph.graph_id)
                        graph.revision = revision + 1
                        graph.cancel_requested_at = database_now
                        event = await self._append_event_record(
                            session,
                            task,
                            "effect_graph.cancel_requested",
                            {"graph_id": graph.graph_id},
                            new_status=None,
                        )
                        graph.last_event_seq = event.seq
                        await self._advance_effect_dag_ready_projection(
                            session,
                            graph=graph,
                            event=event,
                            expected_event_seq=previous_graph_event_seq,
                            mutation={"kind": "graph_cancel_requested"},
                        )
                        await session.flush()
                    snapshot = await self._to_effect_graph(session, graph)
        self._notify_outbox()
        return snapshot

    async def reduce_effect_dag(
        self,
        task_id: str,
        *,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectGraphRead:
        """Propagate skip/cancel and derive the graph terminal state transactionally."""
        notify = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    nodes, edges = await self._load_effect_nodes_and_edges(session, graph.graph_id)
                    decisions = await self._load_effect_branch_decisions(session, graph.graph_id)
                    predecessors: defaultdict[str, list[ToolEffectNodeRecord]] = defaultdict(list)
                    conditional_edges: defaultdict[str, list[ToolEffectEdgeRecord]] = defaultdict(
                        list
                    )
                    nodes_by_id = {node.node_id: node for node in nodes}
                    for edge in edges:
                        if edge.kind in {
                            EffectEdgeKind.SUCCESS.value,
                            EffectEdgeKind.CONDITIONAL.value,
                        }:
                            predecessors[edge.to_node_id].append(nodes_by_id[edge.from_node_id])
                        if edge.kind == EffectEdgeKind.CONDITIONAL.value:
                            conditional_edges[edge.to_node_id].append(edge)
                    decisions_by_key: dict[tuple[str, str], EffectBranchDecisionRead] = {}
                    for decision_record in decisions:
                        source = nodes_by_id.get(decision_record.source_node_id)
                        if source is None:
                            raise EffectBranchDecisionProofRejectedError(
                                decision_record.decision_id
                            )
                        decisions_by_key[
                            (
                                decision_record.source_node_id,
                                decision_record.decision_key,
                            )
                        ] = self._to_effect_branch_decision(
                            decision_record,
                            source=source,
                            edges=edges,
                        )

                    propagation_sources = {
                        EffectNodeStatus.FAILED,
                        EffectNodeStatus.UNKNOWN,
                        EffectNodeStatus.SKIPPED,
                        EffectNodeStatus.CANCELLED,
                        EffectNodeStatus.COMPENSATION_FAILED,
                        EffectNodeStatus.COMPENSATION_UNKNOWN,
                    }
                    changed = True
                    while changed:
                        changed = False
                        for node in nodes:
                            if EffectNodeStatus(node.status) is not EffectNodeStatus.PENDING:
                                continue
                            blocked = any(
                                EffectNodeStatus(predecessor.status) in propagation_sources
                                for predecessor in predecessors[node.node_id]
                            )
                            branch_not_selected = False
                            for edge in conditional_edges[node.node_id]:
                                if edge.decision_key is None:
                                    raise EffectBranchDecisionProofRejectedError(edge.edge_id)
                                selected_decision = decisions_by_key.get(
                                    (edge.from_node_id, edge.decision_key)
                                )
                                if (
                                    selected_decision is not None
                                    and selected_decision.outcome != edge.expected_outcome
                                ):
                                    branch_not_selected = True
                                    break
                            blocked = blocked or branch_not_selected
                            if not blocked:
                                if graph.cancel_requested_at is None:
                                    continue
                                if any(
                                    EffectNodeStatus(predecessor.status)
                                    is not EffectNodeStatus.SUCCEEDED
                                    for predecessor in predecessors[node.node_id]
                                ):
                                    continue
                            target = (
                                EffectNodeStatus.SKIPPED if blocked else EffectNodeStatus.CANCELLED
                            )
                            await self._fence_effect_mutation(
                                session,
                                graph=graph,
                                node=node,
                                owner_id=lease_owner_id,
                                fencing_token=fencing_token,
                            )
                            event = await self._append_event_record(
                                session,
                                task,
                                (
                                    "effect.node.skipped"
                                    if target is EffectNodeStatus.SKIPPED
                                    else "effect.node.cancelled"
                                ),
                                {
                                    "graph_id": graph.graph_id,
                                    "node_id": node.node_id,
                                    "node_key": node.node_key,
                                    "reason": (
                                        "branch_not_selected"
                                        if branch_not_selected
                                        else "predecessor_not_succeeded"
                                        if blocked
                                        else "graph_cancel_requested"
                                    ),
                                },
                                new_status=None,
                            )
                            await self._record_effect_transition(
                                session,
                                graph=graph,
                                node=node,
                                event=event,
                                transition_kind=(
                                    "dependency_skipped"
                                    if target is EffectNodeStatus.SKIPPED
                                    else "graph_cancelled"
                                ),
                                target_node_status=target,
                                target_graph_status=EffectGraphStatus(graph.status),
                                attempt_id=None,
                                bump_revisions=False,
                            )
                            notify = True
                            changed = True

                    statuses = tuple(EffectNodeStatus(node.status) for node in nodes)
                    in_flight = {
                        EffectNodeStatus.PENDING,
                        EffectNodeStatus.ACTIVE,
                        EffectNodeStatus.WAITING_APPROVAL,
                        EffectNodeStatus.RUNNING,
                        EffectNodeStatus.COMPENSATING,
                    }
                    target_graph = EffectGraphStatus(graph.status)
                    if not any(status in in_flight for status in statuses):
                        if any(
                            status
                            in {
                                EffectNodeStatus.UNKNOWN,
                                EffectNodeStatus.COMPENSATION_UNKNOWN,
                            }
                            for status in statuses
                        ):
                            target_graph = EffectGraphStatus.BLOCKED_UNKNOWN
                        elif graph.cancel_requested_at is not None or any(
                            status is EffectNodeStatus.CANCELLED for status in statuses
                        ):
                            target_graph = EffectGraphStatus.CANCELLED
                        elif any(
                            status
                            in {
                                EffectNodeStatus.FAILED,
                                EffectNodeStatus.COMPENSATION_FAILED,
                            }
                            for status in statuses
                        ):
                            succeeded = tuple(
                                node
                                for node in nodes
                                if EffectNodeStatus(node.status) is EffectNodeStatus.SUCCEEDED
                            )
                            if any(
                                node.compensation_strategy == CompensationStrategy.NONE.value
                                for node in succeeded
                            ):
                                target_graph = EffectGraphStatus.BLOCKED_NON_COMPENSABLE
                            elif succeeded:
                                target_graph = EffectGraphStatus.COMPENSATING
                                graph.execution_mode = EffectExecutionMode.COMPENSATING.value
                            else:
                                target_graph = EffectGraphStatus.FAILED
                        elif all(
                            status
                            in {
                                EffectNodeStatus.SUCCEEDED,
                                EffectNodeStatus.SKIPPED,
                            }
                            for status in statuses
                        ):
                            target_graph = EffectGraphStatus.SUCCEEDED

                    if target_graph is not EffectGraphStatus(graph.status):
                        database_now = await database_utc_now(session)
                        previous_graph_event_seq = graph.last_event_seq
                        revision = graph.revision
                        result = await session.execute(
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.revision == revision,
                                ToolEffectGraphRecord.lease_owner_id == lease_owner_id,
                                ToolEffectGraphRecord.fencing_token == fencing_token,
                                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                            )
                            .values(
                                status=target_graph.value,
                                execution_mode=graph.execution_mode,
                                revision=revision + 1,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if int(getattr(result, "rowcount", 0)) != 1:
                            raise EffectGraphFenceRejectedError(graph.graph_id)
                        previous = EffectGraphStatus(graph.status)
                        graph.status = target_graph.value
                        graph.revision = revision + 1
                        event = await self._append_event_record(
                            session,
                            task,
                            "effect_graph.reduced",
                            {
                                "graph_id": graph.graph_id,
                                "from": previous.value,
                                "to": target_graph.value,
                            },
                            new_status=None,
                        )
                        graph.last_event_seq = event.seq
                        await self._advance_effect_dag_ready_projection(
                            session,
                            graph=graph,
                            event=event,
                            expected_event_seq=previous_graph_event_seq,
                            mutation={
                                "kind": "graph_reduced",
                                "from_status": previous.value,
                                "to_status": target_graph.value,
                            },
                        )
                        notify = True
                    await session.flush()
                    snapshot = await self._to_effect_graph(session, graph)
        if notify:
            self._notify_outbox()
        return snapshot

    async def plan_effect_dag_compensation(
        self,
        task_id: str,
        *,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectCompensationPlanRead:
        """Persist maximal parallel reverse-topological waves for applied DAG nodes."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    if EffectGraphStatus(graph.status) is not EffectGraphStatus.COMPENSATING:
                        raise InvalidEffectTransitionError("Effect DAG is not in compensating mode")
                    nodes, edges = await self._load_effect_nodes_and_edges(session, graph.graph_id)
                    selected = {
                        node.node_id: node
                        for node in nodes
                        if EffectNodeStatus(node.status) is EffectNodeStatus.SUCCEEDED
                        and node.compensation_strategy
                        == CompensationStrategy.RECEIPT_BOUND_REVERSE.value
                    }
                    successor_ids: defaultdict[str, set[str]] = defaultdict(set)
                    for edge in edges:
                        if (
                            edge.kind
                            in {
                                EffectEdgeKind.SUCCESS.value,
                                EffectEdgeKind.CONDITIONAL.value,
                            }
                            and edge.from_node_id in selected
                            and edge.to_node_id in selected
                        ):
                            successor_ids[edge.from_node_id].add(edge.to_node_id)
                    remaining = set(selected)
                    waves: list[EffectCompensationWaveRead] = []
                    while remaining:
                        ready = tuple(
                            sorted(
                                (
                                    node_id
                                    for node_id in remaining
                                    if not (successor_ids[node_id] & remaining)
                                ),
                                key=lambda node_id: selected[node_id].ordinal,
                            )
                        )
                        if not ready:
                            raise InvalidEffectTransitionError(
                                "Compensation dependency graph contains a cycle"
                            )
                        waves.append(EffectCompensationWaveRead(ordinal=len(waves), node_ids=ready))
                        remaining.difference_update(ready)
                    proof = {
                        "schema_version": "deskpilot.effect-compensation-plan.v1",
                        "graph_id": graph.graph_id,
                        "graph_revision": graph.revision,
                        "event_seq": graph.last_event_seq,
                        "nodes": [
                            {
                                "node_id": node.node_id,
                                "status": node.status,
                                "revision": node.revision,
                                "last_event_seq": node.last_event_seq,
                            }
                            for node in selected.values()
                        ],
                        "waves": [wave.model_dump(mode="json") for wave in waves],
                    }
                    proof_digest = sha256_digest(proof)
                    plan_id = f"tep_{proof_digest}"
                    record = await session.get(ToolEffectCompensationPlanRecord, plan_id)
                    if record is None:
                        record = ToolEffectCompensationPlanRecord(
                            plan_id=plan_id,
                            graph_id=graph.graph_id,
                            graph_revision=graph.revision,
                            event_seq=graph.last_event_seq,
                            waves=[list(wave.node_ids) for wave in waves],
                            proof_digest=proof_digest,
                            created_at=await database_utc_now(session),
                        )
                        session.add(record)
                        await session.flush()
                    return EffectCompensationPlanRead(
                        plan_id=record.plan_id,
                        graph_id=record.graph_id,
                        graph_revision=record.graph_revision,
                        event_seq=record.event_seq,
                        waves=tuple(
                            EffectCompensationWaveRead(ordinal=ordinal, node_ids=tuple(node_ids))
                            for ordinal, node_ids in enumerate(record.waves)
                        ),
                        proof_digest=record.proof_digest,
                        created_at=self._as_utc(record.created_at),
                    )

    async def get_effect_dag_compensation_plan(
        self,
        task_id: str,
        plan_id: str | None = None,
    ) -> EffectCompensationPlanRead:
        """Load one durable reverse-DAG proof without recomputing mutable graph state."""
        async with self._database.session() as session:
            graph = await self._get_effect_graph_record(session, task_id)
            self._ensure_effect_dag(graph)
            statement = select(ToolEffectCompensationPlanRecord).where(
                ToolEffectCompensationPlanRecord.graph_id == graph.graph_id
            )
            if plan_id is not None:
                statement = statement.where(ToolEffectCompensationPlanRecord.plan_id == plan_id)
            else:
                statement = statement.order_by(ToolEffectCompensationPlanRecord.created_at.desc())
            record = await session.scalar(statement)
            if record is None:
                raise InvalidEffectTransitionError("Effect DAG has no durable compensation plan")
            return self._to_compensation_plan(record)

    async def claim_effect_dag_compensation_nodes(
        self,
        task_id: str,
        node_ids: tuple[str, ...],
        *,
        plan_id: str,
        wave_ordinal: int,
        claim_owner_id: str,
        claim_ttl_seconds: float,
        lease_owner_id: str,
        fencing_token: int,
        admission_proofs: Mapping[str, EffectDagAdmissionProof] | None = None,
    ) -> tuple[EffectNodeClaimRead, ...]:
        """Claim one proven compensation wave only after every earlier barrier passed."""
        if not node_ids or len(node_ids) != len(set(node_ids)):
            raise ValueError("Compensation claim requires distinct node identities")
        if not 1 <= claim_ttl_seconds <= 3_600:
            raise ValueError("Effect node claim TTL must be between 1 and 3600 seconds")
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    if EffectGraphStatus(graph.status) is not EffectGraphStatus.COMPENSATING:
                        raise InvalidEffectTransitionError(
                            "Effect DAG is not accepting compensation claims"
                        )
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    plan_record = await session.get(ToolEffectCompensationPlanRecord, plan_id)
                    if plan_record is None or plan_record.graph_id != graph.graph_id:
                        raise InvalidEffectTransitionError(
                            "Compensation claim plan binding is invalid"
                        )
                    if wave_ordinal < 0 or wave_ordinal >= len(plan_record.waves):
                        raise InvalidEffectTransitionError("Compensation wave ordinal is invalid")
                    wave_ids = tuple(plan_record.waves[wave_ordinal])
                    if any(node_id not in wave_ids for node_id in node_ids):
                        raise InvalidEffectTransitionError(
                            "Compensation claim escaped its proven wave"
                        )
                    prior_ids = tuple(
                        node_id
                        for prior_wave in plan_record.waves[:wave_ordinal]
                        for node_id in prior_wave
                    )
                    if prior_ids:
                        prior_statuses = tuple(
                            (
                                await session.execute(
                                    select(ToolEffectNodeRecord.status).where(
                                        ToolEffectNodeRecord.node_id.in_(prior_ids)
                                    )
                                )
                            )
                            .scalars()
                            .all()
                        )
                        if len(prior_statuses) != len(prior_ids) or any(
                            status != EffectNodeStatus.COMPENSATED.value
                            for status in prior_statuses
                        ):
                            raise InvalidEffectTransitionError(
                                "Compensation wave barrier has not completed"
                            )
                    records = tuple(
                        (
                            await session.execute(
                                select(ToolEffectNodeRecord)
                                .where(ToolEffectNodeRecord.node_id.in_(node_ids))
                                .order_by(ToolEffectNodeRecord.ordinal)
                            )
                        )
                        .scalars()
                        .all()
                    )
                    if len(records) != len(node_ids):
                        raise InvalidEffectTransitionError(
                            "Compensation claim node set is incomplete"
                        )
                    database_now = await database_utc_now(session)
                    expires_at = database_now + timedelta(seconds=claim_ttl_seconds)
                    await self._fence_effect_dag_admission_proofs(
                        session,
                        graph_id=graph.graph_id,
                        node_ids=node_ids,
                        claim_owner_id=claim_owner_id,
                        admission_proofs=admission_proofs,
                        database_now=database_now,
                    )
                    claims: list[EffectNodeClaimRead] = []
                    for node in records:
                        status = EffectNodeStatus(node.status)
                        claim_expiry = (
                            self._as_utc(node.claim_expires_at)
                            if node.claim_expires_at is not None
                            else None
                        )
                        reclaimable = status is EffectNodeStatus.COMPENSATING and (
                            claim_expiry is None or claim_expiry <= database_now
                        )
                        if status is not EffectNodeStatus.SUCCEEDED and not reclaimable:
                            raise InvalidEffectTransitionError("Compensation node is not claimable")
                        await self._fence_effect_mutation(
                            session,
                            graph=graph,
                            node=node,
                            owner_id=lease_owner_id,
                            fencing_token=fencing_token,
                        )
                        node.claim_owner_id = claim_owner_id
                        node.claim_acquired_at = database_now
                        node.claim_heartbeat_at = database_now
                        node.claim_expires_at = expires_at
                        node.claim_fencing_token += 1
                        event = await self._append_event_record(
                            session,
                            task,
                            "effect.compensation.claimed",
                            {
                                "graph_id": graph.graph_id,
                                "node_id": node.node_id,
                                "node_key": node.node_key,
                                "plan_id": plan_id,
                                "wave_ordinal": wave_ordinal,
                                "claim_owner_id": claim_owner_id,
                                "claim_fencing_token": node.claim_fencing_token,
                            },
                            new_status=None,
                        )
                        await self._record_effect_transition(
                            session,
                            graph=graph,
                            node=node,
                            event=event,
                            transition_kind="compensation_claimed",
                            target_node_status=EffectNodeStatus.COMPENSATING,
                            target_graph_status=EffectGraphStatus.COMPENSATING,
                            attempt_id=None,
                            bump_revisions=False,
                        )
                        claims.append(
                            EffectNodeClaimRead(
                                graph_id=graph.graph_id,
                                node_id=node.node_id,
                                node_key=node.node_key,
                                owner_id=claim_owner_id,
                                fencing_token=node.claim_fencing_token,
                                acquired_at=database_now,
                                heartbeat_at=database_now,
                                expires_at=expires_at,
                                ready_proof_digest=plan_record.proof_digest,
                            )
                        )
                    await session.flush()
        self._notify_outbox()
        claims_by_id = {claim.node_id: claim for claim in claims}
        return tuple(claims_by_id[node_id] for node_id in node_ids)

    async def reduce_effect_dag_compensation(
        self,
        task_id: str,
        *,
        plan_id: str,
        lease_owner_id: str,
        fencing_token: int,
    ) -> EffectGraphRead:
        """Derive compensated or blocked compensation graph truth after a wave barrier."""
        notify = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    graph = await self._get_effect_graph_record(session, task_id)
                    self._ensure_effect_dag(graph)
                    await self._assert_effect_graph_lease(
                        session,
                        graph=graph,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    plan = await session.get(ToolEffectCompensationPlanRecord, plan_id)
                    if plan is None or plan.graph_id != graph.graph_id:
                        raise InvalidEffectTransitionError(
                            "Compensation reducer plan binding is invalid"
                        )
                    planned_ids = tuple(node_id for wave in plan.waves for node_id in wave)
                    nodes = tuple(
                        (
                            await session.execute(
                                select(ToolEffectNodeRecord).where(
                                    ToolEffectNodeRecord.node_id.in_(planned_ids)
                                )
                            )
                        )
                        .scalars()
                        .all()
                    )
                    statuses = {EffectNodeStatus(node.status) for node in nodes}
                    current = EffectGraphStatus(graph.status)
                    target = current
                    if EffectNodeStatus.COMPENSATION_UNKNOWN in statuses:
                        target = EffectGraphStatus.BLOCKED_COMPENSATION_UNKNOWN
                    elif EffectNodeStatus.COMPENSATION_FAILED in statuses:
                        target = EffectGraphStatus.BLOCKED_COMPENSATION_FAILED
                    elif nodes and all(
                        EffectNodeStatus(node.status) is EffectNodeStatus.COMPENSATED
                        for node in nodes
                    ):
                        target = EffectGraphStatus.COMPENSATED
                    if target is not current:
                        database_now = await database_utc_now(session)
                        previous_graph_event_seq = graph.last_event_seq
                        revision = graph.revision
                        result = await session.execute(
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.revision == revision,
                                ToolEffectGraphRecord.lease_owner_id == lease_owner_id,
                                ToolEffectGraphRecord.fencing_token == fencing_token,
                                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                            )
                            .values(
                                status=target.value,
                                revision=revision + 1,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                        if int(getattr(result, "rowcount", 0)) != 1:
                            raise EffectGraphFenceRejectedError(graph.graph_id)
                        graph.status = target.value
                        graph.revision = revision + 1
                        event = await self._append_event_record(
                            session,
                            task,
                            "effect_graph.compensation_reduced",
                            {
                                "graph_id": graph.graph_id,
                                "plan_id": plan_id,
                                "from": current.value,
                                "to": target.value,
                            },
                            new_status=None,
                        )
                        graph.last_event_seq = event.seq
                        await self._advance_effect_dag_ready_projection(
                            session,
                            graph=graph,
                            event=event,
                            expected_event_seq=previous_graph_event_seq,
                            mutation={
                                "kind": "compensation_graph_reduced",
                                "from_status": current.value,
                                "to_status": target.value,
                            },
                        )
                        notify = True
                    await session.flush()
                    snapshot = await self._to_effect_graph(session, graph)
        if notify:
            self._notify_outbox()
        return snapshot

    async def acquire_effect_graph_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
    ) -> EffectGraphLeaseRead:
        """Acquire or renew one graph owner using revision CAS and a monotonic fence."""
        if not 1 <= len(owner_id) <= 80:
            raise ValueError("Effect graph lease owner ID is invalid")
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("Effect graph lease TTL must be between 1 and 3600 seconds")
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    graph = await session.scalar(
                        select(ToolEffectGraphRecord).where(
                            ToolEffectGraphRecord.task_id == task_id
                        )
                    )
                    if graph is None:
                        raise EffectGraphNotFoundError(task_id)
                    database_now = await database_utc_now(session)
                    expires_at = database_now + timedelta(seconds=ttl_seconds)
                    current_expiry = (
                        self._as_utc(graph.lease_expires_at)
                        if graph.lease_expires_at is not None
                        else None
                    )
                    same_live_owner = (
                        graph.lease_owner_id == owner_id
                        and current_expiry is not None
                        and current_expiry > database_now
                        and graph.fencing_token > 0
                    )
                    if same_live_owner:
                        statement = (
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.lease_owner_id == owner_id,
                                ToolEffectGraphRecord.fencing_token == graph.fencing_token,
                                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                            )
                            .values(
                                lease_heartbeat_at=database_now,
                                lease_expires_at=expires_at,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                    else:
                        statement = (
                            update(ToolEffectGraphRecord)
                            .where(
                                ToolEffectGraphRecord.graph_id == graph.graph_id,
                                ToolEffectGraphRecord.revision == graph.revision,
                                (
                                    ToolEffectGraphRecord.lease_owner_id.is_(None)
                                    | ToolEffectGraphRecord.lease_expires_at.is_(None)
                                    | (
                                        ToolEffectGraphRecord.lease_expires_at
                                        <= func.current_timestamp()
                                    )
                                ),
                            )
                            .values(
                                lease_owner_id=owner_id,
                                lease_acquired_at=database_now,
                                lease_heartbeat_at=database_now,
                                lease_expires_at=expires_at,
                                fencing_token=graph.fencing_token + 1,
                                revision=graph.revision + 1,
                                updated_at=database_now,
                            )
                            .execution_options(synchronize_session=False)
                        )
                    result = await session.execute(statement)
                    if int(getattr(result, "rowcount", 0)) != 1:
                        raise EffectGraphLeaseUnavailableError(task_id)
                    await session.refresh(graph)
                    return self._to_effect_graph_lease(graph)

    async def renew_effect_graph_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        fencing_token: int,
        ttl_seconds: float,
    ) -> EffectGraphLeaseRead:
        """Renew only a still-live matching owner/fence; expired fences never revive."""
        if not 1 <= ttl_seconds <= 3_600:
            raise ValueError("Effect graph lease TTL must be between 1 and 3600 seconds")
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                expires_at = database_now + timedelta(seconds=ttl_seconds)
                graph = await session.scalar(
                    select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
                )
                if graph is None:
                    raise EffectGraphNotFoundError(task_id)
                result = await session.execute(
                    update(ToolEffectGraphRecord)
                    .where(
                        ToolEffectGraphRecord.graph_id == graph.graph_id,
                        ToolEffectGraphRecord.lease_owner_id == owner_id,
                        ToolEffectGraphRecord.fencing_token == fencing_token,
                        ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
                    )
                    .values(
                        lease_heartbeat_at=database_now,
                        lease_expires_at=expires_at,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    raise EffectGraphFenceRejectedError(graph.graph_id)
                await session.refresh(graph)
                return self._to_effect_graph_lease(graph)

    async def release_effect_graph_lease(
        self,
        task_id: str,
        *,
        owner_id: str,
        fencing_token: int,
    ) -> bool:
        """Release a matching lease without making its fence reusable."""
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                graph = await session.scalar(
                    select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
                )
                if graph is None:
                    return False
                result = await session.execute(
                    update(ToolEffectGraphRecord)
                    .where(
                        ToolEffectGraphRecord.graph_id == graph.graph_id,
                        ToolEffectGraphRecord.lease_owner_id == owner_id,
                        ToolEffectGraphRecord.fencing_token == fencing_token,
                    )
                    .values(
                        lease_owner_id=None,
                        lease_acquired_at=None,
                        lease_heartbeat_at=None,
                        lease_expires_at=None,
                        revision=ToolEffectGraphRecord.revision + 1,
                        updated_at=database_now,
                    )
                    .execution_options(synchronize_session=False)
                )
                return int(getattr(result, "rowcount", 0)) == 1

    async def bind_effect_attempt(
        self,
        task_id: str,
        node_id: str,
        *,
        attempt_id: str,
        call_id: str,
        kind: EffectAttemptKind,
        attempt: int = 1,
        lease_owner_id: str,
        fencing_token: int,
    ) -> None:
        """Bind node/attempt/call identity and journal it with a task event."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task, graph, node = await self._get_effect_state(session, task_id, node_id)
                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    call = await self._get_tool_call(session, task_id, call_id)
                    if call.step_id != node.step_id:
                        raise InvalidEffectTransitionError(
                            "Effect attempt step does not match durable Tool call"
                        )
                    existing = await session.get(ToolEffectAttemptRecord, attempt_id)
                    if existing is not None:
                        if existing.call_id != call_id or existing.node_id != node_id:
                            raise InvalidEffectTransitionError(
                                "Effect attempt identity is already bound"
                            )
                        return
                    event = await self._append_event_record(
                        session,
                        task,
                        "effect.attempt.requested",
                        {
                            "graph_id": graph.graph_id,
                            "node_id": node_id,
                            "attempt_id": attempt_id,
                            "attempt_kind": kind.value,
                            "attempt": attempt,
                            "call_id": call_id,
                        },
                        new_status=None,
                    )
                    timestamp = utc_now()
                    session.add(
                        ToolEffectAttemptRecord(
                            attempt_id=attempt_id,
                            node_id=node_id,
                            kind=kind.value,
                            attempt=attempt,
                            call_id=call_id,
                            status=EffectAttemptStatus.REQUESTED.value,
                            effect_id=None,
                            last_event_seq=event.seq,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                    )
                    await session.flush()
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=event,
                        transition_kind="attempt_requested",
                        target_node_status=EffectNodeStatus(node.status),
                        target_graph_status=EffectGraphStatus(graph.status),
                        attempt_id=attempt_id,
                        bump_revisions=False,
                    )
        self._notify_outbox()

    async def request_effect_tool_call(
        self,
        task_id: str,
        node_id: str,
        *,
        call_id: str,
        attempt_id: str,
        attempt_kind: EffectAttemptKind,
        step_id: str,
        tool_name: str,
        tool_version: str,
        contract_digest: str,
        arguments: dict[str, Any],
        idempotency: ToolIdempotency,
        idempotency_key: str | None,
        tool_attempt: int,
        risk: str | None,
        checkpoint: TaskCheckpointPayload | None,
        lease_owner_id: str,
        fencing_token: int,
    ) -> tuple[TaskEventRead, TaskEventRead]:
        return await self._retry_idempotency_race(
            lambda: self._request_effect_tool_call_once(
                task_id,
                node_id,
                call_id=call_id,
                attempt_id=attempt_id,
                attempt_kind=attempt_kind,
                step_id=step_id,
                tool_name=tool_name,
                tool_version=tool_version,
                contract_digest=contract_digest,
                arguments=arguments,
                idempotency=idempotency,
                idempotency_key=idempotency_key,
                tool_attempt=tool_attempt,
                risk=risk,
                checkpoint=checkpoint,
                lease_owner_id=lease_owner_id,
                fencing_token=fencing_token,
            )
        )

    async def _request_effect_tool_call_once(
        self,
        task_id: str,
        node_id: str,
        *,
        call_id: str,
        attempt_id: str,
        attempt_kind: EffectAttemptKind,
        step_id: str,
        tool_name: str,
        tool_version: str,
        contract_digest: str,
        arguments: dict[str, Any],
        idempotency: ToolIdempotency,
        idempotency_key: str | None,
        tool_attempt: int,
        risk: str | None,
        checkpoint: TaskCheckpointPayload | None,
        lease_owner_id: str,
        fencing_token: int,
    ) -> tuple[TaskEventRead, TaskEventRead]:
        """Commit Tool request, effect attempt, transition, and checkpoint together."""
        if tool_attempt < 1:
            raise ValueError("Tool call attempt must be at least 1")
        if idempotency is ToolIdempotency.KEY_REQUIRED and idempotency_key is None:
            raise ValueError("Tool Contract requires an idempotency key")
        arguments_digest = sha256_digest(arguments)
        key_digest = self._digest_text(idempotency_key) if idempotency_key is not None else None
        idempotency_scope = (
            f"{tool_name}\0{tool_version}\0{key_digest}"
            if idempotency is ToolIdempotency.KEY_REQUIRED
            else call_id
        )
        async with (
            self._task_locks[task_id],
            self._tool_idempotency_locks[idempotency_scope],
        ):
            async with self._database.session() as session:
                async with session.begin():
                    task, graph, node = await self._get_effect_state(
                        session,
                        task_id,
                        node_id,
                    )
                    self._ensure_task_accepts_events(task)
                    if node.step_id != step_id:
                        raise InvalidEffectTransitionError(
                            "Effect request step does not match its graph node"
                        )
                    if await session.get(ToolCallRecord, call_id) is not None:
                        raise ToolCallAlreadyExistsError(call_id)
                    if await session.get(ToolEffectAttemptRecord, attempt_id) is not None:
                        raise InvalidEffectTransitionError(
                            "Effect attempt identity is already bound"
                        )
                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    if idempotency is ToolIdempotency.KEY_REQUIRED:
                        if key_digest is None:
                            raise RuntimeError("Required idempotency digest is missing")
                        existing_receipt = await session.scalar(
                            select(ToolIdempotencyReceiptRecord).where(
                                ToolIdempotencyReceiptRecord.tool_name == tool_name,
                                ToolIdempotencyReceiptRecord.tool_version == tool_version,
                                ToolIdempotencyReceiptRecord.key_digest == key_digest,
                            )
                        )
                        if existing_receipt is not None:
                            raise ToolIdempotencyKeyAlreadyUsedError(existing_receipt.call_id)

                    timestamp = utc_now()
                    call = ToolCallRecord(
                        call_id=call_id,
                        task_id=task_id,
                        step_id=step_id,
                        attempt=tool_attempt,
                        tool_name=tool_name,
                        tool_version=tool_version,
                        contract_digest=contract_digest,
                        arguments_digest=arguments_digest,
                        idempotency=idempotency.value,
                        idempotency_key_digest=key_digest,
                        status=ToolCallStatus.REQUESTED.value,
                        requested_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(call)
                    await session.flush()
                    if idempotency is ToolIdempotency.KEY_REQUIRED:
                        if key_digest is None:
                            raise RuntimeError("Required idempotency digest is missing")
                        session.add(
                            ToolIdempotencyReceiptRecord(
                                receipt_id=f"tir_{uuid4().hex}",
                                call_id=call_id,
                                tool_name=tool_name,
                                tool_version=tool_version,
                                key_digest=key_digest,
                                arguments_digest=arguments_digest,
                                created_at=timestamp,
                            )
                        )
                    request_payload: dict[str, Any] = {
                        "step_id": step_id,
                        "call_id": call_id,
                        "tool": tool_name,
                        "tool_version": tool_version,
                        "contract_digest": contract_digest,
                        "arguments_digest": arguments_digest,
                        "idempotency": idempotency.value,
                        "attempt": tool_attempt,
                    }
                    if key_digest is not None:
                        request_payload["idempotency_key_digest"] = key_digest
                    if risk is not None:
                        request_payload["risk"] = risk
                    request_event = await self._append_event_record(
                        session,
                        task,
                        "tool.requested",
                        request_payload,
                        new_status=None,
                    )
                    effect_attempt = ToolEffectAttemptRecord(
                        attempt_id=attempt_id,
                        node_id=node_id,
                        kind=attempt_kind.value,
                        attempt=1,
                        call_id=call_id,
                        status=EffectAttemptStatus.REQUESTED.value,
                        effect_id=None,
                        last_event_seq=request_event.seq,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(effect_attempt)
                    await session.flush()
                    attempt_event = await self._append_event_record(
                        session,
                        task,
                        "effect.attempt.requested",
                        {
                            "graph_id": graph.graph_id,
                            "node_id": node_id,
                            "attempt_id": attempt_id,
                            "attempt_kind": attempt_kind.value,
                            "attempt": 1,
                            "call_id": call_id,
                        },
                        new_status=None,
                    )
                    effect_attempt.last_event_seq = attempt_event.seq
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=attempt_event,
                        transition_kind="attempt_requested",
                        target_node_status=EffectNodeStatus(node.status),
                        target_graph_status=EffectGraphStatus(graph.status),
                        attempt_id=attempt_id,
                        bump_revisions=False,
                    )
                    if checkpoint is not None:
                        await self._write_task_checkpoint(session, task, checkpoint)
                    await session.flush()
        self._notify_outbox()
        return request_event, attempt_event

    async def transition_effect_node(
        self,
        task_id: str,
        node_id: str,
        *,
        expected_statuses: frozenset[EffectNodeStatus],
        target_status: EffectNodeStatus,
        transition_kind: str,
        event_type: str,
        attempt_id: str | None = None,
        attempt_status: EffectAttemptStatus | None = None,
        graph_status: EffectGraphStatus | None = None,
        execution_mode: EffectExecutionMode | None = None,
        failure_node_id: str | None = None,
        receipt_id: str | None = None,
        create_effect: bool = False,
        lease_owner_id: str,
        fencing_token: int,
    ) -> None:
        """Atomically mutate node state, append its event, and write transition proof."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task, graph, node = await self._get_effect_state(session, task_id, node_id)
                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                    )
                    current = EffectNodeStatus(node.status)
                    if current not in expected_statuses:
                        raise InvalidEffectTransitionError(
                            f"Effect node {node_id} cannot transition from {current.value}"
                        )
                    attempt_record: ToolEffectAttemptRecord | None = None
                    if attempt_id is not None:
                        attempt_record = await session.get(ToolEffectAttemptRecord, attempt_id)
                        if attempt_record is None or attempt_record.node_id != node_id:
                            raise InvalidEffectTransitionError(
                                "Effect transition attempt binding is invalid"
                            )
                    target_graph = graph_status or EffectGraphStatus(graph.status)
                    event_payload: dict[str, Any] = {
                        "graph_id": graph.graph_id,
                        "node_id": node.node_id,
                        "node_key": node.node_key,
                        "transition": transition_kind,
                        "from": current.value,
                        "to": target_status.value,
                        "graph_status": target_graph.value,
                    }
                    if attempt_record is not None:
                        event_payload.update(
                            {
                                "attempt_id": attempt_record.attempt_id,
                                "attempt_kind": attempt_record.kind,
                                "call_id": attempt_record.call_id,
                            }
                        )
                    if receipt_id is not None:
                        event_payload["receipt_id"] = receipt_id
                    event = await self._append_event_record(
                        session,
                        task,
                        event_type,
                        event_payload,
                        new_status=None,
                    )
                    if attempt_record is not None and attempt_status is not None:
                        attempt_record.status = attempt_status.value
                        attempt_record.last_event_seq = event.seq
                        attempt_record.updated_at = utc_now()
                    if create_effect:
                        if (
                            attempt_record is None
                            or attempt_status is not EffectAttemptStatus.SUCCEEDED
                        ):
                            raise InvalidEffectTransitionError(
                                "Only a succeeded bound attempt can create an effect"
                            )
                        await self._create_effect_for_succeeded_attempt(
                            session,
                            node=node,
                            attempt=attempt_record,
                            receipt_id=receipt_id,
                        )
                    graph.current_node_id = node_id
                    if failure_node_id is not None:
                        graph.failure_node_id = failure_node_id
                    if execution_mode is not None:
                        graph.execution_mode = execution_mode.value
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=event,
                        transition_kind=transition_kind,
                        target_node_status=target_status,
                        target_graph_status=target_graph,
                        attempt_id=attempt_id,
                        bump_revisions=False,
                    )
                    await session.flush()
        self._notify_outbox()

    async def get_tool_call_status(
        self,
        task_id: str,
        call_id: str,
    ) -> ToolCallStatus:
        async with self._database.session() as session:
            call = await self._get_tool_call(session, task_id, call_id)
            return ToolCallStatus(call.status)

    async def finish_effect_tool_call(
        self,
        task_id: str,
        node_id: str,
        *,
        call_id: str,
        attempt_id: str,
        status: ToolCallStatus,
        target_status: EffectNodeStatus,
        transition_kind: str,
        event_type: str,
        attempt_status: EffectAttemptStatus,
        graph_status: EffectGraphStatus,
        execution_mode: EffectExecutionMode,
        failure_node_id: str | None,
        result: dict[str, Any] | None = None,
        error_code: str | None = None,
        retryable: bool = False,
        resolution_source: str = "runner",
        create_effect: bool = False,
        checkpoint: TaskCheckpointPayload | None = None,
        lease_owner_id: str,
        fencing_token: int,
        node_claim_owner_id: str | None = None,
        node_claim_fencing_token: int | None = None,
    ) -> tuple[TaskEventRead, ...]:
        """Commit Tool terminal truth, graph transition, and checkpoint as one command."""
        if not status.is_terminal:
            raise ValueError("Effect Tool call finish status must be terminal")
        if status is ToolCallStatus.SUCCEEDED and result is None:
            raise ValueError("A succeeded effect Tool call requires a result")
        if status is not ToolCallStatus.SUCCEEDED and result is not None:
            raise ValueError("Only a succeeded effect Tool call may persist a result")
        if (node_claim_owner_id is None) != (node_claim_fencing_token is None):
            raise ValueError("Effect node claim owner and fence must be paired")
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task, graph, node = await self._get_effect_state(
                        session,
                        task_id,
                        node_id,
                    )
                    self._ensure_task_accepts_events(task)
                    call = await self._get_tool_call(session, task_id, call_id)
                    attempt = await session.get(ToolEffectAttemptRecord, attempt_id)
                    if attempt is None or attempt.node_id != node_id or attempt.call_id != call_id:
                        raise InvalidEffectTransitionError(
                            "Effect terminal command attempt binding is invalid"
                        )
                    current_call = ToolCallStatus(call.status)
                    if current_call.is_terminal:
                        return ()
                    requested_terminal_allowed = (
                        current_call is ToolCallStatus.REQUESTED
                        and status in {ToolCallStatus.FAILED, ToolCallStatus.CANCELLED}
                    )
                    if (
                        current_call is not ToolCallStatus.RUNNING
                        and not requested_terminal_allowed
                    ):
                        raise InvalidToolCallTransitionError(
                            call_id,
                            current_call,
                            status,
                        )
                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=fencing_token,
                        node_claim_owner_id=node_claim_owner_id,
                        node_claim_fencing_token=node_claim_fencing_token,
                    )

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

                    attempt.status = attempt_status.value
                    attempt.updated_at = timestamp
                    receipt = await session.scalar(
                        select(ToolCommitReceiptRecord).where(
                            ToolCommitReceiptRecord.call_id == call_id
                        )
                    )
                    if create_effect:
                        if attempt_status is not EffectAttemptStatus.SUCCEEDED:
                            raise InvalidEffectTransitionError(
                                "Only a succeeded attempt can create an effect"
                            )
                        await self._create_effect_for_succeeded_attempt(
                            session,
                            node=node,
                            attempt=attempt,
                            receipt_id=(receipt.receipt_id if receipt is not None else None),
                        )
                    effect_event = await self._append_event_record(
                        session,
                        task,
                        event_type,
                        {
                            "graph_id": graph.graph_id,
                            "node_id": node.node_id,
                            "node_key": node.node_key,
                            "transition": transition_kind,
                            "from": node.status,
                            "to": target_status.value,
                            "graph_status": graph_status.value,
                            "attempt_id": attempt.attempt_id,
                            "attempt_kind": attempt.kind,
                            "call_id": call.call_id,
                            **({"receipt_id": receipt.receipt_id} if receipt is not None else {}),
                        },
                        new_status=None,
                    )
                    attempt.last_event_seq = effect_event.seq
                    graph.current_node_id = node_id
                    graph.failure_node_id = failure_node_id
                    graph.execution_mode = execution_mode.value
                    if node_claim_owner_id is not None:
                        node.claim_owner_id = None
                        node.claim_acquired_at = None
                        node.claim_heartbeat_at = None
                        node.claim_expires_at = None
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=effect_event,
                        transition_kind=transition_kind,
                        target_node_status=target_status,
                        target_graph_status=graph_status,
                        attempt_id=attempt_id,
                        bump_revisions=False,
                    )
                    events = [tool_event, effect_event]
                    if status is ToolCallStatus.UNKNOWN:
                        current_task_status = TaskStatus(task.status)
                        self._ensure_transition(
                            task_id,
                            current_task_status,
                            TaskStatus.WAITING_RECONCILIATION,
                        )
                        events.append(
                            await self._append_event_record(
                                session,
                                task,
                                "task.waiting_reconciliation",
                                {
                                    "from": current_task_status.value,
                                    "to": TaskStatus.WAITING_RECONCILIATION.value,
                                    "code": "TOOL_RESULT_UNKNOWN",
                                    "call_id": call.call_id,
                                    "graph_id": graph.graph_id,
                                    "node_id": node.node_id,
                                },
                                new_status=TaskStatus.WAITING_RECONCILIATION,
                            )
                        )
                    if checkpoint is not None:
                        await self._write_task_checkpoint(session, task, checkpoint)
                    await session.flush()
        self._notify_outbox()
        return tuple(events)

    async def get_commit_receipt(self, receipt_id: str) -> ToolCommitReceipt:
        async with self._database.session() as session:
            record = await session.get(ToolCommitReceiptRecord, receipt_id)
            if record is None:
                raise LookupError(f"Tool commit receipt not found: {receipt_id}")
            return self._to_commit_receipt(record)

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
            return [await self._to_reconciliation(session, record) for record in records]

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
                    commit_receipt.model_dump(mode="json") if commit_receipt is not None else None
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
                        raise RuntimeError("Reconciliation evidence requires an unknown Tool call")

                    evidence_record = await session.scalar(
                        select(ToolReconciliationEvidenceRecord).where(
                            ToolReconciliationEvidenceRecord.reconciliation_id == reconciliation_id,
                            ToolReconciliationEvidenceRecord.evidence_digest == digest,
                        )
                    )
                    if evidence_record is None:
                        timestamp = utc_now()
                        if commit_receipt is not None:
                            await self._persist_commit_receipt(
                                session,
                                call,
                                {"commit_receipt": commit_receipt.model_dump(mode="json")},
                                projected_at=timestamp,
                            )
                        evidence_record = ToolReconciliationEvidenceRecord(
                            evidence_id=f"rce_{uuid4().hex}",
                            reconciliation_id=reconciliation_id,
                            evidence_digest=digest,
                            kind=kind.value,
                            queried_runner_id=queried_runner_id,
                            receipt_id=(
                                commit_receipt.receipt_id if commit_receipt is not None else None
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
        return await self._retry_idempotency_race(
            lambda: self._resolve_reconciliation_once(
                reconciliation_id,
                outcome=outcome,
                evidence_summary=evidence_summary,
                idempotency_key=idempotency_key,
                resolved_by=resolved_by,
            )
        )

    async def _resolve_reconciliation_once(
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

    async def recover_reconciliation_graph(
        self,
        reconciliation_id: str,
        *,
        action: GraphRecoveryAction,
        idempotency_key: str,
        lease_owner_id: str,
        lease_ttl_seconds: float,
    ) -> ReconciliationGraphRecoveryRead:
        return await self._retry_idempotency_race(
            lambda: self._recover_reconciliation_graph_once(
                reconciliation_id,
                action=action,
                idempotency_key=idempotency_key,
                lease_owner_id=lease_owner_id,
                lease_ttl_seconds=lease_ttl_seconds,
            )
        )

    async def _recover_reconciliation_graph_once(
        self,
        reconciliation_id: str,
        *,
        action: GraphRecoveryAction,
        idempotency_key: str,
        lease_owner_id: str,
        lease_ttl_seconds: float,
    ) -> ReconciliationGraphRecoveryRead:
        """Apply an explicit verdict to a blocked graph without rewriting its call."""
        initial = await self.get_reconciliation(reconciliation_id)
        lease = await self.acquire_effect_graph_lease(
            initial.task_id,
            owner_id=lease_owner_id,
            ttl_seconds=lease_ttl_seconds,
        )
        key_digest = self._digest_text(idempotency_key)
        operation = "tool_reconciliation.recover_graph"
        fingerprint = sha256_digest(
            {
                "operation": operation,
                "reconciliation_id": reconciliation_id,
                "action": action.value,
            }
        )
        resumed = False
        async with (
            self._reconciliation_locks[reconciliation_id],
            self._task_locks[initial.task_id],
        ):
            async with self._database.session() as session:
                async with session.begin():
                    record = await session.get(
                        ToolReconciliationRecord,
                        reconciliation_id,
                    )
                    if record is None:
                        raise ReconciliationNotFoundError(reconciliation_id)
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
                        graph = await session.scalar(
                            select(ToolEffectGraphRecord).where(
                                ToolEffectGraphRecord.task_id == record.task_id
                            )
                        )
                        task = await session.get(TaskRecord, record.task_id)
                        if graph is None or task is None:
                            raise EffectGraphNotFoundError(record.task_id)
                        return ReconciliationGraphRecoveryRead(
                            reconciliation=await self._to_reconciliation(session, record),
                            task=self._to_task(task),
                            graph=await self._to_effect_graph(session, graph),
                            replayed=True,
                            resumed=TaskStatus(task.status) is TaskStatus.RUNNING,
                        )

                    if ReconciliationStatus(record.status) is not ReconciliationStatus.RESOLVED:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "RECONCILIATION_NOT_RESOLVED",
                        )
                    if record.outcome is None:
                        raise RuntimeError("Resolved reconciliation has no outcome")
                    outcome = ReconciliationOutcome(record.outcome)
                    recovery_status = GraphRecoveryStatus(record.graph_recovery_status)
                    if recovery_status is GraphRecoveryStatus.NOT_APPLICABLE:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "RECONCILIATION_HAS_NO_EFFECT_GRAPH",
                        )
                    if recovery_status is GraphRecoveryStatus.APPLIED:
                        if record.graph_recovery_action is None:
                            raise RuntimeError("Applied graph recovery has no action")
                        raise ReconciliationGraphRecoveryAlreadyAppliedError(
                            reconciliation_id,
                            GraphRecoveryAction(record.graph_recovery_action),
                        )
                    if action is GraphRecoveryAction.CONTINUE and outcome in {
                        ReconciliationOutcome.ACCEPTED_UNKNOWN,
                        ReconciliationOutcome.CONFIRMED_FAILED,
                    }:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            (
                                "ACCEPTED_UNKNOWN_CANNOT_CONTINUE"
                                if outcome is ReconciliationOutcome.ACCEPTED_UNKNOWN
                                else "CONFIRMED_FAILED_CANNOT_PROVE_NO_EFFECT"
                            ),
                        )

                    task = await session.get(TaskRecord, record.task_id)
                    if task is None:
                        raise TaskNotFoundError(record.task_id)
                    if TaskStatus(task.status) is not TaskStatus.WAITING_RECONCILIATION:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "TASK_NOT_WAITING_RECONCILIATION",
                        )
                    attempt = await session.scalar(
                        select(ToolEffectAttemptRecord).where(
                            ToolEffectAttemptRecord.call_id == record.call_id
                        )
                    )
                    if attempt is None:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "EFFECT_ATTEMPT_NOT_FOUND",
                        )
                    node = await session.get(ToolEffectNodeRecord, attempt.node_id)
                    if node is None:
                        raise InvalidEffectTransitionError(
                            "Reconciliation effect attempt lost its node"
                        )
                    graph = await session.get(ToolEffectGraphRecord, node.graph_id)
                    if graph is None or graph.task_id != task.task_id:
                        raise InvalidEffectTransitionError(
                            "Reconciliation effect attempt lost its graph"
                        )
                    if (
                        graph.graph_id != lease.graph_id
                        or graph.status != EffectGraphStatus.BLOCKED_UNKNOWN.value
                        or attempt.status != EffectAttemptStatus.UNKNOWN.value
                    ):
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "GRAPH_IS_NOT_BLOCKED_UNKNOWN",
                        )
                    attempt_kind = EffectAttemptKind(attempt.kind)
                    expected_node_status = (
                        EffectNodeStatus.COMPENSATION_UNKNOWN
                        if attempt_kind is EffectAttemptKind.COMPENSATION
                        else EffectNodeStatus.UNKNOWN
                    )
                    if EffectNodeStatus(node.status) is not expected_node_status:
                        raise InvalidEffectTransitionError(
                            "Reconciliation node does not match its unknown attempt"
                        )

                    checkpoint_record = await session.get(
                        TaskRuntimeCheckpointRecord,
                        task.task_id,
                    )
                    checkpoint: TaskCheckpointPayload | None = None
                    if checkpoint_record is not None and self._checkpoint_codec is not None:
                        checkpoint = self._checkpoint_codec.decode(
                            task_id=checkpoint_record.task_id,
                            scheme=checkpoint_record.protection_scheme,
                            payload=checkpoint_record.protected_payload,
                        )
                    if action is GraphRecoveryAction.CONTINUE and checkpoint is None:
                        raise ReconciliationGraphRecoveryNotAllowedError(
                            reconciliation_id,
                            "GRAPH_CHECKPOINT_UNAVAILABLE",
                        )

                    await self._fence_effect_mutation(
                        session,
                        graph=graph,
                        node=node,
                        owner_id=lease_owner_id,
                        fencing_token=lease.fencing_token,
                    )
                    timestamp = utc_now()
                    target_node_status = EffectNodeStatus(node.status)
                    target_graph_status = EffectGraphStatus.FAILED
                    event_type = "reconciliation.graph.terminated"
                    transition_kind = "reconciliation_terminated"
                    target_task_status = TaskStatus.FAILED
                    updated_checkpoint: TaskCheckpointPayload | None = None

                    if action is GraphRecoveryAction.CONTINUE:
                        if outcome is ReconciliationOutcome.CONFIRMED_SUCCEEDED:
                            receipt = await session.scalar(
                                select(ToolCommitReceiptRecord).where(
                                    ToolCommitReceiptRecord.call_id == record.call_id
                                )
                            )
                            if (
                                node.compensation_strategy != CompensationStrategy.NONE.value
                                and receipt is None
                            ):
                                raise ReconciliationGraphRecoveryNotAllowedError(
                                    reconciliation_id,
                                    "CONFIRMED_SUCCESS_REQUIRES_COMMIT_RECEIPT",
                                )
                            target_node_status = (
                                EffectNodeStatus.COMPENSATED
                                if attempt_kind is EffectAttemptKind.COMPENSATION
                                else EffectNodeStatus.SUCCEEDED
                            )
                            target_graph_status = (
                                EffectGraphStatus.COMPENSATING
                                if attempt_kind is EffectAttemptKind.COMPENSATION
                                else EffectGraphStatus.ACTIVE
                            )
                            attempt.status = EffectAttemptStatus.SUCCEEDED.value
                            await self._create_effect_for_succeeded_attempt(
                                session,
                                node=node,
                                attempt=attempt,
                                receipt_id=(receipt.receipt_id if receipt is not None else None),
                            )
                            target_task_status = TaskStatus.RUNNING
                            event_type = "reconciliation.graph.continued"
                            transition_kind = "reconciliation_confirmed_succeeded"
                            if checkpoint is None:
                                raise RuntimeError("Graph continuation lost its checkpoint")
                            updated_checkpoint = checkpoint.model_copy(
                                update={
                                    "next_stage": 7,
                                    "graph_fencing_token": lease.fencing_token,
                                    "reconciled_call_id": record.call_id,
                                    "reconciled_outcome": outcome,
                                }
                            )
                            resumed = True
                        else:
                            attempt.status = EffectAttemptStatus.FAILED.value
                            target_node_status = (
                                EffectNodeStatus.COMPENSATION_FAILED
                                if attempt_kind is EffectAttemptKind.COMPENSATION
                                else EffectNodeStatus.FAILED
                            )
                            can_compensate = (
                                attempt_kind is EffectAttemptKind.FORWARD and node.ordinal > 0
                            )
                            if can_compensate:
                                previous = await session.scalar(
                                    select(ToolEffectNodeRecord).where(
                                        ToolEffectNodeRecord.graph_id == graph.graph_id,
                                        ToolEffectNodeRecord.ordinal == node.ordinal - 1,
                                    )
                                )
                                if previous is None:
                                    raise InvalidEffectTransitionError(
                                        "Saga compensation predecessor is missing"
                                    )
                                graph.current_node_id = previous.node_id
                                graph.failure_node_id = node.node_id
                                graph.execution_mode = EffectExecutionMode.COMPENSATING.value
                                target_graph_status = EffectGraphStatus.COMPENSATING
                                target_task_status = TaskStatus.RUNNING
                                event_type = "reconciliation.graph.continued"
                                transition_kind = "reconciliation_started_compensation"
                                if checkpoint is None:
                                    raise RuntimeError("Graph continuation lost its checkpoint")
                                updated_checkpoint = checkpoint.model_copy(
                                    update={
                                        "next_stage": 3,
                                        "tool_call_id": effect_call_id(
                                            previous.node_id,
                                            EffectAttemptKind.COMPENSATION,
                                        ),
                                        "tool_arguments": None,
                                        "tool_resources": (),
                                        "expected_resource_versions": {},
                                        "tool_idempotency_key": None,
                                        "policy_request": None,
                                        "policy_decision": None,
                                        "approval_id": None,
                                        "current_node_id": previous.node_id,
                                        "current_node_index": previous.ordinal,
                                        "execution_mode": EffectExecutionMode.COMPENSATING,
                                        "failure_node_id": node.node_id,
                                        "graph_fencing_token": lease.fencing_token,
                                        "reconciled_call_id": None,
                                        "reconciled_outcome": None,
                                    }
                                )
                                resumed = True
                            else:
                                target_graph_status = EffectGraphStatus.FAILED
                                transition_kind = "reconciliation_confirmed_no_effect"

                    event = await self._append_event_record(
                        session,
                        task,
                        event_type,
                        {
                            "reconciliation_id": reconciliation_id,
                            "graph_id": graph.graph_id,
                            "node_id": node.node_id,
                            "attempt_id": attempt.attempt_id,
                            "call_id": record.call_id,
                            "outcome": outcome.value,
                            "action": action.value,
                            "resumed": resumed,
                        },
                        new_status=target_task_status,
                    )
                    attempt.last_event_seq = event.seq
                    attempt.updated_at = timestamp
                    await self._record_effect_transition(
                        session,
                        graph=graph,
                        node=node,
                        event=event,
                        transition_kind=transition_kind,
                        target_node_status=target_node_status,
                        target_graph_status=target_graph_status,
                        attempt_id=attempt.attempt_id,
                        bump_revisions=False,
                    )
                    if updated_checkpoint is not None:
                        await self._write_task_checkpoint(
                            session,
                            task,
                            updated_checkpoint,
                        )

                    record.graph_recovery_status = GraphRecoveryStatus.APPLIED.value
                    record.graph_recovery_action = action.value
                    record.graph_recovery_event_id = event.event_id
                    record.graph_recovered_at = timestamp
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
                    await session.flush()
                    reconciliation_snapshot = await self._to_reconciliation(session, record)
                    graph_snapshot = await self._to_effect_graph(session, graph)
                    task_snapshot = self._to_task(task)

        self._notify_outbox()
        return ReconciliationGraphRecoveryRead(
            reconciliation=reconciliation_snapshot,
            task=task_snapshot,
            graph=graph_snapshot,
            replayed=False,
            resumed=resumed,
        )

    async def create_reconciliation_attempt(
        self,
        reconciliation_id: str,
        *,
        idempotency_key: str,
    ) -> ReconciliationAttemptRead:
        return await self._retry_idempotency_race(
            lambda: self._create_reconciliation_attempt_once(
                reconciliation_id,
                idempotency_key=idempotency_key,
            )
        )

    async def _create_reconciliation_attempt_once(
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
        return await self._retry_idempotency_race(
            lambda: self._create_reconciliation_compensation_once(
                reconciliation_id,
                request=request,
                idempotency_key=idempotency_key,
            )
        )

    async def _create_reconciliation_compensation_once(
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
                ToolReconciliationEvidenceRecord.reconciliation_id == record.reconciliation_id,
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
            or set(approval.capabilities or []) != set(FILE_MOVE_CONTRACT.security.capabilities)
            or dict(approval.expected_resource_versions or {}) != receipt.resource_versions_before
        ):
            raise not_allowed("COMPENSATION_APPROVAL_BINDING_INVALID")

        resources = tuple(
            ApprovalResourceRead.model_validate(item) for item in (approval.resource_scope or [])
        )
        source_resources = tuple(
            item for item in resources if item.operations == (FILE_MOVE_SOURCE_CAPABILITY,)
        )
        destination_resources = tuple(
            item for item in resources if item.operations == (FILE_MOVE_DESTINATION_CAPABILITY,)
        )
        if len(resources) != 2 or len(source_resources) != 1 or len(destination_resources) != 1:
            raise not_allowed("COMPENSATION_RESOURCE_SCOPE_INVALID")
        original_source = source_resources[0]
        original_destination = destination_resources[0]
        if (
            original_source.kind != "filesystem_path"
            or original_destination.kind != "filesystem_path"
            or original_source.version != receipt.resource_versions_before["source"]
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
        fail_task_on_deny: bool = True,
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
                    if current_task_status not in {
                        TaskStatus.RUNNING,
                        TaskStatus.WAITING_APPROVAL,
                    }:
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
                        if fail_task_on_deny:
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
                        if current_task_status is TaskStatus.RUNNING:
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
                        await self._cancel_sibling_approvals(
                            session,
                            task,
                            approval_id=approval_id,
                            timestamp=timestamp,
                            reason_code="APPROVAL_BATCH_EXPIRED",
                            resolution_reason="Another approval in the batch expired",
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
                            pending_approvals = int(
                                (
                                    await session.scalar(
                                        select(func.count())
                                        .select_from(ApprovalRecord)
                                        .where(
                                            ApprovalRecord.task_id == task_id,
                                            ApprovalRecord.status == ApprovalStatus.PENDING.value,
                                            ApprovalRecord.approval_id != approval_id,
                                        )
                                    )
                                )
                                or 0
                            )
                            if pending_approvals == 0:
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
                            await self._cancel_sibling_approvals(
                                session,
                                task,
                                approval_id=approval_id,
                                timestamp=timestamp,
                                reason_code="APPROVAL_BATCH_REJECTED",
                                resolution_reason=("Another approval in the batch was rejected"),
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
        return await self._retry_idempotency_race(
            lambda: self._record_tool_requested_once(
                task_id,
                call_id=call_id,
                step_id=step_id,
                tool_name=tool_name,
                tool_version=tool_version,
                contract_digest=contract_digest,
                arguments=arguments,
                idempotency=idempotency,
                idempotency_key=idempotency_key,
                attempt=attempt,
                risk=risk,
            )
        )

    async def _record_tool_requested_once(
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
                                ToolIdempotencyReceiptRecord.key_digest == idempotency_key_digest,
                            )
                        )
                        if existing_receipt is not None:
                            raise ToolIdempotencyKeyAlreadyUsedError(existing_receipt.call_id)

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
        effect_node_id: str | None = None,
        effect_attempt_id: str | None = None,
        effect_graph_status: EffectGraphStatus | None = None,
        effect_execution_mode: EffectExecutionMode | None = None,
        effect_failure_node_id: str | None = None,
        checkpoint: TaskCheckpointPayload | None = None,
        lease_owner_id: str | None = None,
        fencing_token: int | None = None,
        node_claim_owner_id: str | None = None,
        node_claim_fencing_token: int | None = None,
    ) -> TaskEventRead:
        """Consume exact policy/approval truth at the uncertain Runner boundary."""
        if not runner_id:
            raise ValueError("Runner ID must not be empty")
        if authorization.task_id != task_id or authorization.call_id != call_id:
            raise ToolAuthorizationError(
                call_id,
                "Authorization grant belongs to another task or call",
            )
        required_effect_command_fields = (
            effect_node_id,
            effect_attempt_id,
            effect_graph_status,
            effect_execution_mode,
            lease_owner_id,
            fencing_token,
        )
        if any(value is not None for value in required_effect_command_fields) and not all(
            value is not None for value in required_effect_command_fields
        ):
            raise ValueError("Effect start transaction binding must be complete")
        if (node_claim_owner_id is None) != (node_claim_fencing_token is None):
            raise ValueError("Effect node claim owner and fence must be paired")
        if node_claim_owner_id is not None and effect_node_id is None:
            raise ValueError("Effect node claim requires an effect transaction")

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
                        effect_graph: ToolEffectGraphRecord | None = None
                        effect_node: ToolEffectNodeRecord | None = None
                        effect_attempt: ToolEffectAttemptRecord | None = None
                        if effect_node_id is not None:
                            if (
                                effect_attempt_id is None
                                or effect_graph_status is None
                                or effect_execution_mode is None
                                or lease_owner_id is None
                                or fencing_token is None
                            ):
                                raise RuntimeError("Effect start transaction lost its binding")
                            _, effect_graph, effect_node = await self._get_effect_state(
                                session,
                                task_id,
                                effect_node_id,
                            )
                            if (
                                effect_execution_mode is EffectExecutionMode.FORWARD
                                and effect_graph.cancel_requested_at is not None
                            ):
                                raise EffectGraphCancelRequestedError(effect_graph.graph_id)
                            effect_attempt = await session.get(
                                ToolEffectAttemptRecord,
                                effect_attempt_id,
                            )
                            if (
                                effect_attempt is None
                                or effect_attempt.node_id != effect_node_id
                                or effect_attempt.call_id != call_id
                            ):
                                raise InvalidEffectTransitionError(
                                    "Effect start attempt binding is invalid"
                                )
                            await self._fence_effect_mutation(
                                session,
                                graph=effect_graph,
                                node=effect_node,
                                owner_id=lease_owner_id,
                                fencing_token=fencing_token,
                                node_claim_owner_id=node_claim_owner_id,
                                node_claim_fencing_token=node_claim_fencing_token,
                            )
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
                        if (
                            effect_graph is not None
                            and effect_node is not None
                            and effect_attempt is not None
                            and effect_graph_status is not None
                            and effect_execution_mode is not None
                        ):
                            effect_event = await self._append_event_record(
                                session,
                                task,
                                "effect.attempt.started",
                                {
                                    "graph_id": effect_graph.graph_id,
                                    "node_id": effect_node.node_id,
                                    "node_key": effect_node.node_key,
                                    "transition": "runner_started",
                                    "from": effect_node.status,
                                    "to": EffectNodeStatus.RUNNING.value,
                                    "graph_status": effect_graph_status.value,
                                    "attempt_id": effect_attempt.attempt_id,
                                    "attempt_kind": effect_attempt.kind,
                                    "call_id": call.call_id,
                                },
                                new_status=None,
                            )
                            effect_attempt.status = EffectAttemptStatus.RUNNING.value
                            effect_attempt.last_event_seq = effect_event.seq
                            effect_attempt.updated_at = timestamp
                            effect_graph.current_node_id = effect_node.node_id
                            effect_graph.failure_node_id = effect_failure_node_id
                            effect_graph.execution_mode = effect_execution_mode.value
                            await self._record_effect_transition(
                                session,
                                graph=effect_graph,
                                node=effect_node,
                                event=effect_event,
                                transition_kind="runner_started",
                                target_node_status=EffectNodeStatus.RUNNING,
                                target_graph_status=effect_graph_status,
                                attempt_id=effect_attempt.attempt_id,
                                bump_revisions=False,
                            )
                            if checkpoint is not None:
                                await self._write_task_checkpoint(
                                    session,
                                    task,
                                    checkpoint,
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

                    if status is ToolCallStatus.UNKNOWN:
                        current_task_status = TaskStatus(task.status)
                        self._ensure_transition(
                            task_id,
                            current_task_status,
                            TaskStatus.WAITING_RECONCILIATION,
                        )
                        events.append(
                            await self._append_event_record(
                                session,
                                task,
                                "task.waiting_reconciliation",
                                {
                                    "from": current_task_status.value,
                                    "to": TaskStatus.WAITING_RECONCILIATION.value,
                                    "code": "TOOL_RESULT_UNKNOWN",
                                    "message": (
                                        "Tool outcome requires explicit graph-level "
                                        "reconciliation before execution can continue."
                                    ),
                                    "call_id": call.call_id,
                                    "tool_error_code": call.error_code,
                                },
                                new_status=TaskStatus.WAITING_RECONCILIATION,
                            )
                        )
                        await self._rebind_task_checkpoint(session, task)
                    elif fail_task and status in {
                        ToolCallStatus.FAILED,
                        ToolCallStatus.CANCELLED,
                    }:
                        current_task_status = TaskStatus(task.status)
                        self._ensure_transition(
                            task_id,
                            current_task_status,
                            TaskStatus.FAILED,
                        )
                        task_failure_code = {
                            ToolCallStatus.FAILED: "TOOL_CALL_FAILED",
                            ToolCallStatus.CANCELLED: "TOOL_CALL_CANCELLED",
                        }[status]
                        events.append(
                            await self._append_event_record(
                                session,
                                task,
                                "task.failed",
                                {
                                    "error_type": "ToolCallFailedError",
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
            select(ToolCommitReceiptRecord).where(ToolCommitReceiptRecord.call_id == call.call_id)
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
        excluded_task_ids: frozenset[str] = frozenset(),
        lease_owner_id: str | None = None,
        lease_ttl_seconds: float | None = None,
    ) -> ToolCallRecoveryResult:
        """Idempotently reconcile calls left non-terminal by a process restart."""
        if (lease_owner_id is None) != (lease_ttl_seconds is None):
            raise ValueError("Startup graph lease binding must be complete")
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
            if task_id in excluded_task_ids:
                continue
            async with self._database.session() as session:
                task_calls = tuple(
                    (
                        await session.scalars(
                            select(ToolCallRecord).where(
                                ToolCallRecord.task_id == task_id,
                                ToolCallRecord.status.in_(incomplete_statuses),
                            )
                        )
                    ).all()
                )
            if task_calls and all(
                ToolCallStatus(call.status) is ToolCallStatus.REQUESTED
                and call.call_id in recoverable_requested_call_ids
                for call in task_calls
            ):
                continue
            startup_lease: EffectGraphLeaseRead | None = None
            if lease_owner_id is not None and lease_ttl_seconds is not None:
                try:
                    startup_lease = await self.acquire_effect_graph_lease(
                        task_id,
                        owner_id=lease_owner_id,
                        ttl_seconds=lease_ttl_seconds,
                    )
                except EffectGraphNotFoundError:
                    pass
                except EffectGraphLeaseUnavailableError:
                    continue
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
                            effect_attempt = await session.scalar(
                                select(ToolEffectAttemptRecord).where(
                                    ToolEffectAttemptRecord.call_id == call.call_id
                                )
                            )
                            if effect_attempt is not None:
                                effect_node = await session.get(
                                    ToolEffectNodeRecord,
                                    effect_attempt.node_id,
                                )
                                if effect_node is None:
                                    raise InvalidEffectTransitionError(
                                        "Recovered effect attempt lost its node"
                                    )
                                effect_graph = await session.get(
                                    ToolEffectGraphRecord,
                                    effect_node.graph_id,
                                )
                                if effect_graph is None:
                                    raise InvalidEffectTransitionError(
                                        "Recovered effect attempt lost its graph"
                                    )
                                if lease_owner_id is not None:
                                    if startup_lease is None:
                                        raise EffectGraphFenceRejectedError(effect_graph.graph_id)
                                    await self._fence_effect_mutation(
                                        session,
                                        graph=effect_graph,
                                        node=effect_node,
                                        owner_id=lease_owner_id,
                                        fencing_token=startup_lease.fencing_token,
                                    )
                                attempt_kind = EffectAttemptKind(effect_attempt.kind)
                                effect_attempt.status = (
                                    EffectAttemptStatus.UNKNOWN.value
                                    if terminal is ToolCallStatus.UNKNOWN
                                    else EffectAttemptStatus.FAILED.value
                                )
                                effect_attempt.last_event_seq = event.seq
                                effect_attempt.updated_at = timestamp
                                effect_graph.current_node_id = effect_node.node_id
                                effect_graph.failure_node_id = effect_node.node_id
                                await self._record_effect_transition(
                                    session,
                                    graph=effect_graph,
                                    node=effect_node,
                                    event=event,
                                    transition_kind=(
                                        "startup_unknown"
                                        if terminal is ToolCallStatus.UNKNOWN
                                        else "startup_not_dispatched"
                                    ),
                                    target_node_status=(
                                        (
                                            EffectNodeStatus.COMPENSATION_UNKNOWN
                                            if attempt_kind is EffectAttemptKind.COMPENSATION
                                            else EffectNodeStatus.UNKNOWN
                                        )
                                        if terminal is ToolCallStatus.UNKNOWN
                                        else (
                                            EffectNodeStatus.COMPENSATION_FAILED
                                            if attempt_kind is EffectAttemptKind.COMPENSATION
                                            else EffectNodeStatus.FAILED
                                        )
                                    ),
                                    target_graph_status=(
                                        EffectGraphStatus.BLOCKED_UNKNOWN
                                        if terminal is ToolCallStatus.UNKNOWN
                                        else EffectGraphStatus.FAILED
                                    ),
                                    attempt_id=effect_attempt.attempt_id,
                                    bump_revisions=startup_lease is None,
                                )
                            task_events_created += 1

                        if not task_is_terminal:
                            current_task_status = TaskStatus(task.status)
                            target_task_status = (
                                TaskStatus.WAITING_RECONCILIATION
                                if task_running_unknown
                                else TaskStatus.FAILED
                            )
                            self._ensure_transition(
                                task_id,
                                current_task_status,
                                target_task_status,
                            )
                            task_error_code = (
                                "TOOL_RESULT_UNKNOWN"
                                if task_running_unknown
                                else "TOOL_CALL_INTERRUPTED_BEFORE_DISPATCH"
                            )
                            await self._append_event_record(
                                session,
                                task,
                                (
                                    "task.waiting_reconciliation"
                                    if task_running_unknown
                                    else "task.failed"
                                ),
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
                                new_status=target_task_status,
                            )
                            if task_running_unknown:
                                await self._rebind_task_checkpoint(session, task)
                            task_events_created += 1
                            if target_task_status is TaskStatus.FAILED:
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
        excluded_task_ids: frozenset[str] = frozenset(),
        lease_owner_id: str | None = None,
        lease_ttl_seconds: float | None = None,
    ) -> ApprovalRecoveryResult:
        """Fail closed when an unconsumed approval lost its runtime checkpoint."""
        if (lease_owner_id is None) != (lease_ttl_seconds is None):
            raise ValueError("Startup graph lease binding must be complete")
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
            if task_id in excluded_task_ids:
                continue
            if task_id in recoverable_task_ids:
                continue
            if lease_owner_id is not None and lease_ttl_seconds is not None:
                try:
                    await self.acquire_effect_graph_lease(
                        task_id,
                        owner_id=lease_owner_id,
                        ttl_seconds=lease_ttl_seconds,
                    )
                except EffectGraphNotFoundError:
                    pass
                except EffectGraphLeaseUnavailableError:
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
        expected_last_event_seq: int | None = None,
        control_request_event_id: str | None = None,
    ) -> TaskRead:
        event_created = False
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)

                    current = TaskStatus(task.status)
                    await self._assert_control_revision(
                        session,
                        task,
                        command=command,
                        target=target,
                        expected_last_event_seq=expected_last_event_seq,
                        control_request_event_id=control_request_event_id,
                    )
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
                        if expected_last_event_seq is not None:
                            payload["expected_last_event_seq"] = expected_last_event_seq
                        if control_request_event_id is not None:
                            payload["control_request_event_id"] = control_request_event_id
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
        expected_last_event_seq: int | None = None,
        control_request_event_id: str | None = None,
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
                    await self._assert_control_revision(
                        session,
                        task,
                        command="cancel",
                        target=TaskStatus.CANCELLED,
                        expected_last_event_seq=expected_last_event_seq,
                        control_request_event_id=control_request_event_id,
                    )
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
                    if expected_last_event_seq is not None:
                        payload["expected_last_event_seq"] = expected_last_event_seq
                    if control_request_event_id is not None:
                        payload["control_request_event_id"] = control_request_event_id
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

    async def reserve_task_control(
        self,
        task_id: str,
        *,
        command: str,
        target: TaskStatus,
        expected_last_event_seq: int,
        requested_by: str = "user",
    ) -> TaskEventRead:
        """Persist exact user control intent before stopping a live runtime."""
        async with self._task_locks[task_id]:
            async with self._database.session() as session:
                async with session.begin():
                    task = await session.get(TaskRecord, task_id)
                    if task is None:
                        raise TaskNotFoundError(task_id)
                    current = TaskStatus(task.status)
                    if task.last_event_seq != expected_last_event_seq:
                        raise TaskRevisionConflictError(
                            task_id,
                            expected_last_event_seq=expected_last_event_seq,
                            current_last_event_seq=task.last_event_seq,
                            current_status=current,
                        )
                    self._ensure_transition(task_id, current, target)
                    event = await self._append_event_record(
                        session,
                        task,
                        "task.control_requested",
                        {
                            "from": current.value,
                            "to": target.value,
                            "requested_action": command,
                            "requested_by": requested_by,
                            "expected_last_event_seq": expected_last_event_seq,
                        },
                        new_status=None,
                    )
        self._notify_outbox()
        return event

    async def _assert_control_revision(
        self,
        session: AsyncSession,
        task: TaskRecord,
        *,
        command: str,
        target: TaskStatus,
        expected_last_event_seq: int | None,
        control_request_event_id: str | None,
    ) -> None:
        current = TaskStatus(task.status)
        if expected_last_event_seq is None:
            if control_request_event_id is not None:
                raise TaskRevisionConflictError(
                    task.task_id,
                    expected_last_event_seq=task.last_event_seq,
                    current_last_event_seq=task.last_event_seq,
                    current_status=current,
                )
            return
        if control_request_event_id is None:
            if task.last_event_seq != expected_last_event_seq:
                raise TaskRevisionConflictError(
                    task.task_id,
                    expected_last_event_seq=expected_last_event_seq,
                    current_last_event_seq=task.last_event_seq,
                    current_status=current,
                )
            return

        reservation = await session.get(TaskEventRecord, control_request_event_id)
        payload = dict(reservation.payload or {}) if reservation is not None else {}
        if (
            reservation is None
            or reservation.task_id != task.task_id
            or reservation.type != "task.control_requested"
            or reservation.seq != expected_last_event_seq + 1
            or payload.get("requested_action") != command
            or payload.get("to") != target.value
            or payload.get("expected_last_event_seq") != expected_last_event_seq
        ):
            raise TaskRevisionConflictError(
                task.task_id,
                expected_last_event_seq=expected_last_event_seq,
                current_last_event_seq=task.last_event_seq,
                current_status=current,
            )

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

    async def _cancel_sibling_approvals(
        self,
        session: AsyncSession,
        task: TaskRecord,
        *,
        approval_id: str,
        timestamp: datetime,
        reason_code: str,
        resolution_reason: str,
    ) -> None:
        siblings = tuple(
            (
                await session.scalars(
                    select(ApprovalRecord)
                    .where(
                        ApprovalRecord.task_id == task.task_id,
                        ApprovalRecord.approval_id != approval_id,
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
        for sibling in siblings:
            previous = ApprovalStatus(sibling.status)
            sibling.status = ApprovalStatus.CANCELLED.value
            sibling.resolved_at = sibling.resolved_at or timestamp
            sibling.resolved_by = sibling.resolved_by or "system"
            sibling.resolution_reason = resolution_reason
            sibling.updated_at = timestamp
            await self._append_event_record(
                session,
                task,
                (
                    "approval.resolved"
                    if previous is ApprovalStatus.PENDING
                    else "approval.invalidated"
                ),
                {
                    "approval_id": sibling.approval_id,
                    "call_id": sibling.call_id,
                    "status": ApprovalStatus.CANCELLED.value,
                    "decision": sibling.decision,
                    "reason_code": reason_code,
                },
                new_status=None,
            )
            call = await self._get_tool_call(session, task.task_id, sibling.call_id)
            if ToolCallStatus(call.status) is ToolCallStatus.REQUESTED:
                await self._cancel_requested_call_for_approval(
                    session,
                    task,
                    sibling,
                    timestamp=timestamp,
                    error_code=reason_code,
                    resolution_source="policy",
                )

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
            select(ToolReconciliationRecord).where(ToolReconciliationRecord.call_id == call.call_id)
        )
        if existing is not None:
            return
        effect_attempt = await session.scalar(
            select(ToolEffectAttemptRecord).where(ToolEffectAttemptRecord.call_id == call.call_id)
        )
        session.add(
            ToolReconciliationRecord(
                reconciliation_id=f"rec_{uuid4().hex}",
                task_id=call.task_id,
                call_id=call.call_id,
                status=ReconciliationStatus.PENDING.value,
                graph_recovery_status=(
                    GraphRecoveryStatus.PENDING.value
                    if effect_attempt is not None
                    else GraphRecoveryStatus.NOT_APPLICABLE.value
                ),
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
            [await self._to_reconciliation_evidence(session, item) for item in evidence_records]
        )
        status = ReconciliationStatus(record.status)
        outcome = ReconciliationOutcome(record.outcome) if record.outcome is not None else None
        can_create_attempt = (
            status is ReconciliationStatus.RESOLVED
            and outcome is ReconciliationOutcome.CONFIRMED_NO_EFFECT
            and record.new_attempt_task_id is None
            and record.compensation_created_at is None
            and self._supports_explicit_attempt(call)
        )
        has_commit_receipt = any(
            item.kind is ReconciliationEvidenceKind.COMMIT_RECEIPT for item in receipt_evidence
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
                self._as_utc(record.resolved_at) if record.resolved_at is not None else None
            ),
            graph_recovery_status=GraphRecoveryStatus(record.graph_recovery_status),
            graph_recovery_action=(
                GraphRecoveryAction(record.graph_recovery_action)
                if record.graph_recovery_action is not None
                else None
            ),
            graph_recovery_event_id=record.graph_recovery_event_id,
            graph_recovered_at=(
                self._as_utc(record.graph_recovered_at)
                if record.graph_recovered_at is not None
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
                self._to_commit_receipt(receipt_record) if receipt_record is not None else None
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

    @staticmethod
    async def _get_effect_state(
        session: AsyncSession,
        task_id: str,
        node_id: str,
    ) -> tuple[TaskRecord, ToolEffectGraphRecord, ToolEffectNodeRecord]:
        task = await session.get(TaskRecord, task_id)
        if task is None:
            raise TaskNotFoundError(task_id)
        graph = await session.scalar(
            select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
        )
        if graph is None:
            raise EffectGraphNotFoundError(task_id)
        node = await session.get(ToolEffectNodeRecord, node_id)
        if node is None or node.graph_id != graph.graph_id:
            raise InvalidEffectTransitionError("Effect node does not belong to the task graph")
        return task, graph, node

    @staticmethod
    async def _get_effect_graph_record(
        session: AsyncSession,
        task_id: str,
    ) -> ToolEffectGraphRecord:
        graph = await session.scalar(
            select(ToolEffectGraphRecord).where(ToolEffectGraphRecord.task_id == task_id)
        )
        if graph is None:
            raise EffectGraphNotFoundError(task_id)
        return graph

    @staticmethod
    def _ensure_effect_dag(graph: ToolEffectGraphRecord) -> None:
        if graph.schema_version != EFFECT_DAG_SCHEMA_VERSION:
            raise InvalidEffectTransitionError("Operation requires a v2 Tool effect DAG")

    @staticmethod
    def _validate_effect_dag(
        definitions: tuple[EffectDagNodeDefinition, ...],
    ) -> None:
        if not 1 <= len(definitions) <= EFFECT_DAG_MAX_NODES:
            raise ValueError(
                f"A Tool effect DAG requires between 1 and {EFFECT_DAG_MAX_NODES} nodes"
            )
        keys = [definition.node_key for definition in definitions]
        if len(keys) != len(set(keys)):
            raise ValueError("Tool effect DAG node keys must be unique")
        steps = [definition.step_id for definition in definitions]
        if len(steps) != len(set(steps)):
            raise ValueError("Tool effect DAG step IDs must be unique")
        key_set = set(keys)
        indegree = {key: 0 for key in keys}
        successors: defaultdict[str, list[str]] = defaultdict(list)
        dependency_count = 0
        for definition in definitions:
            if len(definition.depends_on) != len(set(definition.depends_on)):
                raise ValueError("Tool effect DAG dependencies must be unique")
            condition_identities = [
                (condition.predecessor_key, condition.decision_key)
                for condition in definition.conditional_depends_on
            ]
            if len(condition_identities) != len(set(condition_identities)):
                raise ValueError("Tool effect DAG conditional dependencies must be unique")
            conditional_predecessors = [
                condition.predecessor_key for condition in definition.conditional_depends_on
            ]
            all_predecessors = [*definition.depends_on, *conditional_predecessors]
            if len(all_predecessors) != len(set(all_predecessors)):
                raise ValueError(
                    "Tool effect DAG predecessors cannot be both conditional and unconditional"
                )
            if len(all_predecessors) > EFFECT_DAG_MAX_PREDECESSORS:
                raise ValueError(
                    "Tool effect DAG nodes support at most "
                    f"{EFFECT_DAG_MAX_PREDECESSORS} predecessors"
                )
            dependency_count += len(all_predecessors)
            if dependency_count > EFFECT_DAG_MAX_DEPENDENCIES:
                raise ValueError(
                    f"Tool effect DAG supports at most {EFFECT_DAG_MAX_DEPENDENCIES} dependencies"
                )
            for dependency in all_predecessors:
                if dependency not in key_set:
                    raise ValueError(f"Tool effect DAG dependency does not exist: {dependency}")
                if dependency == definition.node_key:
                    raise ValueError("Tool effect DAG nodes cannot depend on themselves")
                indegree[definition.node_key] += 1
                successors[dependency].append(definition.node_key)
        ready = [key for key in keys if indegree[key] == 0]
        visited = 0
        while ready:
            current = ready.pop()
            visited += 1
            for successor in successors[current]:
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        if visited != len(definitions):
            raise ValueError("Tool effect DAG dependencies contain a cycle")

    @staticmethod
    async def _load_effect_nodes_and_edges(
        session: AsyncSession,
        graph_id: str,
    ) -> tuple[tuple[ToolEffectNodeRecord, ...], tuple[ToolEffectEdgeRecord, ...]]:
        nodes = tuple(
            (
                await session.scalars(
                    select(ToolEffectNodeRecord)
                    .where(ToolEffectNodeRecord.graph_id == graph_id)
                    .order_by(ToolEffectNodeRecord.ordinal)
                )
            ).all()
        )
        edges = tuple(
            (
                await session.scalars(
                    select(ToolEffectEdgeRecord)
                    .where(ToolEffectEdgeRecord.graph_id == graph_id)
                    .order_by(
                        ToolEffectEdgeRecord.from_node_id,
                        ToolEffectEdgeRecord.to_node_id,
                        ToolEffectEdgeRecord.kind,
                    )
                )
            ).all()
        )
        return nodes, edges

    @staticmethod
    async def _load_effect_branch_decisions(
        session: AsyncSession,
        graph_id: str,
    ) -> tuple[ToolEffectBranchDecisionRecord, ...]:
        return tuple(
            (
                await session.scalars(
                    select(ToolEffectBranchDecisionRecord)
                    .where(ToolEffectBranchDecisionRecord.graph_id == graph_id)
                    .order_by(
                        ToolEffectBranchDecisionRecord.event_seq,
                        ToolEffectBranchDecisionRecord.decision_id,
                    )
                )
            ).all()
        )

    @staticmethod
    def _effect_branch_decision_proof_digest(
        *,
        graph_id: str,
        source: ToolEffectNodeRecord,
        decision_key: str,
        outcome: str,
        evidence_digest: str,
        source_node_revision: int,
        source_event_seq: int,
        edges: tuple[ToolEffectEdgeRecord, ...],
    ) -> str:
        conditions = sorted(
            (
                {
                    "edge_id": edge.edge_id,
                    "to_node_id": edge.to_node_id,
                    "expected_outcome": edge.expected_outcome,
                }
                for edge in edges
                if edge.kind == EffectEdgeKind.CONDITIONAL.value
                and edge.from_node_id == source.node_id
                and edge.decision_key == decision_key
            ),
            key=lambda item: (str(item["to_node_id"]), str(item["edge_id"])),
        )
        return sha256_digest(
            {
                "schema_version": "deskpilot.effect-branch-decision.v1",
                "graph_id": graph_id,
                "source_node_id": source.node_id,
                "source_node_key": source.node_key,
                "source_status": EffectNodeStatus.SUCCEEDED.value,
                "source_node_revision": source_node_revision,
                "source_event_seq": source_event_seq,
                "decision_key": decision_key,
                "outcome": outcome,
                "evidence_digest": evidence_digest,
                "conditions": conditions,
            }
        )

    @classmethod
    def _to_effect_branch_decision(
        cls,
        record: ToolEffectBranchDecisionRecord,
        *,
        source: ToolEffectNodeRecord,
        edges: tuple[ToolEffectEdgeRecord, ...],
    ) -> EffectBranchDecisionRead:
        expected_digest = cls._effect_branch_decision_proof_digest(
            graph_id=record.graph_id,
            source=source,
            decision_key=record.decision_key,
            outcome=record.outcome,
            evidence_digest=record.evidence_digest,
            source_node_revision=record.source_node_revision,
            source_event_seq=record.source_event_seq,
            edges=edges,
        )
        if (
            record.proof_digest != expected_digest
            or record.decision_id != effect_branch_decision_id(expected_digest)
        ):
            raise EffectBranchDecisionProofRejectedError(record.decision_id)
        return EffectBranchDecisionRead(
            decision_id=record.decision_id,
            graph_id=record.graph_id,
            source_node_id=record.source_node_id,
            source_node_key=source.node_key,
            decision_key=record.decision_key,
            outcome=record.outcome,
            evidence_digest=record.evidence_digest,
            source_node_revision=record.source_node_revision,
            source_event_seq=record.source_event_seq,
            proof_digest=record.proof_digest,
            event_seq=record.event_seq,
            created_at=cls._as_utc(record.created_at),
        )

    @staticmethod
    def _to_effect_branch_decision_proof(
        decision: EffectBranchDecisionRead,
    ) -> EffectBranchDecisionProof:
        return EffectBranchDecisionProof(
            decision_id=decision.decision_id,
            source_node_id=decision.source_node_id,
            source_node_key=decision.source_node_key,
            decision_key=decision.decision_key,
            outcome=decision.outcome,
            evidence_digest=decision.evidence_digest,
            source_node_revision=decision.source_node_revision,
            source_event_seq=decision.source_event_seq,
            proof_digest=decision.proof_digest,
        )

    @classmethod
    def _effect_ready_projection_membership(
        cls,
        *,
        node: ToolEffectNodeRecord,
        remaining_predecessors: int,
        unresolved_branches: int,
        branch_rejected: bool,
        database_time: datetime,
    ) -> bool:
        claim_expired = (
            node.claim_owner_id is None
            or node.claim_expires_at is None
            or cls._as_utc(node.claim_expires_at) <= database_time
        )
        return (
            not branch_rejected
            and remaining_predecessors == 0
            and unresolved_branches == 0
            and EffectNodeStatus(node.status) in {EffectNodeStatus.PENDING, EffectNodeStatus.ACTIVE}
            and claim_expired
        )

    @staticmethod
    def _validate_effect_ready_projection_state(
        state: ToolEffectDagReadyStateRecord,
    ) -> None:
        if (
            state.membership_version != 1
            or state.projected_node_count < 0
            or state.ready_node_count < 0
            or state.ready_node_count > state.projected_node_count
        ):
            raise EffectReadySetProofRejectedError(state.graph_id)

    @staticmethod
    def _effect_ready_projection_node_digest(
        *,
        graph_id: str,
        node_id: str,
        ordinal: int,
        remaining_predecessors: int,
        unresolved_branches: int,
        branch_rejected: bool,
        membership_ready: bool,
        revision: int,
    ) -> str:
        return sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-projection-node.v2",
                "graph_id": graph_id,
                "node_id": node_id,
                "ordinal": ordinal,
                "remaining_predecessors": remaining_predecessors,
                "unresolved_branches": unresolved_branches,
                "branch_rejected": branch_rejected,
                "membership_ready": membership_ready,
                "revision": revision,
            }
        )

    @classmethod
    def _validate_effect_ready_projection_node(
        cls,
        record: ToolEffectDagReadyNodeRecord,
    ) -> None:
        expected = cls._effect_ready_projection_node_digest(
            graph_id=record.graph_id,
            node_id=record.node_id,
            ordinal=record.ordinal,
            remaining_predecessors=record.remaining_predecessors,
            unresolved_branches=record.unresolved_branches,
            branch_rejected=record.branch_rejected,
            membership_ready=record.membership_ready,
            revision=record.revision,
        )
        if record.proof_digest != expected:
            raise EffectReadySetProofRejectedError(record.graph_id)

    @classmethod
    async def _ensure_effect_dag_ready_projection(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
    ) -> ToolEffectDagReadyStateRecord:
        state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
        if (
            state is not None
            and state.event_seq == graph.last_event_seq
            and state.membership_version == 1
        ):
            cls._validate_effect_ready_projection_state(state)
            return state
        return await cls._rebuild_effect_dag_ready_projection(
            session,
            graph=graph,
        )

    @classmethod
    async def _rebuild_effect_dag_ready_projection(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
    ) -> ToolEffectDagReadyStateRecord:
        started_at = time.perf_counter()
        nodes, edges = await cls._load_effect_nodes_and_edges(session, graph.graph_id)
        decisions = await cls._load_effect_branch_decisions(session, graph.graph_id)
        nodes_by_id = {node.node_id: node for node in nodes}
        forward_edges: defaultdict[str, list[ToolEffectEdgeRecord]] = defaultdict(list)
        conditional_edges: defaultdict[str, list[ToolEffectEdgeRecord]] = defaultdict(list)
        for edge in edges:
            if edge.kind in {
                EffectEdgeKind.SUCCESS.value,
                EffectEdgeKind.CONDITIONAL.value,
            }:
                forward_edges[edge.to_node_id].append(edge)
            if edge.kind == EffectEdgeKind.CONDITIONAL.value:
                conditional_edges[edge.to_node_id].append(edge)
        decisions_by_key: dict[tuple[str, str], EffectBranchDecisionRead] = {}
        for record in decisions:
            source = nodes_by_id.get(record.source_node_id)
            if source is None:
                raise EffectBranchDecisionProofRejectedError(record.decision_id)
            decisions_by_key[(record.source_node_id, record.decision_key)] = (
                cls._to_effect_branch_decision(record, source=source, edges=edges)
            )
        existing_rows = {
            row.node_id: row
            for row in (
                await session.scalars(
                    select(ToolEffectDagReadyNodeRecord).where(
                        ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id
                    )
                )
            ).all()
        }
        timestamp = await database_utc_now(session)
        rebuilt: list[ToolEffectDagReadyNodeRecord] = []
        ready_node_count = 0
        for node in nodes:
            predecessors = forward_edges[node.node_id]
            remaining = sum(
                EffectNodeStatus(nodes_by_id[edge.from_node_id].status)
                is not EffectNodeStatus.SUCCEEDED
                for edge in predecessors
            )
            unresolved = 0
            rejected = False
            for edge in conditional_edges[node.node_id]:
                if edge.decision_key is None or edge.expected_outcome is None:
                    raise EffectReadySetProofRejectedError(graph.graph_id)
                decision = decisions_by_key.get((edge.from_node_id, edge.decision_key))
                if decision is None:
                    unresolved += 1
                elif decision.outcome != edge.expected_outcome:
                    rejected = True
            previous = existing_rows.get(node.node_id)
            revision = 1 if previous is None else previous.revision + 1
            membership_ready = cls._effect_ready_projection_membership(
                node=node,
                remaining_predecessors=remaining,
                unresolved_branches=unresolved,
                branch_rejected=rejected,
                database_time=timestamp,
            )
            ready_node_count += int(membership_ready)
            rebuilt.append(
                ToolEffectDagReadyNodeRecord(
                    node_id=node.node_id,
                    graph_id=graph.graph_id,
                    ordinal=node.ordinal,
                    remaining_predecessors=remaining,
                    unresolved_branches=unresolved,
                    branch_rejected=rejected,
                    membership_ready=membership_ready,
                    revision=revision,
                    proof_digest=cls._effect_ready_projection_node_digest(
                        graph_id=graph.graph_id,
                        node_id=node.node_id,
                        ordinal=node.ordinal,
                        remaining_predecessors=remaining,
                        unresolved_branches=unresolved,
                        branch_rejected=rejected,
                        membership_ready=membership_ready,
                        revision=revision,
                    ),
                    updated_at=timestamp,
                )
            )
        await session.execute(
            delete(ToolEffectDagReadyNodeRecord).where(
                ToolEffectDagReadyNodeRecord.graph_id == graph.graph_id
            )
        )
        session.add_all(rebuilt)
        state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
        state_revision = 1 if state is None else state.revision + 1
        content_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-projection-rebuild.v2",
                "graph_id": graph.graph_id,
                "event_seq": graph.last_event_seq,
                "state_revision": state_revision,
                "projected_node_count": len(rebuilt),
                "ready_node_count": ready_node_count,
                "nodes": [row.proof_digest for row in rebuilt],
            }
        )
        rebuild_duration_ms = max(0, round((time.perf_counter() - started_at) * 1_000))
        if state is None:
            state = ToolEffectDagReadyStateRecord(
                graph_id=graph.graph_id,
                revision=state_revision,
                event_seq=graph.last_event_seq,
                content_digest=content_digest,
                membership_version=1,
                projected_node_count=len(rebuilt),
                ready_node_count=ready_node_count,
                rebuild_count=1,
                last_rebuild_duration_ms=rebuild_duration_ms,
                rebuilt_at=timestamp,
                updated_at=timestamp,
            )
            session.add(state)
        else:
            state.revision = state_revision
            state.event_seq = graph.last_event_seq
            state.content_digest = content_digest
            state.membership_version = 1
            state.projected_node_count = len(rebuilt)
            state.ready_node_count = ready_node_count
            state.rebuild_count += 1
            state.last_rebuild_duration_ms = rebuild_duration_ms
            state.rebuilt_at = timestamp
            state.updated_at = timestamp
        await session.flush()
        return state

    @classmethod
    async def _advance_effect_dag_ready_projection(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        event: TaskEventRead,
        expected_event_seq: int,
        mutation: dict[str, Any],
        changed_rows: tuple[ToolEffectDagReadyNodeRecord, ...] = (),
        ready_count_delta: int = 0,
    ) -> None:
        state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
        if state is None or state.event_seq != expected_event_seq or state.membership_version != 1:
            await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
            return
        cls._validate_effect_ready_projection_state(state)
        timestamp = await database_utc_now(session)
        state.revision += 1
        state.event_seq = event.seq
        state.ready_node_count += ready_count_delta
        cls._validate_effect_ready_projection_state(state)
        state.content_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-projection-chain.v2",
                "graph_id": graph.graph_id,
                "previous_digest": state.content_digest,
                "projection_revision": state.revision,
                "event_id": event.event_id,
                "event_seq": event.seq,
                "projected_node_count": state.projected_node_count,
                "ready_node_count": state.ready_node_count,
                "mutation": mutation,
                "changed_rows": [row.proof_digest for row in changed_rows],
            }
        )
        state.updated_at = timestamp

    @classmethod
    async def _apply_effect_dag_ready_transition(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        node: ToolEffectNodeRecord,
        event: TaskEventRead,
        expected_event_seq: int,
        previous_status: EffectNodeStatus,
        target_status: EffectNodeStatus,
        transition_kind: str,
    ) -> None:
        state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
        if state is None or state.event_seq != expected_event_seq or state.membership_version != 1:
            await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
            return
        cls._validate_effect_ready_projection_state(state)
        current_projection = await session.get(
            ToolEffectDagReadyNodeRecord,
            node.node_id,
        )
        if current_projection is None or current_projection.graph_id != graph.graph_id:
            await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
            return
        projections = {node.node_id: current_projection}
        affected_nodes = {node.node_id: node}
        counter_changed_ids: set[str] = set()
        previous_succeeded = previous_status is EffectNodeStatus.SUCCEEDED
        target_succeeded = target_status is EffectNodeStatus.SUCCEEDED
        if previous_succeeded != target_succeeded:
            outgoing = tuple(
                (
                    await session.scalars(
                        select(ToolEffectEdgeRecord).where(
                            ToolEffectEdgeRecord.graph_id == graph.graph_id,
                            ToolEffectEdgeRecord.from_node_id == node.node_id,
                            ToolEffectEdgeRecord.kind.in_(
                                (
                                    EffectEdgeKind.SUCCESS.value,
                                    EffectEdgeKind.CONDITIONAL.value,
                                )
                            ),
                        )
                    )
                ).all()
            )
            target_ids = tuple({edge.to_node_id for edge in outgoing})
            target_projections = (
                tuple(
                    (
                        await session.scalars(
                            select(ToolEffectDagReadyNodeRecord).where(
                                ToolEffectDagReadyNodeRecord.node_id.in_(target_ids)
                            )
                        )
                    ).all()
                )
                if target_ids
                else ()
            )
            target_nodes = (
                tuple(
                    (
                        await session.scalars(
                            select(ToolEffectNodeRecord).where(
                                ToolEffectNodeRecord.node_id.in_(target_ids)
                            )
                        )
                    ).all()
                )
                if target_ids
                else ()
            )
            projections.update({row.node_id: row for row in target_projections})
            affected_nodes.update({row.node_id: row for row in target_nodes})
            if not set(target_ids).issubset(projections) or not set(target_ids).issubset(
                affected_nodes
            ):
                await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
                return
            delta = -1 if target_succeeded else 1
            for target_id in target_ids:
                projection = projections[target_id]
                projection.remaining_predecessors += delta
                if projection.remaining_predecessors < 0:
                    raise EffectReadySetProofRejectedError(graph.graph_id)
                counter_changed_ids.add(target_id)
        timestamp = await database_utc_now(session)
        changed_rows: list[ToolEffectDagReadyNodeRecord] = []
        ready_count_delta = 0
        for node_id, projection in projections.items():
            projected_node = affected_nodes[node_id]
            membership_ready = cls._effect_ready_projection_membership(
                node=projected_node,
                remaining_predecessors=projection.remaining_predecessors,
                unresolved_branches=projection.unresolved_branches,
                branch_rejected=projection.branch_rejected,
                database_time=timestamp,
            )
            membership_changed = membership_ready != projection.membership_ready
            if membership_changed:
                ready_count_delta += 1 if membership_ready else -1
                projection.membership_ready = membership_ready
            if membership_changed or node_id in counter_changed_ids:
                projection.revision += 1
                projection.proof_digest = cls._effect_ready_projection_node_digest(
                    graph_id=projection.graph_id,
                    node_id=projection.node_id,
                    ordinal=projection.ordinal,
                    remaining_predecessors=projection.remaining_predecessors,
                    unresolved_branches=projection.unresolved_branches,
                    branch_rejected=projection.branch_rejected,
                    membership_ready=projection.membership_ready,
                    revision=projection.revision,
                )
                projection.updated_at = timestamp
                changed_rows.append(projection)
        await cls._advance_effect_dag_ready_projection(
            session,
            graph=graph,
            event=event,
            expected_event_seq=expected_event_seq,
            mutation={
                "kind": "node_transition",
                "transition_kind": transition_kind,
                "node_id": node.node_id,
                "node_revision": node.revision,
                "from_status": previous_status.value,
                "to_status": target_status.value,
            },
            changed_rows=tuple(sorted(changed_rows, key=lambda row: row.ordinal)),
            ready_count_delta=ready_count_delta,
        )

    @classmethod
    async def _apply_effect_dag_ready_branch_decision(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        decision: ToolEffectBranchDecisionRecord,
        event: TaskEventRead,
        expected_event_seq: int,
    ) -> None:
        state = await session.get(ToolEffectDagReadyStateRecord, graph.graph_id)
        if state is None or state.event_seq != expected_event_seq or state.membership_version != 1:
            await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
            return
        cls._validate_effect_ready_projection_state(state)
        edges = tuple(
            (
                await session.scalars(
                    select(ToolEffectEdgeRecord).where(
                        ToolEffectEdgeRecord.graph_id == graph.graph_id,
                        ToolEffectEdgeRecord.from_node_id == decision.source_node_id,
                        ToolEffectEdgeRecord.kind == EffectEdgeKind.CONDITIONAL.value,
                        ToolEffectEdgeRecord.decision_key == decision.decision_key,
                    )
                )
            ).all()
        )
        target_ids = tuple({edge.to_node_id for edge in edges})
        projections = {
            row.node_id: row
            for row in (
                await session.scalars(
                    select(ToolEffectDagReadyNodeRecord).where(
                        ToolEffectDagReadyNodeRecord.node_id.in_(target_ids)
                    )
                )
            ).all()
        }
        target_nodes = {
            row.node_id: row
            for row in (
                await session.scalars(
                    select(ToolEffectNodeRecord).where(ToolEffectNodeRecord.node_id.in_(target_ids))
                )
            ).all()
        }
        if set(projections) != set(target_ids) or set(target_nodes) != set(target_ids):
            await cls._rebuild_effect_dag_ready_projection(session, graph=graph)
            return
        timestamp = await database_utc_now(session)
        changed_rows: list[ToolEffectDagReadyNodeRecord] = []
        previous_memberships = {
            node_id: projection.membership_ready for node_id, projection in projections.items()
        }
        for edge in edges:
            projection = projections[edge.to_node_id]
            projection.unresolved_branches -= 1
            if projection.unresolved_branches < 0:
                raise EffectReadySetProofRejectedError(graph.graph_id)
            if edge.expected_outcome != decision.outcome:
                projection.branch_rejected = True
        ready_count_delta = 0
        for target_id, projection in projections.items():
            projection.membership_ready = cls._effect_ready_projection_membership(
                node=target_nodes[target_id],
                remaining_predecessors=projection.remaining_predecessors,
                unresolved_branches=projection.unresolved_branches,
                branch_rejected=projection.branch_rejected,
                database_time=timestamp,
            )
            if projection.membership_ready != previous_memberships[target_id]:
                ready_count_delta += 1 if projection.membership_ready else -1
            projection.revision += 1
            projection.proof_digest = cls._effect_ready_projection_node_digest(
                graph_id=projection.graph_id,
                node_id=projection.node_id,
                ordinal=projection.ordinal,
                remaining_predecessors=projection.remaining_predecessors,
                unresolved_branches=projection.unresolved_branches,
                branch_rejected=projection.branch_rejected,
                membership_ready=projection.membership_ready,
                revision=projection.revision,
            )
            projection.updated_at = timestamp
            changed_rows.append(projection)
        await cls._advance_effect_dag_ready_projection(
            session,
            graph=graph,
            event=event,
            expected_event_seq=expected_event_seq,
            mutation={
                "kind": "branch_decision",
                "decision_id": decision.decision_id,
                "source_node_id": decision.source_node_id,
                "decision_key": decision.decision_key,
                "outcome": decision.outcome,
            },
            changed_rows=tuple(sorted(changed_rows, key=lambda row: row.ordinal)),
            ready_count_delta=ready_count_delta,
        )

    @classmethod
    async def _reconcile_expired_effect_ready_memberships(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        projection: ToolEffectDagReadyStateRecord,
        database_time: datetime,
    ) -> ToolEffectDagReadyStateRecord:
        expected_revision = projection.revision
        expected_digest = projection.content_digest
        locked = await session.scalar(
            select(ToolEffectDagReadyStateRecord)
            .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if (
            locked is None
            or locked.event_seq != graph.last_event_seq
            or locked.revision != expected_revision
            or locked.content_digest != expected_digest
        ):
            raise EffectReadySetProofRejectedError(graph.graph_id)
        cls._validate_effect_ready_projection_state(locked)
        expired_rows = tuple(
            (
                await session.execute(
                    select(ToolEffectNodeRecord, ToolEffectDagReadyNodeRecord)
                    .join(
                        ToolEffectDagReadyNodeRecord,
                        ToolEffectDagReadyNodeRecord.node_id == ToolEffectNodeRecord.node_id,
                    )
                    .where(
                        ToolEffectNodeRecord.graph_id == graph.graph_id,
                        ToolEffectNodeRecord.status.in_(
                            (
                                EffectNodeStatus.PENDING.value,
                                EffectNodeStatus.ACTIVE.value,
                            )
                        ),
                        ToolEffectNodeRecord.claim_owner_id.is_not(None),
                        ToolEffectNodeRecord.claim_expires_at.is_not(None),
                        ToolEffectNodeRecord.claim_expires_at <= database_time,
                        ToolEffectDagReadyNodeRecord.membership_ready.is_(False),
                        ToolEffectDagReadyNodeRecord.branch_rejected.is_(False),
                        ToolEffectDagReadyNodeRecord.remaining_predecessors == 0,
                        ToolEffectDagReadyNodeRecord.unresolved_branches == 0,
                    )
                    .order_by(ToolEffectNodeRecord.claim_expires_at)
                )
            ).all()
        )
        if not expired_rows:
            return locked
        changed_rows: list[ToolEffectDagReadyNodeRecord] = []
        timestamp = await database_utc_now(session)
        for node, ready_node in expired_rows:
            cls._validate_effect_ready_projection_node(ready_node)
            if not cls._effect_ready_projection_membership(
                node=node,
                remaining_predecessors=ready_node.remaining_predecessors,
                unresolved_branches=ready_node.unresolved_branches,
                branch_rejected=ready_node.branch_rejected,
                database_time=database_time,
            ):
                raise EffectReadySetProofRejectedError(graph.graph_id)
            ready_node.membership_ready = True
            ready_node.revision += 1
            ready_node.proof_digest = cls._effect_ready_projection_node_digest(
                graph_id=ready_node.graph_id,
                node_id=ready_node.node_id,
                ordinal=ready_node.ordinal,
                remaining_predecessors=ready_node.remaining_predecessors,
                unresolved_branches=ready_node.unresolved_branches,
                branch_rejected=ready_node.branch_rejected,
                membership_ready=True,
                revision=ready_node.revision,
            )
            ready_node.updated_at = timestamp
            changed_rows.append(ready_node)
        previous_digest = locked.content_digest
        locked.revision += 1
        locked.ready_node_count += len(changed_rows)
        cls._validate_effect_ready_projection_state(locked)
        locked.content_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-projection-expiry.v1",
                "graph_id": graph.graph_id,
                "previous_digest": previous_digest,
                "projection_revision": locked.revision,
                "event_seq": locked.event_seq,
                "database_time": database_time.isoformat(),
                "projected_node_count": locked.projected_node_count,
                "ready_node_count": locked.ready_node_count,
                "changed_rows": [row.proof_digest for row in changed_rows],
            }
        )
        locked.updated_at = timestamp
        await session.flush()
        return locked

    @classmethod
    async def _resolve_effect_ready_cursor(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        projection: ToolEffectDagReadyStateRecord,
        cursor: str | None,
        database_time: datetime,
    ) -> tuple[datetime, int | None]:
        if cursor is None:
            return database_time, None
        checkpoint = await session.get(ToolEffectReadySetCheckpointRecord, cursor)
        if checkpoint is None:
            raise EffectReadySetProofRejectedError(graph.graph_id)
        previous = cls._to_ready_set_checkpoint(checkpoint)
        if (
            previous.graph_id != graph.graph_id
            or not previous.has_more
            or previous.next_cursor != cursor
            or previous.last_ordinal is None
            or previous.graph_fencing_token != graph.fencing_token
            or previous.event_seq != graph.last_event_seq
            or previous.projection_revision != projection.revision
            or previous.projection_digest != projection.content_digest
        ):
            raise EffectReadySetProofRejectedError(graph.graph_id)
        expected_membership = cls._effect_ready_projection_snapshot_digest(
            graph=graph,
            projection=projection,
            total_ready=previous.total_ready,
        )
        expected_digest, expected_last_ordinal = cls._build_effect_ready_projection_page(
            graph=graph,
            projection=projection,
            ready_page=previous.ready_nodes,
            ready_set_digest=expected_membership,
            page_size=previous.page_size,
            cursor=previous.cursor,
            after_ordinal=previous.after_ordinal,
            total_ready=previous.total_ready,
            has_more=previous.has_more,
            database_time=previous.database_time,
        )
        if (
            previous.ready_set_digest != expected_membership
            or previous.proof_digest != expected_digest
            or previous.last_ordinal != expected_last_ordinal
        ):
            raise EffectReadySetProofRejectedError(graph.graph_id)
        return previous.database_time, previous.last_ordinal

    @classmethod
    async def _read_effect_dag_ready_page(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        projection: ToolEffectDagReadyStateRecord,
        database_time: datetime,
        page_size: int,
        after_ordinal: int | None,
    ) -> tuple[
        tuple[EffectReadyNodeProof, ...],
        tuple[ToolEffectNodeRecord, ...],
        int,
        bool,
    ]:
        if (
            EffectGraphStatus(graph.status) is not EffectGraphStatus.ACTIVE
            or graph.cancel_requested_at is not None
        ):
            return (), (), 0, False
        if projection.graph_id != graph.graph_id or projection.event_seq != graph.last_event_seq:
            raise EffectReadySetProofRejectedError(graph.graph_id)
        cls._validate_effect_ready_projection_state(projection)
        total_ready = projection.ready_node_count
        candidate_rows = tuple(
            (
                await session.execute(
                    build_effect_ready_page_statement(
                        graph_id=graph.graph_id,
                        page_size=page_size,
                        after_ordinal=after_ordinal,
                    )
                )
            ).all()
        )
        has_more = len(candidate_rows) > page_size
        rows = candidate_rows[:page_size]
        if after_ordinal is None and (
            (total_ready <= page_size and len(candidate_rows) != total_ready)
            or (total_ready > page_size and len(candidate_rows) != page_size + 1)
        ):
            raise EffectReadySetProofRejectedError(graph.graph_id)
        if not rows:
            return (), (), total_ready, False
        page_nodes = tuple(row[0] for row in rows)
        ready_projections = {row[1].node_id: row[1] for row in rows}
        for ready_projection in ready_projections.values():
            cls._validate_effect_ready_projection_node(ready_projection)
        page_ids = tuple(node.node_id for node in page_nodes)
        incoming_edges = tuple(
            (
                await session.scalars(
                    select(ToolEffectEdgeRecord)
                    .where(
                        ToolEffectEdgeRecord.to_node_id.in_(page_ids),
                        ToolEffectEdgeRecord.kind.in_(
                            (
                                EffectEdgeKind.SUCCESS.value,
                                EffectEdgeKind.CONDITIONAL.value,
                            )
                        ),
                    )
                    .order_by(
                        ToolEffectEdgeRecord.to_node_id,
                        ToolEffectEdgeRecord.from_node_id,
                        ToolEffectEdgeRecord.edge_id,
                    )
                )
            ).all()
        )
        predecessor_ids = tuple({edge.from_node_id for edge in incoming_edges})
        predecessor_nodes = (
            tuple(
                (
                    await session.scalars(
                        select(ToolEffectNodeRecord).where(
                            ToolEffectNodeRecord.node_id.in_(predecessor_ids)
                        )
                    )
                ).all()
            )
            if predecessor_ids
            else ()
        )
        nodes_by_id = {node.node_id: node for node in (*page_nodes, *predecessor_nodes)}
        conditional = tuple(
            edge for edge in incoming_edges if edge.kind == EffectEdgeKind.CONDITIONAL.value
        )
        conditional_sources = tuple({edge.from_node_id for edge in conditional})
        conditional_keys = tuple(
            {edge.decision_key for edge in conditional if edge.decision_key is not None}
        )
        decision_records = (
            tuple(
                (
                    await session.scalars(
                        select(ToolEffectBranchDecisionRecord).where(
                            ToolEffectBranchDecisionRecord.graph_id == graph.graph_id,
                            ToolEffectBranchDecisionRecord.source_node_id.in_(conditional_sources),
                            ToolEffectBranchDecisionRecord.decision_key.in_(conditional_keys),
                        )
                    )
                ).all()
            )
            if conditional_sources and conditional_keys
            else ()
        )
        decisions_by_key = {
            (record.source_node_id, record.decision_key): record for record in decision_records
        }
        edges_by_target: defaultdict[str, list[ToolEffectEdgeRecord]] = defaultdict(list)
        for edge in incoming_edges:
            edges_by_target[edge.to_node_id].append(edge)
        proofs: list[EffectReadyNodeProof] = []
        for node in page_nodes:
            ready_projection = ready_projections[node.node_id]
            predecessors = tuple(
                nodes_by_id[edge.from_node_id] for edge in edges_by_target[node.node_id]
            )
            if len(predecessors) != len(edges_by_target[node.node_id]) or any(
                EffectNodeStatus(predecessor.status) is not EffectNodeStatus.SUCCEEDED
                for predecessor in predecessors
            ):
                raise EffectReadySetProofRejectedError(graph.graph_id)
            matching_decisions: list[EffectBranchDecisionRead] = []
            for edge in edges_by_target[node.node_id]:
                if edge.kind != EffectEdgeKind.CONDITIONAL.value:
                    continue
                if edge.decision_key is None or edge.expected_outcome is None:
                    raise EffectReadySetProofRejectedError(graph.graph_id)
                record = decisions_by_key.get((edge.from_node_id, edge.decision_key))
                source = nodes_by_id.get(edge.from_node_id)
                if record is None or source is None or record.outcome != edge.expected_outcome:
                    raise EffectReadySetProofRejectedError(graph.graph_id)
                if record.decision_id != effect_branch_decision_id(record.proof_digest):
                    raise EffectBranchDecisionProofRejectedError(record.decision_id)
                matching_decisions.append(
                    EffectBranchDecisionRead(
                        decision_id=record.decision_id,
                        graph_id=record.graph_id,
                        source_node_id=record.source_node_id,
                        source_node_key=source.node_key,
                        decision_key=record.decision_key,
                        outcome=record.outcome,
                        evidence_digest=record.evidence_digest,
                        source_node_revision=record.source_node_revision,
                        source_event_seq=record.source_event_seq,
                        proof_digest=record.proof_digest,
                        event_seq=record.event_seq,
                        created_at=cls._as_utc(record.created_at),
                    )
                )
            if (
                ready_projection.remaining_predecessors != 0
                or ready_projection.unresolved_branches != 0
                or ready_projection.branch_rejected
                or not ready_projection.membership_ready
                or not cls._effect_ready_projection_membership(
                    node=node,
                    remaining_predecessors=ready_projection.remaining_predecessors,
                    unresolved_branches=ready_projection.unresolved_branches,
                    branch_rejected=ready_projection.branch_rejected,
                    database_time=database_time,
                )
            ):
                raise EffectReadySetProofRejectedError(graph.graph_id)
            claim_expiry = (
                cls._as_utc(node.claim_expires_at) if node.claim_expires_at is not None else None
            )
            proofs.append(
                EffectReadyNodeProof(
                    node_id=node.node_id,
                    node_key=node.node_key,
                    ordinal=node.ordinal,
                    status=EffectNodeStatus(node.status),
                    revision=node.revision,
                    last_event_seq=node.last_event_seq,
                    prior_claim_fencing_token=node.claim_fencing_token,
                    prior_claim_expires_at=claim_expiry,
                    predecessors=tuple(
                        EffectPredecessorProof(
                            node_id=predecessor.node_id,
                            node_key=predecessor.node_key,
                            status=EffectNodeStatus(predecessor.status),
                            revision=predecessor.revision,
                            last_event_seq=predecessor.last_event_seq,
                        )
                        for predecessor in sorted(
                            predecessors,
                            key=lambda item: item.ordinal,
                        )
                    ),
                    branch_decisions=tuple(
                        cls._to_effect_branch_decision_proof(decision)
                        for decision in sorted(
                            matching_decisions,
                            key=lambda item: (
                                nodes_by_id[item.source_node_id].ordinal,
                                item.decision_key,
                            ),
                        )
                    ),
                )
            )
        return tuple(proofs), page_nodes, total_ready, has_more

    @staticmethod
    def _effect_ready_projection_snapshot_digest(
        *,
        graph: ToolEffectGraphRecord,
        projection: ToolEffectDagReadyStateRecord,
        total_ready: int,
    ) -> str:
        return sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-set-membership.v4",
                "graph_id": graph.graph_id,
                "graph_fencing_token": graph.fencing_token,
                "event_seq": graph.last_event_seq,
                "projection_revision": projection.revision,
                "projection_digest": projection.content_digest,
                "projected_node_count": projection.projected_node_count,
                "ready_node_count": projection.ready_node_count,
                "total_ready": total_ready,
            }
        )

    @staticmethod
    def _build_effect_ready_projection_page(
        *,
        graph: ToolEffectGraphRecord,
        projection: ToolEffectDagReadyStateRecord,
        ready_page: tuple[EffectReadyNodeProof, ...],
        ready_set_digest: str,
        page_size: int,
        cursor: str | None,
        after_ordinal: int | None,
        total_ready: int,
        has_more: bool,
        database_time: datetime,
    ) -> tuple[str, int | None]:
        last_ordinal = ready_page[-1].ordinal if ready_page else None
        proof_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-set.v6",
                "graph_id": graph.graph_id,
                "graph_fencing_token": graph.fencing_token,
                "event_seq": graph.last_event_seq,
                "projection_revision": projection.revision,
                "projection_digest": projection.content_digest,
                "database_time": database_time.isoformat(),
                "ready_set_digest": ready_set_digest,
                "cursor": cursor,
                "page_size": page_size,
                "after_ordinal": after_ordinal,
                "last_ordinal": last_ordinal,
                "total_ready": total_ready,
                "has_more": has_more,
                "ready_nodes": [node.model_dump(mode="json") for node in ready_page],
            }
        )
        return proof_digest, last_ordinal

    @classmethod
    def _build_effect_ready_set(
        cls,
        *,
        graph: ToolEffectGraphRecord,
        nodes: tuple[ToolEffectNodeRecord, ...],
        edges: tuple[ToolEffectEdgeRecord, ...],
        decisions: tuple[ToolEffectBranchDecisionRecord, ...],
        database_now: datetime,
    ) -> tuple[tuple[EffectReadyNodeProof, ...], str]:
        nodes_by_id = {node.node_id: node for node in nodes}
        predecessor_ids: defaultdict[str, list[str]] = defaultdict(list)
        conditional_edges: defaultdict[str, list[ToolEffectEdgeRecord]] = defaultdict(list)
        for edge in edges:
            if edge.kind in {
                EffectEdgeKind.SUCCESS.value,
                EffectEdgeKind.CONDITIONAL.value,
            }:
                predecessor_ids[edge.to_node_id].append(edge.from_node_id)
            if edge.kind == EffectEdgeKind.CONDITIONAL.value:
                conditional_edges[edge.to_node_id].append(edge)
        decisions_by_key: dict[tuple[str, str], EffectBranchDecisionRead] = {}
        for decision_record in decisions:
            source = nodes_by_id.get(decision_record.source_node_id)
            if source is None:
                raise EffectBranchDecisionProofRejectedError(decision_record.decision_id)
            decision_read = cls._to_effect_branch_decision(
                decision_record,
                source=source,
                edges=edges,
            )
            decisions_by_key[(source.node_id, decision_record.decision_key)] = decision_read
        ready_nodes: list[EffectReadyNodeProof] = []
        graph_accepts_claims = (
            EffectGraphStatus(graph.status) is EffectGraphStatus.ACTIVE
            and graph.cancel_requested_at is None
        )
        for node in nodes:
            if not graph_accepts_claims:
                continue
            status = EffectNodeStatus(node.status)
            claim_expiry = (
                cls._as_utc(node.claim_expires_at) if node.claim_expires_at is not None else None
            )
            claim_available = (
                node.claim_owner_id is None or claim_expiry is None or claim_expiry <= database_now
            )
            pending = status is EffectNodeStatus.PENDING and claim_available
            reclaimable = status is EffectNodeStatus.ACTIVE and claim_available
            if not (pending or reclaimable):
                continue
            predecessors = tuple(nodes_by_id[node_id] for node_id in predecessor_ids[node.node_id])
            if any(
                EffectNodeStatus(predecessor.status) is not EffectNodeStatus.SUCCEEDED
                for predecessor in predecessors
            ):
                continue
            matching_decisions: list[EffectBranchDecisionRead] = []
            branch_selected = True
            for edge in conditional_edges[node.node_id]:
                if edge.decision_key is None or edge.expected_outcome is None:
                    raise EffectReadySetProofRejectedError(graph.graph_id)
                selected_decision = decisions_by_key.get((edge.from_node_id, edge.decision_key))
                if selected_decision is None or selected_decision.outcome != edge.expected_outcome:
                    branch_selected = False
                    break
                matching_decisions.append(selected_decision)
            if not branch_selected:
                continue
            ready_nodes.append(
                EffectReadyNodeProof(
                    node_id=node.node_id,
                    node_key=node.node_key,
                    ordinal=node.ordinal,
                    status=status,
                    revision=node.revision,
                    last_event_seq=node.last_event_seq,
                    prior_claim_fencing_token=node.claim_fencing_token,
                    prior_claim_expires_at=claim_expiry,
                    predecessors=tuple(
                        EffectPredecessorProof(
                            node_id=predecessor.node_id,
                            node_key=predecessor.node_key,
                            status=EffectNodeStatus(predecessor.status),
                            revision=predecessor.revision,
                            last_event_seq=predecessor.last_event_seq,
                        )
                        for predecessor in sorted(predecessors, key=lambda item: item.ordinal)
                    ),
                    branch_decisions=tuple(
                        cls._to_effect_branch_decision_proof(decision)
                        for decision in sorted(
                            matching_decisions,
                            key=lambda item: (
                                nodes_by_id[item.source_node_id].ordinal,
                                item.decision_key,
                            ),
                        )
                    ),
                )
            )
        proof = tuple(ready_nodes)
        digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-ready-set-membership.v1",
                "graph_id": graph.graph_id,
                "graph_revision": graph.revision,
                "event_seq": graph.last_event_seq,
                "ready_nodes": [node.model_dump(mode="json") for node in proof],
            }
        )
        return proof, digest

    @staticmethod
    async def _assert_effect_graph_lease(
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        owner_id: str,
        fencing_token: int,
    ) -> None:
        graph_id = await session.scalar(
            select(ToolEffectGraphRecord.graph_id).where(
                ToolEffectGraphRecord.graph_id == graph.graph_id,
                ToolEffectGraphRecord.lease_owner_id == owner_id,
                ToolEffectGraphRecord.fencing_token == fencing_token,
                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
            )
        )
        if graph_id is None:
            raise EffectGraphFenceRejectedError(graph.graph_id)

    @classmethod
    def _to_ready_set_checkpoint(
        cls,
        checkpoint: ToolEffectReadySetCheckpointRecord,
    ) -> EffectReadySetCheckpointRead:
        proof = checkpoint.predecessor_proof
        if proof.get("schema_version") != "deskpilot.effect-ready-set.v6":
            raise EffectReadySetProofRejectedError(checkpoint.graph_id)
        try:
            ready_nodes = tuple(
                EffectReadyNodeProof.model_validate(item) for item in proof["ready_nodes"]
            )
            graph_fencing_token = int(proof["graph_fencing_token"])
            projection_revision = int(proof["projection_revision"])
            projection_digest = str(proof["projection_digest"])
            database_time = datetime.fromisoformat(str(proof["database_time"]))
            ready_set_digest = str(proof["ready_set_digest"])
            raw_cursor = proof["cursor"]
            cursor = None if raw_cursor is None else str(raw_cursor)
            page_size = int(proof["page_size"])
            raw_after_ordinal = proof["after_ordinal"]
            after_ordinal = None if raw_after_ordinal is None else int(raw_after_ordinal)
            raw_last_ordinal = proof["last_ordinal"]
            last_ordinal = None if raw_last_ordinal is None else int(raw_last_ordinal)
            total_ready = int(proof["total_ready"])
            has_more = proof["has_more"]
        except (KeyError, TypeError, ValueError):
            raise EffectReadySetProofRejectedError(checkpoint.graph_id) from None
        if not isinstance(has_more, bool):
            raise EffectReadySetProofRejectedError(checkpoint.graph_id)
        if tuple(node.node_id for node in ready_nodes) != tuple(checkpoint.ready_node_ids):
            raise EffectReadySetProofRejectedError(checkpoint.graph_id)
        if (
            checkpoint.checkpoint_id != f"ter_{checkpoint.proof_digest}"
            or re.fullmatch(r"[0-9a-f]{64}", checkpoint.proof_digest) is None
            or graph_fencing_token < 1
            or projection_revision < 1
            or re.fullmatch(r"[0-9a-f]{64}", projection_digest) is None
            or re.fullmatch(r"[0-9a-f]{64}", ready_set_digest) is None
            or not 1 <= page_size <= 1_000
            or total_ready < 0
            or len(ready_nodes) > page_size
            or total_ready < len(ready_nodes)
            or (cursor is None) != (after_ordinal is None)
            or (cursor is not None and (not cursor.startswith("ter_") or len(cursor) != 68))
            or (after_ordinal is not None and after_ordinal < 0)
            or (last_ordinal is not None and last_ordinal < 0)
            or (ready_nodes and last_ordinal != ready_nodes[-1].ordinal)
            or (not ready_nodes and last_ordinal is not None)
            or any(
                current.ordinal <= previous.ordinal
                for previous, current in zip(ready_nodes, ready_nodes[1:], strict=False)
            )
            or (
                after_ordinal is not None
                and any(node.ordinal <= after_ordinal for node in ready_nodes)
            )
            or (has_more and not ready_nodes)
        ):
            raise EffectReadySetProofRejectedError(checkpoint.graph_id)
        database_time = cls._as_utc(database_time)
        next_cursor = checkpoint.checkpoint_id if has_more else None
        return EffectReadySetCheckpointRead(
            checkpoint_id=checkpoint.checkpoint_id,
            graph_id=checkpoint.graph_id,
            graph_revision=checkpoint.graph_revision,
            graph_fencing_token=graph_fencing_token,
            event_seq=checkpoint.event_seq,
            projection_revision=projection_revision,
            projection_digest=projection_digest,
            ready_nodes=ready_nodes,
            ready_set_digest=ready_set_digest,
            proof_digest=checkpoint.proof_digest,
            cursor=cursor,
            page_size=page_size,
            after_ordinal=after_ordinal,
            last_ordinal=last_ordinal,
            next_cursor=next_cursor,
            total_ready=total_ready,
            has_more=has_more,
            database_time=database_time,
            created_at=cls._as_utc(checkpoint.created_at),
        )

    @classmethod
    def _to_compensation_plan(
        cls,
        record: ToolEffectCompensationPlanRecord,
    ) -> EffectCompensationPlanRead:
        return EffectCompensationPlanRead(
            plan_id=record.plan_id,
            graph_id=record.graph_id,
            graph_revision=record.graph_revision,
            event_seq=record.event_seq,
            waves=tuple(
                EffectCompensationWaveRead(
                    ordinal=ordinal,
                    node_ids=tuple(node_ids),
                )
                for ordinal, node_ids in enumerate(record.waves)
            ),
            proof_digest=record.proof_digest,
            created_at=cls._as_utc(record.created_at),
        )

    @classmethod
    async def _record_effect_transition(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        node: ToolEffectNodeRecord,
        event: TaskEventRead,
        transition_kind: str,
        target_node_status: EffectNodeStatus,
        target_graph_status: EffectGraphStatus,
        attempt_id: str | None,
        bump_revisions: bool = True,
    ) -> None:
        previous_node_status = EffectNodeStatus(node.status)
        previous_graph_status = EffectGraphStatus(graph.status)
        previous_graph_event_seq = graph.last_event_seq
        timestamp = utc_now()
        node.status = target_node_status.value
        if bump_revisions:
            node.revision += 1
        node.last_event_seq = event.seq
        node.updated_at = timestamp
        graph.status = target_graph_status.value
        if bump_revisions:
            graph.revision += 1
        graph.last_event_seq = event.seq
        graph.updated_at = timestamp
        transition_id = f"tet_{hashlib.sha256(event.event_id.encode('utf-8')).hexdigest()}"
        session.add(
            ToolEffectTransitionRecord(
                transition_id=transition_id,
                graph_id=graph.graph_id,
                node_id=node.node_id,
                attempt_id=attempt_id,
                event_id=event.event_id,
                event_seq=event.seq,
                transition_kind=transition_kind,
                from_status=previous_node_status.value,
                to_status=target_node_status.value,
                graph_from_status=previous_graph_status.value,
                graph_to_status=target_graph_status.value,
                created_at=timestamp,
            )
        )
        if graph.schema_version == EFFECT_DAG_SCHEMA_VERSION:
            await cls._apply_effect_dag_ready_transition(
                session,
                graph=graph,
                node=node,
                event=event,
                expected_event_seq=previous_graph_event_seq,
                previous_status=previous_node_status,
                target_status=target_node_status,
                transition_kind=transition_kind,
            )

    @staticmethod
    async def _fence_effect_dag_admission_proofs(
        session: AsyncSession,
        *,
        graph_id: str,
        node_ids: tuple[str, ...],
        claim_owner_id: str,
        admission_proofs: Mapping[str, EffectDagAdmissionProof] | None,
        database_now: datetime,
    ) -> None:
        if admission_proofs is None:
            return
        if set(admission_proofs) != set(node_ids):
            raise EffectDagAdmissionProofRejectedError(graph_id)
        for node_id in node_ids:
            proof = admission_proofs[node_id]
            if proof.owner_id != claim_owner_id:
                raise EffectDagAdmissionProofRejectedError(graph_id)
            result = await session.execute(
                update(ToolEffectDagAdmissionRecord)
                .where(
                    ToolEffectDagAdmissionRecord.admission_id == proof.admission_id,
                    ToolEffectDagAdmissionRecord.graph_id == graph_id,
                    ToolEffectDagAdmissionRecord.node_id == node_id,
                    ToolEffectDagAdmissionRecord.owner_id == proof.owner_id,
                    ToolEffectDagAdmissionRecord.status == "granted",
                    ToolEffectDagAdmissionRecord.fencing_token == proof.fencing_token,
                    ToolEffectDagAdmissionRecord.expires_at > func.current_timestamp(),
                )
                .values(
                    revision=ToolEffectDagAdmissionRecord.revision + 1,
                    heartbeat_at=database_now,
                    updated_at=database_now,
                )
                .execution_options(synchronize_session=False)
            )
            if int(getattr(result, "rowcount", 0)) != 1:
                raise EffectDagAdmissionProofRejectedError(graph_id)

    @classmethod
    async def _fence_effect_mutation(
        cls,
        session: AsyncSession,
        *,
        graph: ToolEffectGraphRecord,
        node: ToolEffectNodeRecord,
        owner_id: str,
        fencing_token: int,
        node_claim_owner_id: str | None = None,
        node_claim_fencing_token: int | None = None,
    ) -> None:
        """Claim graph/node revisions only while the database lease is live."""
        database_now = await database_utc_now(session)
        graph_revision = graph.revision
        node_revision = node.revision
        graph_result = await session.execute(
            update(ToolEffectGraphRecord)
            .where(
                ToolEffectGraphRecord.graph_id == graph.graph_id,
                ToolEffectGraphRecord.revision == graph_revision,
                ToolEffectGraphRecord.lease_owner_id == owner_id,
                ToolEffectGraphRecord.fencing_token == fencing_token,
                ToolEffectGraphRecord.lease_expires_at > func.current_timestamp(),
            )
            .values(revision=graph_revision + 1, updated_at=database_now)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(graph_result, "rowcount", 0)) != 1:
            raise EffectGraphFenceRejectedError(graph.graph_id)
        node_conditions = [
            ToolEffectNodeRecord.node_id == node.node_id,
            ToolEffectNodeRecord.revision == node_revision,
        ]
        if node_claim_owner_id is not None or node_claim_fencing_token is not None:
            if node_claim_owner_id is None or node_claim_fencing_token is None:
                raise ValueError("Effect node claim owner and fence must be paired")
            node_conditions.extend(
                (
                    ToolEffectNodeRecord.claim_owner_id == node_claim_owner_id,
                    ToolEffectNodeRecord.claim_fencing_token == node_claim_fencing_token,
                    ToolEffectNodeRecord.claim_expires_at > func.current_timestamp(),
                )
            )
        node_result = await session.execute(
            update(ToolEffectNodeRecord)
            .where(*node_conditions)
            .values(revision=node_revision + 1, updated_at=database_now)
            .execution_options(synchronize_session=False)
        )
        if int(getattr(node_result, "rowcount", 0)) != 1:
            if node_claim_owner_id is not None:
                raise EffectNodeFenceRejectedError(node.node_id)
            raise InvalidEffectTransitionError(
                "Effect node revision changed during a fenced transition"
            )
        graph.revision = graph_revision + 1
        node.revision = node_revision + 1

    @staticmethod
    async def _create_effect_for_succeeded_attempt(
        session: AsyncSession,
        *,
        node: ToolEffectNodeRecord,
        attempt: ToolEffectAttemptRecord,
        receipt_id: str | None,
    ) -> None:
        if receipt_id is None and node.compensation_strategy != CompensationStrategy.NONE.value:
            raise InvalidEffectTransitionError(
                "Compensable effects require a durable commit receipt"
            )
        effect_identity = tool_effect_id(attempt.attempt_id)
        existing_effect = await session.get(ToolEffectRecord, effect_identity)
        if existing_effect is not None:
            attempt.effect_id = effect_identity
            return
        kind = EffectAttemptKind(attempt.kind)
        compensates_effect_id: str | None = None
        state = EffectState.APPLIED
        if kind is EffectAttemptKind.COMPENSATION:
            original = await session.scalar(
                select(ToolEffectRecord)
                .where(
                    ToolEffectRecord.node_id == node.node_id,
                    ToolEffectRecord.kind == EffectAttemptKind.FORWARD.value,
                )
                .order_by(ToolEffectRecord.created_at)
            )
            if original is None or original.state != EffectState.APPLIED.value:
                raise InvalidEffectTransitionError("Compensation has no applied forward effect")
            original.state = EffectState.COMPENSATED.value
            original.updated_at = utc_now()
            compensates_effect_id = original.effect_id
            state = EffectState.COMPENSATION_APPLIED
        timestamp = utc_now()
        session.add(
            ToolEffectRecord(
                effect_id=effect_identity,
                node_id=node.node_id,
                attempt_id=attempt.attempt_id,
                kind=attempt.kind,
                state=state.value,
                receipt_id=receipt_id,
                compensates_effect_id=compensates_effect_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        attempt.effect_id = effect_identity

    @classmethod
    def _to_effect_graph_lease(
        cls,
        graph: ToolEffectGraphRecord,
    ) -> EffectGraphLeaseRead:
        if (
            graph.lease_owner_id is None
            or graph.lease_acquired_at is None
            or graph.lease_heartbeat_at is None
            or graph.lease_expires_at is None
            or graph.fencing_token < 1
        ):
            raise EffectGraphLeaseUnavailableError(graph.task_id)
        return EffectGraphLeaseRead(
            graph_id=graph.graph_id,
            owner_id=graph.lease_owner_id,
            fencing_token=graph.fencing_token,
            acquired_at=cls._as_utc(graph.lease_acquired_at),
            heartbeat_at=cls._as_utc(graph.lease_heartbeat_at),
            expires_at=cls._as_utc(graph.lease_expires_at),
        )

    async def _to_effect_graph(
        self,
        session: AsyncSession,
        graph: ToolEffectGraphRecord,
    ) -> EffectGraphRead:
        if graph.schema_version not in {
            EFFECT_GRAPH_SCHEMA_VERSION,
            EFFECT_DAG_SCHEMA_VERSION,
        }:
            raise InvalidEffectTransitionError("Tool effect graph schema version is unsupported")
        nodes = tuple(
            (
                await session.scalars(
                    select(ToolEffectNodeRecord)
                    .where(ToolEffectNodeRecord.graph_id == graph.graph_id)
                    .order_by(ToolEffectNodeRecord.ordinal)
                )
            ).all()
        )
        edges = tuple(
            (
                await session.scalars(
                    select(ToolEffectEdgeRecord)
                    .where(ToolEffectEdgeRecord.graph_id == graph.graph_id)
                    .order_by(
                        ToolEffectEdgeRecord.from_node_id,
                        ToolEffectEdgeRecord.to_node_id,
                        ToolEffectEdgeRecord.kind,
                    )
                )
            ).all()
        )
        branch_decision_records = await self._load_effect_branch_decisions(session, graph.graph_id)
        attempts = tuple(
            (
                await session.scalars(
                    select(ToolEffectAttemptRecord)
                    .join(
                        ToolEffectNodeRecord,
                        ToolEffectNodeRecord.node_id == ToolEffectAttemptRecord.node_id,
                    )
                    .where(ToolEffectNodeRecord.graph_id == graph.graph_id)
                    .order_by(
                        ToolEffectNodeRecord.ordinal,
                        ToolEffectAttemptRecord.kind,
                        ToolEffectAttemptRecord.attempt,
                    )
                )
            ).all()
        )
        effects = tuple(
            (
                await session.scalars(
                    select(ToolEffectRecord)
                    .join(
                        ToolEffectNodeRecord,
                        ToolEffectNodeRecord.node_id == ToolEffectRecord.node_id,
                    )
                    .where(ToolEffectNodeRecord.graph_id == graph.graph_id)
                    .order_by(ToolEffectRecord.created_at, ToolEffectRecord.effect_id)
                )
            ).all()
        )
        transitions = tuple(
            (
                await session.scalars(
                    select(ToolEffectTransitionRecord)
                    .where(ToolEffectTransitionRecord.graph_id == graph.graph_id)
                    .order_by(ToolEffectTransitionRecord.event_seq)
                )
            ).all()
        )
        attempts_by_node: defaultdict[str, list[EffectAttemptRead]] = defaultdict(list)
        for attempt in attempts:
            attempts_by_node[attempt.node_id].append(
                EffectAttemptRead(
                    attempt_id=attempt.attempt_id,
                    kind=EffectAttemptKind(attempt.kind),
                    attempt=attempt.attempt,
                    call_id=attempt.call_id,
                    status=EffectAttemptStatus(attempt.status),
                    effect_id=attempt.effect_id,
                    last_event_seq=attempt.last_event_seq,
                )
            )
        effects_by_node: defaultdict[str, list[ToolEffectRead]] = defaultdict(list)
        for effect in effects:
            effects_by_node[effect.node_id].append(
                ToolEffectRead(
                    effect_id=effect.effect_id,
                    attempt_id=effect.attempt_id,
                    kind=EffectAttemptKind(effect.kind),
                    state=EffectState(effect.state),
                    receipt_id=effect.receipt_id,
                    compensates_effect_id=effect.compensates_effect_id,
                )
            )
        return EffectGraphRead(
            graph_id=graph.graph_id,
            task_id=graph.task_id,
            schema_version=(
                EFFECT_DAG_SCHEMA_VERSION
                if graph.schema_version == EFFECT_DAG_SCHEMA_VERSION
                else EFFECT_GRAPH_SCHEMA_VERSION
            ),
            status=EffectGraphStatus(graph.status),
            execution_mode=EffectExecutionMode(graph.execution_mode),
            current_node_id=graph.current_node_id,
            failure_node_id=graph.failure_node_id,
            fencing_token=graph.fencing_token,
            lease_owner_id=graph.lease_owner_id,
            lease_acquired_at=(
                self._as_utc(graph.lease_acquired_at)
                if graph.lease_acquired_at is not None
                else None
            ),
            lease_heartbeat_at=(
                self._as_utc(graph.lease_heartbeat_at)
                if graph.lease_heartbeat_at is not None
                else None
            ),
            lease_expires_at=(
                self._as_utc(graph.lease_expires_at) if graph.lease_expires_at is not None else None
            ),
            cancel_requested_at=(
                self._as_utc(graph.cancel_requested_at)
                if graph.cancel_requested_at is not None
                else None
            ),
            revision=graph.revision,
            last_event_seq=graph.last_event_seq,
            nodes=tuple(
                EffectNodeRead(
                    node_id=node.node_id,
                    node_key=node.node_key,
                    ordinal=node.ordinal,
                    step_id=node.step_id,
                    tool_name=node.tool_name,
                    tool_version=node.tool_version,
                    contract_digest=node.contract_digest,
                    compensation_strategy=CompensationStrategy(node.compensation_strategy),
                    status=EffectNodeStatus(node.status),
                    revision=node.revision,
                    last_event_seq=node.last_event_seq,
                    claim_owner_id=node.claim_owner_id,
                    claim_acquired_at=(
                        self._as_utc(node.claim_acquired_at)
                        if node.claim_acquired_at is not None
                        else None
                    ),
                    claim_heartbeat_at=(
                        self._as_utc(node.claim_heartbeat_at)
                        if node.claim_heartbeat_at is not None
                        else None
                    ),
                    claim_expires_at=(
                        self._as_utc(node.claim_expires_at)
                        if node.claim_expires_at is not None
                        else None
                    ),
                    claim_fencing_token=node.claim_fencing_token,
                    attempts=tuple(attempts_by_node[node.node_id]),
                    effects=tuple(effects_by_node[node.node_id]),
                )
                for node in nodes
            ),
            edges=tuple(
                EffectEdgeRead(
                    edge_id=edge.edge_id,
                    from_node_id=edge.from_node_id,
                    to_node_id=edge.to_node_id,
                    kind=EffectEdgeKind(edge.kind),
                    decision_key=edge.decision_key,
                    expected_outcome=edge.expected_outcome,
                )
                for edge in edges
            ),
            branch_decisions=tuple(
                self._to_effect_branch_decision(
                    decision,
                    source=next(node for node in nodes if node.node_id == decision.source_node_id),
                    edges=edges,
                )
                for decision in branch_decision_records
            ),
            transitions=tuple(
                EffectTransitionRead(
                    transition_id=transition.transition_id,
                    node_id=transition.node_id,
                    attempt_id=transition.attempt_id,
                    event_id=transition.event_id,
                    event_seq=transition.event_seq,
                    transition_kind=transition.transition_kind,
                    from_status=EffectNodeStatus(transition.from_status),
                    to_status=EffectNodeStatus(transition.to_status),
                    graph_from_status=EffectGraphStatus(transition.graph_from_status),
                    graph_to_status=EffectGraphStatus(transition.graph_to_status),
                    created_at=self._as_utc(transition.created_at),
                )
                for transition in transitions
            ),
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
