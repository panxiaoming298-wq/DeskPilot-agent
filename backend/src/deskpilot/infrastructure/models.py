"""Persistence models. They deliberately stay outside the domain package."""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TaskRecord(Base):
    __tablename__ = "tasks"

    task_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    conversation_id: Mapped[str | None] = mapped_column(String(40), nullable=True, index=True)
    goal: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), index=True)
    mode: Mapped[str] = mapped_column(String(32), default="fake")
    privacy_mode: Mapped[str] = mapped_column(String(32))
    constraints: Mapped[list[str]] = mapped_column(JSON, default=list)
    last_event_seq: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=utc_now,
        onupdate=utc_now,
    )

    events: Mapped[list["TaskEventRecord"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    tool_calls: Mapped[list["ToolCallRecord"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    approvals: Mapped[list["ApprovalRecord"]] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
    )
    runtime_checkpoint: Mapped["TaskRuntimeCheckpointRecord | None"] = relationship(
        back_populates="task",
        cascade="all, delete-orphan",
        uselist=False,
    )


class TaskRuntimeCheckpointRecord(Base):
    """Current protected runtime snapshot bound to one task event sequence."""

    __tablename__ = "task_runtime_checkpoints"
    __table_args__ = (
        CheckConstraint(
            "next_stage >= 0 AND next_stage <= 8",
            name="ck_task_runtime_checkpoints_next_stage",
        ),
        CheckConstraint(
            "event_seq >= 1 AND revision >= 1",
            name="ck_task_runtime_checkpoints_positive_versions",
        ),
        Index("ix_task_runtime_checkpoints_stage", "next_stage", "updated_at"),
    )

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        primary_key=True,
    )
    schema_version: Mapped[str] = mapped_column(String(64))
    next_stage: Mapped[int] = mapped_column(Integer)
    event_seq: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    protection_scheme: Mapped[str] = mapped_column(String(64))
    protected_payload: Mapped[bytes] = mapped_column(LargeBinary)
    payload_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)

    task: Mapped[TaskRecord] = relationship(back_populates="runtime_checkpoint")


class TaskEventRecord(Base):
    __tablename__ = "task_events"
    __table_args__ = (UniqueConstraint("task_id", "seq", name="uq_task_event_seq"),)

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        index=True,
    )
    seq: Mapped[int] = mapped_column(Integer)
    type: Mapped[str] = mapped_column(String(80), index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    trace_id: Mapped[str] = mapped_column(String(40), index=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    task: Mapped[TaskRecord] = relationship(back_populates="events")


class OutboxMessageRecord(Base):
    """Durable broker message written in the same transaction as its task event."""

    __tablename__ = "outbox_messages"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_outbox_event_id"),
        Index("ix_outbox_pending", "published_at", "available_at", "created_at"),
    )

    message_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
        index=True,
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="CASCADE"),
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    topic: Mapped[str] = mapped_column(String(80))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ToolCallRecord(Base):
    """Durable execution ledger for calls crossing the Runner process boundary."""

    __tablename__ = "tool_calls"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')",
            name="ck_tool_calls_status",
        ),
        CheckConstraint(
            "policy_effect IS NULL OR policy_effect IN ('allow', 'deny', 'require_approval')",
            name="ck_tool_calls_policy_effect",
        ),
        UniqueConstraint(
            "task_id",
            "step_id",
            "attempt",
            name="uq_tool_calls_task_step_attempt",
        ),
        UniqueConstraint("terminal_event_id", name="uq_tool_calls_terminal_event_id"),
        Index("ix_tool_calls_task_status", "task_id", "status"),
        Index("ix_tool_calls_recovery", "status", "updated_at"),
    )

    call_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
    )
    step_id: Mapped[str] = mapped_column(String(128))
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(32))
    contract_digest: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    policy_decision_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    policy_revision: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_effect: Mapped[str | None] = mapped_column(String(32), nullable=True)
    resource_scope_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    policy_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="SET NULL"),
        nullable=True,
    )
    authorization_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    idempotency: Mapped[str] = mapped_column(String(32))
    idempotency_key_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    runner_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolution_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    terminal_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    task: Mapped[TaskRecord] = relationship(back_populates="tool_calls")
    approval: Mapped["ApprovalRecord | None"] = relationship(
        back_populates="tool_call",
        uselist=False,
    )


