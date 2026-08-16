"""Secret-free contracts for the protected effect-runtime operations surface."""

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EFFECT_RUNTIME_OPERATIONS_SCHEMA_VERSION: Literal["deskpilot.effect-runtime-operations.v1"] = (
    "deskpilot.effect-runtime-operations.v1"
)


class OperationsAlertSeverity(StrEnum):
    WARNING = "warning"
    CRITICAL = "critical"


class OperationsAlert(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    severity: OperationsAlertSeverity
    domain: str = Field(min_length=1, max_length=32)
    count: int = Field(ge=1)


class OperationsAlertTransition(StrEnum):
    OPENED = "opened"
    UPDATED = "updated"
    RESOLVED = "resolved"


class OperationsAlertNotificationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notification_id: str
    sequence: int = Field(ge=1)
    alert_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,119}$")
    transition: OperationsAlertTransition
    severity: OperationsAlertSeverity
    domain: str = Field(min_length=1, max_length=32)
    count: int = Field(ge=0)
    alert_revision: int = Field(ge=1)
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_event_id: str
    audit_sequence: int = Field(ge=1)
    previous_event_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: datetime


class OperationsAlertNotificationPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    notifications: tuple[OperationsAlertNotificationRead, ...]
    next_after_sequence: int = Field(ge=0)
    has_more: bool


class GraphControlOperationsMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    pending: int = Field(ge=0)
    processing: int = Field(ge=0)
    applied: int = Field(ge=0)
    superseded: int = Field(ge=0)
    actionable: int = Field(ge=0)
    claim_expired: int = Field(ge=0)
    unrouted: int = Field(ge=0)
    oldest_actionable_at: datetime | None = None


class AdmissionOperationsMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    pending: int = Field(ge=0)
    granted: int = Field(ge=0)
    released: int = Field(ge=0)
    cancelled: int = Field(ge=0)
    withdrawn: int = Field(ge=0)
    expired: int = Field(ge=0)
    live_pending: int = Field(ge=0)
    live_granted: int = Field(ge=0)
    expired_leases: int = Field(ge=0)
    scheduler_revision: int = Field(ge=1)
    next_grant_sequence: int = Field(ge=1)
    configuration_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    global_limit: int | None = Field(default=None, ge=1)
    per_graph_limit: int | None = Field(default=None, ge=1)
    default_tool_limit: int | None = Field(default=None, ge=1)


class ReadyProjectionOperationsMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    projected_graphs: int = Field(ge=0)
    projected_nodes: int = Field(ge=0)
    ready_nodes: int = Field(ge=0)
    missing_live_graphs: int = Field(ge=0)
    event_drift_graphs: int = Field(ge=0)
    row_count_drift_graphs: int = Field(ge=0)
    rebuilds_observed: int = Field(ge=0)
    last_rebuilt_at: datetime | None = None


class OutboxOperationsMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total: int = Field(ge=0)
    pending_ready: int = Field(ge=0)
    retry_scheduled: int = Field(ge=0)
    in_flight: int = Field(ge=0)
    published: int = Field(ge=0)
    dead_lettered: int = Field(ge=0)
    inbox_receipts: int = Field(ge=0)
    oldest_pending_at: datetime | None = None
    oldest_dead_lettered_at: datetime | None = None


class GraphControlOperationsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    control_id: str
    task_id: str
    graph_id: str
    command: str
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: str
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    target_owner_id: str | None = None
    target_fencing_token: int | None = Field(default=None, ge=1)
    claim_owner_id: str | None = None
    claim_fencing_token: int = Field(ge=0)
    claim_expires_at: datetime | None = None
    last_error_code: str | None = None
    updated_at: datetime


class AdmissionOperationsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str
    batch_id: str
    graph_id: str
    node_id: str
    tool_name: str
    owner_id: str
    status: str
    revision: int = Field(ge=1)
    fencing_token: int = Field(ge=0)
    grant_sequence: int | None = Field(default=None, ge=1)
    expires_at: datetime
    updated_at: datetime


class ReadyProjectionOperationsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    graph_status: str
    graph_event_seq: int = Field(ge=1)
    projection_revision: int = Field(ge=1)
    projection_event_seq: int = Field(ge=1)
    content_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    rebuild_count: int = Field(ge=1)
    last_rebuild_duration_ms: int | None = Field(default=None, ge=0)
    projected_nodes: int = Field(ge=0)
    dependency_ready_nodes: int = Field(ge=0)
    rebuilt_at: datetime
    updated_at: datetime


class OutboxOperationsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    task_id: str
    event_id: str
    event_seq: int = Field(ge=1)
    topic: str
    state: Literal["pending", "in_flight", "published", "dead_lettered"]
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    attempt_count: int = Field(ge=0)
    claim_owner_id: str | None = None
    claim_fencing_token: int = Field(ge=0)
    available_at: datetime
    claim_expires_at: datetime | None = None
    published_at: datetime | None = None
    dead_lettered_at: datetime | None = None
    error_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class EffectRuntimeOperationsSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.effect-runtime-operations.v1"] = (
        EFFECT_RUNTIME_OPERATIONS_SCHEMA_VERSION
    )
    database_time: datetime
    graph_controls: GraphControlOperationsMetrics
    admissions: AdmissionOperationsMetrics
    ready_projection: ReadyProjectionOperationsMetrics
    outbox: OutboxOperationsMetrics
    alerts: tuple[OperationsAlert, ...] = ()
    graph_control_samples: tuple[GraphControlOperationsRead, ...] = ()
    admission_samples: tuple[AdmissionOperationsRead, ...] = ()
    ready_projection_samples: tuple[ReadyProjectionOperationsRead, ...] = ()
    outbox_samples: tuple[OutboxOperationsRead, ...] = ()
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EffectRuntimeAuditEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event_id: str
    sequence: int = Field(ge=1)
    action: str = Field(min_length=1, max_length=64)
    actor_id: str = Field(min_length=1, max_length=80)
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_event_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    details: dict[str, object]
    occurred_at: datetime


class EffectRuntimeAuditPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    events: tuple[EffectRuntimeAuditEventRead, ...]
    next_after_sequence: int
    has_more: bool


class EffectRuntimeAuditExportPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.effect-runtime-audit-export.v1"] = (
        "deskpilot.effect-runtime-audit-export.v1"
    )
    export_id: str = Field(pattern=r"^opx_[0-9a-f]{64}$")
    database_time: datetime
    through_sequence: int = Field(ge=0)
    through_event_digest: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    events: tuple[EffectRuntimeAuditEventRead, ...]
    page_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    next_cursor: str | None = None
    has_more: bool


class MetricsAuditResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot: EffectRuntimeOperationsSnapshot
    audit_event: EffectRuntimeAuditEventRead
    alert_notifications: tuple[OperationsAlertNotificationRead, ...] = ()


class RetentionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    retention_days: int | None = Field(default=None, ge=1, le=3_650)


class RetentionCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_controls: int = Field(ge=0)
    admissions: int = Field(ge=0)
    ready_checkpoints: int = Field(ge=0)
    ready_nodes: int = Field(ge=0)
    ready_states: int = Field(ge=0)
    published_outbox: int = Field(ge=0)
    inbox_receipts: int = Field(ge=0)

    @property
    def total(self) -> int:
        return sum(self.model_dump().values())


class RetentionRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cutoff: datetime
    counts: RetentionCounts
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_event: EffectRuntimeAuditEventRead


class OutboxRequeueResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    message_id: str
    attempt_count: int = Field(ge=0)
    claim_fencing_token: int = Field(ge=1)
    available_at: datetime
    audit_event: EffectRuntimeAuditEventRead


__all__ = [
    "EffectRuntimeAuditEventRead",
    "EffectRuntimeAuditExportPage",
    "EffectRuntimeAuditPage",
    "EffectRuntimeOperationsSnapshot",
    "MetricsAuditResult",
    "OutboxRequeueResult",
    "OperationsAlertNotificationPage",
    "OperationsAlertNotificationRead",
    "RetentionRunRequest",
    "RetentionRunResult",
]
