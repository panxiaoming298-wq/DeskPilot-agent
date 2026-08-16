"""Public contracts for manually reconciling uncertain Tool calls."""

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.domain.effect_graph import EffectGraphRead
from deskpilot.domain.schemas import TaskRead
from deskpilot.domain.tool_commit import ToolCommitReceipt
from deskpilot.domain.tool_contracts import ToolIdempotency


class ReconciliationStatus(StrEnum):
    PENDING = "pending"
    RESOLVED = "resolved"


class ReconciliationOutcome(StrEnum):
    CONFIRMED_SUCCEEDED = "confirmed_succeeded"
    CONFIRMED_FAILED = "confirmed_failed"
    CONFIRMED_NO_EFFECT = "confirmed_no_effect"
    ACCEPTED_UNKNOWN = "accepted_unknown"


class ReconciliationEvidenceKind(StrEnum):
    COMMIT_RECEIPT = "commit_receipt"
    NO_RECEIPT = "no_receipt"
    QUERY_FAILED = "query_failed"


class GraphRecoveryStatus(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    PENDING = "pending"
    APPLIED = "applied"


class GraphRecoveryAction(StrEnum):
    CONTINUE = "continue"
    TERMINATE = "terminate"


class ToolIdempotencyReceiptRead(BaseModel):
    """Durable ownership of a caller-supplied Tool idempotency key digest."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    receipt_id: str
    call_id: str
    tool_name: str
    tool_version: str
    key_digest: str
    arguments_digest: str
    created_at: datetime


class ReconciliationReceiptEvidenceRead(BaseModel):
    """One immutable, secret-free observation from the signed Runner query."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str
    kind: ReconciliationEvidenceKind
    queried_runner_id: str | None
    commit_receipt: ToolCommitReceipt | None
    error_code: str | None
    observed_at: datetime


class ReconciliationRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation_id: str
    task_id: str
    call_id: str
    step_id: str
    attempt: int = Field(ge=1)
    tool_name: str
    tool_version: str
    contract_digest: str
    arguments_digest: str
    idempotency: ToolIdempotency
    runner_id: str | None
    call_error_code: str | None
    call_resolution_source: str | None
    call_requested_at: datetime
    call_started_at: datetime | None
    call_finished_at: datetime | None
    status: ReconciliationStatus
    outcome: ReconciliationOutcome | None
    evidence_summary: str | None
    resolved_by: str | None
    unknown_at: datetime
    resolved_at: datetime | None
    graph_recovery_status: GraphRecoveryStatus
    graph_recovery_action: GraphRecoveryAction | None
    graph_recovery_event_id: str | None
    graph_recovered_at: datetime | None
    can_create_attempt: bool
    new_attempt_task_id: str | None
    new_attempt_created_at: datetime | None
    can_create_compensation: bool
    compensation_task_id: str | None
    compensation_receipt_id: str | None
    compensation_created_at: datetime | None
    idempotency_receipt: ToolIdempotencyReceiptRead | None
    receipt_evidence: tuple[ReconciliationReceiptEvidenceRead, ...] = ()
    updated_at: datetime


class ResolveReconciliationCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    outcome: ReconciliationOutcome
    evidence_summary: str = Field(min_length=1, max_length=2_000)


class ReconciliationResolutionRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation: ReconciliationRead
    replayed: bool


class ReconciliationAttemptRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation: ReconciliationRead
    task: TaskRead
    replayed: bool


class ReconciliationEvidenceRefreshRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation: ReconciliationRead
    evidence: ReconciliationReceiptEvidenceRead
    replayed: bool


class ReconciliationCompensationRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation: ReconciliationRead
    task: TaskRead
    replayed: bool


class RecoverGraphCommand(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: GraphRecoveryAction


class ReconciliationGraphRecoveryRead(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reconciliation: ReconciliationRead
    task: TaskRead
    graph: EffectGraphRead
    replayed: bool
    resumed: bool