class ToolIdempotencyReceiptRecord(Base):
    """Durable ownership receipt for a Tool idempotency-key digest."""

    __tablename__ = "tool_idempotency_receipts"
    __table_args__ = (
        UniqueConstraint(
            "tool_name",
            "tool_version",
            "key_digest",
            name="uq_tool_idempotency_receipts_scope_key",
        ),
        UniqueConstraint("call_id", name="uq_tool_idempotency_receipts_call_id"),
    )

    receipt_id: Mapped[str] = mapped_column(String(140), primary_key=True)
    call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="CASCADE"),
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(32))
    key_digest: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolCommitReceiptRecord(Base):
    """Control-plane projection of a Runner-durable external commit receipt."""

    __tablename__ = "tool_commit_receipts"
    __table_args__ = (UniqueConstraint("call_id", name="uq_tool_commit_receipts_call_id"),)

    receipt_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="CASCADE"),
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(32))
    authorization_id: Mapped[str] = mapped_column(String(80))
    approval_id: Mapped[str] = mapped_column(String(128))
    preview_hash: Mapped[str] = mapped_column(String(64))
    prepare_digest: Mapped[str] = mapped_column(String(64))
    idempotency_key_digest: Mapped[str] = mapped_column(String(64))
    resource_versions_before: Mapped[dict[str, str]] = mapped_column(JSON)
    resource_versions_after: Mapped[dict[str, str]] = mapped_column(JSON)
    commit_started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    receipt_recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    projected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolReconciliationRecord(Base):
    """Immutable manual verdict layered over an unknown Tool call."""

    __tablename__ = "tool_reconciliations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'resolved')",
            name="ck_tool_reconciliations_status",
        ),
        CheckConstraint(
            "outcome IS NULL OR outcome IN ("
            "'confirmed_succeeded', 'confirmed_failed', "
            "'confirmed_no_effect', 'accepted_unknown')",
            name="ck_tool_reconciliations_outcome",
        ),
        CheckConstraint(
            "(status = 'pending' AND outcome IS NULL AND resolved_at IS NULL) OR "
            "(status = 'resolved' AND outcome IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_tool_reconciliations_resolution",
        ),
        UniqueConstraint("call_id", name="uq_tool_reconciliations_call_id"),
        UniqueConstraint(
            "new_attempt_task_id",
            name="uq_tool_reconciliations_new_attempt_task_id",
        ),
        UniqueConstraint(
            "compensation_task_id",
            name="uq_tool_reconciliations_compensation_task_id",
        ),
        Index(
            "ix_tool_reconciliations_status_unknown_at",
            "status",
            "unknown_at",
        ),
        Index(
            "ix_tool_reconciliations_task_status",
            "task_id",
            "status",
        ),
    )

    reconciliation_id: Mapped[str] = mapped_column(String(140), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
    )
    call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="CASCADE"),
    )
    status: Mapped[str] = mapped_column(String(16))
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    evidence_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    unknown_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    new_attempt_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
        nullable=True,
    )
    new_attempt_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    compensation_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
        nullable=True,
    )
    compensation_receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_commit_receipts.receipt_id", ondelete="SET NULL"),
        nullable=True,
    )
    compensation_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolReconciliationEvidenceRecord(Base):
    """Append-only signed-query evidence layered over an unknown Tool call."""

    __tablename__ = "tool_reconciliation_evidence"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('commit_receipt', 'no_receipt', 'query_failed')",
            name="ck_tool_reconciliation_evidence_kind",
        ),
        CheckConstraint(
            "(kind = 'commit_receipt' AND receipt_id IS NOT NULL "
            "AND error_code IS NULL) OR "
            "(kind = 'no_receipt' AND receipt_id IS NULL "
            "AND error_code IS NULL) OR "
            "(kind = 'query_failed' AND receipt_id IS NULL "
            "AND error_code IS NOT NULL)",
            name="ck_tool_reconciliation_evidence_payload",
        ),
        UniqueConstraint(
            "reconciliation_id",
            "evidence_digest",
            name="uq_tool_reconciliation_evidence_digest",
        ),
        Index(
            "ix_tool_reconciliation_evidence_observed",
            "reconciliation_id",
            "observed_at",
        ),
    )

    evidence_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_reconciliations.reconciliation_id", ondelete="CASCADE"),
    )
    evidence_digest: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(32))
    queried_runner_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_commit_receipts.receipt_id", ondelete="CASCADE"),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolReconciliationIdempotencyRecord(Base):
    """Persistent receipt for reconciliation write API requests."""

    __tablename__ = "tool_reconciliation_idempotency_records"
    __table_args__ = (
        Index(
            "ix_tool_reconciliation_idempotency_reconciliation",
            "reconciliation_id",
            "created_at",
        ),
    )

    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(80))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    reconciliation_id: Mapped[str] = mapped_column(
        ForeignKey("tool_reconciliations.reconciliation_id", ondelete="CASCADE"),
    )
    created_task_id: Mapped[str | None] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ApprovalRecord(Base):
    """Durable, exact-binding user approval for one Tool Runner call."""

    __tablename__ = "approvals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired', 'cancelled')",
            name="ck_approvals_status",
        ),
        CheckConstraint(
            "decision IS NULL OR decision IN ('approved', 'rejected')",
            name="ck_approvals_decision",
        ),
        CheckConstraint(
            "policy_decision IN ('allow', 'deny', 'require_approval')",
            name="ck_approvals_policy_decision",
        ),
        CheckConstraint(
            "scope IS NULL OR scope = 'once'",
            name="ck_approvals_scope",
        ),
        UniqueConstraint("call_id", name="uq_approvals_call_id"),
        Index("ix_approvals_status_expires_at", "status", "expires_at"),
        Index("ix_approvals_task_status", "task_id", "status"),
    )

    approval_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    decision_id: Mapped[str] = mapped_column(String(80))
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
    )
    call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="CASCADE"),
    )
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(32))
    risk_level: Mapped[str] = mapped_column(String(2))
    policy_decision: Mapped[str] = mapped_column(String(16))
    policy_rule_id: Mapped[str] = mapped_column(String(100))
    policy_revision: Mapped[str] = mapped_column(String(64))
    reason_code: Mapped[str] = mapped_column(String(100))
    contract_digest: Mapped[str] = mapped_column(String(64))
    arguments_digest: Mapped[str] = mapped_column(String(64))
    binding_digest: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(Text)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list)
    resource_scope: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    consequences: Mapped[list[str]] = mapped_column(JSON, default=list)
    reversible: Mapped[bool] = mapped_column(Boolean)
    data_egress: Mapped[dict[str, Any]] = mapped_column(JSON)
    expected_resource_versions: Mapped[dict[str, str]] = mapped_column(
        JSON,
        default=dict,
    )
    preview_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    scope: Mapped[str | None] = mapped_column(String(16), nullable=True)
    resolved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    resolution_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    task: Mapped[TaskRecord] = relationship(back_populates="approvals")
    tool_call: Mapped[ToolCallRecord] = relationship(back_populates="approval")


class ProviderCatalogStateRecord(Base):
    """Singleton metadata for the imported public Provider catalog projection."""

    __tablename__ = "model_provider_catalog_state"

    catalog_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer)
    default_provider_id: Mapped[str] = mapped_column(String(64))
    content_digest: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    entries: Mapped[list["ProviderCatalogEntryRecord"]] = relationship(
        back_populates="catalog",
        cascade="all, delete-orphan",
    )


class ProviderCatalogEntryRecord(Base):
    """Public descriptor only; endpoints and credential references are excluded."""

    __tablename__ = "model_provider_catalog_entries"
    __table_args__ = (
        UniqueConstraint(
            "catalog_id",
            "ordinal",
            name="uq_model_provider_catalog_entry_ordinal",
        ),
        Index("ix_model_provider_catalog_entries_enabled", "enabled"),
    )

    catalog_id: Mapped[str] = mapped_column(
        ForeignKey("model_provider_catalog_state.catalog_id", ondelete="CASCADE"),
        primary_key=True,
    )
    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    descriptor: Mapped[dict[str, Any]] = mapped_column(JSON)
    enabled: Mapped[bool] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    catalog: Mapped[ProviderCatalogStateRecord] = relationship(back_populates="entries")


class ProviderRuntimeConfigRecord(Base):
    """DPAPI-protected runtime adapter configuration; no secret is stored here."""

    __tablename__ = "model_provider_runtime_configs"

    provider_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    config_kind: Mapped[str] = mapped_column(String(64))
    payload_schema_version: Mapped[int] = mapped_column(Integer)
    protection_scheme: Mapped[str] = mapped_column(String(64))
    protected_payload: Mapped[bytes] = mapped_column(LargeBinary)
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderConfigAuditEventRecord(Base):
    """Append-only, value-free history of runtime configuration mutations."""

    __tablename__ = "model_provider_config_audit_events"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_model_provider_config_audit_event_id"),
        Index(
            "ix_model_provider_config_audit_provider_sequence",
            "provider_id",
            "sequence",
        ),
        Index("ix_model_provider_config_audit_occurred_at", "occurred_at"),
    )

    sequence: Mapped[int] = mapped_column(
        Integer,
        primary_key=True,
        autoincrement=True,
    )
    event_id: Mapped[str] = mapped_column(String(40))
    provider_id: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(32))
    actor_type: Mapped[str] = mapped_column(String(32))
    config_revision: Mapped[int] = mapped_column(Integer)
    changed_fields: Mapped[list[str]] = mapped_column(JSON)
    credential_disposition: Mapped[str] = mapped_column(String(64))
    correlation_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderIdempotencyRecord(Base):
    """Persistent replay receipt; raw keys and sensitive request values are excluded."""

    __tablename__ = "model_provider_idempotency_records"
    __table_args__ = (Index("ix_model_provider_idempotency_expires_at", "expires_at"),)

    key_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    operation: Mapped[str] = mapped_column(String(80))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
