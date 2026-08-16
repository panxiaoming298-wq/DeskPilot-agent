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


class ToolEffectGraphRecord(Base):
    """Mutable projection of one immutable-version Tool effect graph."""

    __tablename__ = "tool_effect_graphs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'compensating', 'succeeded', 'compensated', "
            "'failed', 'cancelled', 'blocked_unknown', 'blocked_non_compensable', "
            "'blocked_compensation_failed', 'blocked_compensation_unknown')",
            name="ck_tool_effect_graphs_status",
        ),
        CheckConstraint(
            "execution_mode IN ('forward', 'compensating')",
            name="ck_tool_effect_graphs_execution_mode",
        ),
        CheckConstraint(
            "revision >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_graphs_positive_versions",
        ),
        UniqueConstraint("task_id", name="uq_tool_effect_graphs_task_id"),
        Index("ix_tool_effect_graphs_status", "status", "updated_at"),
    )

    graph_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.task_id", ondelete="CASCADE"),
    )
    schema_version: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(32))
    execution_mode: Mapped[str] = mapped_column(String(16))
    current_node_id: Mapped[str | None] = mapped_column(String(68), nullable=True)
    failure_node_id: Mapped[str | None] = mapped_column(String(68), nullable=True)
    lease_owner_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    lease_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    cancel_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    revision: Mapped[int] = mapped_column(Integer)
    last_event_seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectGraphControlRecord(Base):
    """Durable command mailbox routed to one live graph owner generation."""

    __tablename__ = "tool_effect_graph_controls"
    __table_args__ = (
        UniqueConstraint(
            "graph_id",
            "command",
            name="uq_tool_effect_graph_controls_command",
        ),
        CheckConstraint(
            "command IN ('cancel')",
            name="ck_tool_effect_graph_controls_command",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'applied', 'superseded')",
            name="ck_tool_effect_graph_controls_status",
        ),
        CheckConstraint(
            "revision >= 1 AND attempt_count >= 0 AND claim_fencing_token >= 0",
            name="ck_tool_effect_graph_controls_positive_versions",
        ),
        CheckConstraint(
            "(target_owner_id IS NULL AND target_fencing_token IS NULL) OR "
            "(target_owner_id IS NOT NULL AND target_fencing_token >= 1)",
            name="ck_tool_effect_graph_controls_target_pair",
        ),
        Index(
            "ix_tool_effect_graph_controls_route",
            "status",
            "target_owner_id",
            "available_at",
            "created_at",
            "control_id",
        ),
        Index(
            "ix_tool_effect_graph_controls_claim_expiry",
            "claim_expires_at",
        ),
    )

    control_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.task_id", ondelete="CASCADE"))
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    command: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(80))
    target_owner_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    target_fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error_code: Mapped[str | None] = mapped_column(String(120), nullable=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_owner_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    claim_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    applied_graph_fencing_token: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolEffectDagAdmissionStateRecord(Base):
    """Admission configuration binding and SQLite-compatible global CAS state."""

    __tablename__ = "tool_effect_dag_admission_state"
    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND next_grant_sequence >= 1",
            name="ck_tool_effect_dag_admission_state_versions",
        ),
        CheckConstraint(
            "(configuration_digest IS NULL AND global_limit IS NULL "
            "AND per_graph_limit IS NULL AND default_tool_limit IS NULL "
            "AND tool_limits_digest IS NULL) OR "
            "(configuration_digest IS NOT NULL AND global_limit >= 1 "
            "AND per_graph_limit >= 1 AND per_graph_limit <= global_limit "
            "AND default_tool_limit >= 1 AND default_tool_limit <= global_limit "
            "AND tool_limits_digest IS NOT NULL)",
            name="ck_tool_effect_dag_admission_state_configuration",
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    next_grant_sequence: Mapped[int] = mapped_column(
        Integer,
        default=1,
        server_default="1",
    )
    configuration_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    global_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    per_graph_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    default_tool_limit: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tool_limits_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectDagAdmissionShardRecord(Base):
    """One independently locked PostgreSQL admission scheduling domain."""

    __tablename__ = "tool_effect_dag_admission_shards"
    __table_args__ = (
        CheckConstraint(
            "shard_id >= 0 AND shard_id < 16 AND revision >= 1 "
            "AND (last_grant_sequence IS NULL OR last_grant_sequence >= 1)",
            name="ck_tool_effect_dag_admission_shards_values",
        ),
        Index(
            "ix_tool_effect_dag_admission_shards_fairness",
            "last_grant_sequence",
            "shard_id",
        ),
    )

    shard_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_grant_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectDagAdmissionRecord(Base):
    """Durable wait ticket that becomes one fenced cluster capacity permit."""

    __tablename__ = "tool_effect_dag_admissions"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "node_id",
            name="uq_tool_effect_dag_admissions_batch_node",
        ),
        CheckConstraint(
            "status IN ('pending', 'granted', 'released', 'cancelled', 'withdrawn', 'expired')",
            name="ck_tool_effect_dag_admissions_status",
        ),
        CheckConstraint(
            "revision >= 1 AND fencing_token >= 0 AND lease_ttl_seconds >= 1 "
            "AND scheduling_shard >= 0 AND scheduling_shard < 16",
            name="ck_tool_effect_dag_admissions_versions",
        ),
        CheckConstraint(
            "(status = 'granted' AND grant_sequence IS NOT NULL "
            "AND fencing_token >= 1 AND granted_at IS NOT NULL "
            "AND heartbeat_at IS NOT NULL) OR status != 'granted'",
            name="ck_tool_effect_dag_admissions_grant",
        ),
        Index(
            "ix_tool_effect_dag_admissions_route",
            "status",
            "expires_at",
            "created_at",
        ),
        Index(
            "ix_tool_effect_dag_admissions_active",
            "status",
            "graph_id",
            "tool_name",
            "expires_at",
        ),
        Index(
            "ix_tool_effect_dag_admissions_owner",
            "owner_id",
            "status",
        ),
        Index(
            "ix_tool_effect_dag_admissions_shard_route",
            "scheduling_shard",
            "status",
            "expires_at",
            "created_at",
            "batch_id",
            "admission_id",
        ),
    )

    admission_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(String(40))
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    node_id: Mapped[str] = mapped_column(String(68))
    tool_name: Mapped[str] = mapped_column(String(200))
    owner_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16))
    scheduling_shard: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    lease_ttl_seconds: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    grant_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    granted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )


class ToolEffectNodeRecord(Base):
    """Stable graph node; raw Tool arguments never enter this projection."""

    __tablename__ = "tool_effect_nodes"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'active', 'waiting_approval', 'running', "
            "'succeeded', 'failed', 'unknown', 'compensating', 'compensated', "
            "'compensation_failed', 'compensation_unknown', 'skipped', 'cancelled')",
            name="ck_tool_effect_nodes_status",
        ),
        CheckConstraint(
            "compensation_strategy IN ('none', 'receipt_bound_reverse')",
            name="ck_tool_effect_nodes_compensation_strategy",
        ),
        CheckConstraint(
            "ordinal >= 0 AND revision >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_nodes_positive_versions",
        ),
        UniqueConstraint("graph_id", "node_key", name="uq_tool_effect_nodes_key"),
        UniqueConstraint("graph_id", "ordinal", name="uq_tool_effect_nodes_ordinal"),
        Index("ix_tool_effect_nodes_graph_status", "graph_id", "status"),
        Index("ix_effect_nodes_claim_expires_at", "claim_expires_at"),
        Index(
            "ix_effect_nodes_graph_claim_expires",
            "graph_id",
            "claim_expires_at",
        ),
    )

    node_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE"),
    )
    node_key: Mapped[str] = mapped_column(String(64))
    ordinal: Mapped[int] = mapped_column(Integer)
    step_id: Mapped[str] = mapped_column(String(128))
    tool_name: Mapped[str] = mapped_column(String(200))
    tool_version: Mapped[str] = mapped_column(String(32))
    contract_digest: Mapped[str] = mapped_column(String(64))
    compensation_strategy: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32))
    revision: Mapped[int] = mapped_column(Integer)
    last_event_seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    claim_owner_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    claim_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ToolEffectEdgeRecord(Base):
    __tablename__ = "tool_effect_edges"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('success', 'conditional', 'compensation_order')",
            name="ck_tool_effect_edges_kind",
        ),
        CheckConstraint(
            "(kind = 'conditional' AND decision_key IS NOT NULL AND "
            "expected_outcome IS NOT NULL) OR "
            "(kind <> 'conditional' AND decision_key IS NULL AND "
            "expected_outcome IS NULL)",
            name="ck_tool_effect_edges_branch_metadata",
        ),
        UniqueConstraint(
            "graph_id",
            "from_node_id",
            "to_node_id",
            "kind",
            name="uq_tool_effect_edges_identity",
        ),
    )

    edge_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE"),
    )
    from_node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
    )
    to_node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
    )
    kind: Mapped[str] = mapped_column(String(32))
    decision_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_outcome: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ToolEffectBranchDecisionRecord(Base):
    """Append-only, content-addressed branch selection proof."""

    __tablename__ = "tool_effect_branch_decisions"
    __table_args__ = (
        CheckConstraint(
            "source_node_revision >= 1 AND source_event_seq >= 1 AND event_seq >= 1",
            name="ck_tool_effect_branch_decisions_positive_versions",
        ),
        UniqueConstraint(
            "graph_id",
            "source_node_id",
            "decision_key",
            name="uq_tool_effect_branch_decisions_key",
        ),
        Index(
            "ix_tool_effect_branch_decisions_graph_event",
            "graph_id",
            "event_seq",
        ),
    )

    decision_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    source_node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE")
    )
    decision_key: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(64))
    evidence_digest: Mapped[str] = mapped_column(String(64))
    source_node_revision: Mapped[int] = mapped_column(Integer)
    source_event_seq: Mapped[int] = mapped_column(Integer)
    proof_digest: Mapped[str] = mapped_column(String(64))
    event_id: Mapped[str] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="CASCADE"), unique=True
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectDagReadyStateRecord(Base):
    """Hash-chained head of one incrementally maintained DAG ready projection."""

    __tablename__ = "tool_effect_dag_ready_states"
    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND event_seq >= 1 "
            "AND membership_version IN (0, 1) "
            "AND projected_node_count >= 0 AND ready_node_count >= 0 "
            "AND ready_node_count <= projected_node_count",
            name="ck_effect_dag_ready_states_versions",
        ),
        Index("ix_effect_dag_ready_states_event", "graph_id", "event_seq"),
    )

    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE"),
        primary_key=True,
    )
    revision: Mapped[int] = mapped_column(Integer)
    event_seq: Mapped[int] = mapped_column(Integer)
    content_digest: Mapped[str] = mapped_column(String(64))
    membership_version: Mapped[int] = mapped_column(Integer, default=1, server_default="0")
    projected_node_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    ready_node_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    rebuild_count: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_rebuild_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rebuilt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectDagReadyNodeRecord(Base):
    """Per-node dependency counters used to query ready pages without a graph scan."""

    __tablename__ = "tool_effect_dag_ready_nodes"
    __table_args__ = (
        CheckConstraint(
            "ordinal >= 0 AND remaining_predecessors >= 0 "
            "AND unresolved_branches >= 0 AND revision >= 1",
            name="ck_effect_dag_ready_nodes_counters",
        ),
        UniqueConstraint(
            "graph_id",
            "ordinal",
            name="uq_effect_dag_ready_nodes_ordinal",
        ),
        Index(
            "ix_effect_dag_ready_nodes_query",
            "graph_id",
            "branch_rejected",
            "remaining_predecessors",
            "unresolved_branches",
            "ordinal",
        ),
        Index(
            "ix_effect_dag_ready_nodes_membership",
            "graph_id",
            "membership_ready",
            "ordinal",
        ),
    )

    node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
        primary_key=True,
    )
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    remaining_predecessors: Mapped[int] = mapped_column(Integer)
    unresolved_branches: Mapped[int] = mapped_column(Integer)
    branch_rejected: Mapped[bool] = mapped_column(Boolean)
    membership_ready: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    revision: Mapped[int] = mapped_column(Integer)
    proof_digest: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectAttemptRecord(Base):
    __tablename__ = "tool_effect_attempts"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('forward', 'compensation')",
            name="ck_tool_effect_attempts_kind",
        ),
        CheckConstraint(
            "status IN ('requested', 'running', 'succeeded', 'failed', 'cancelled', 'unknown')",
            name="ck_tool_effect_attempts_status",
        ),
        CheckConstraint(
            "attempt >= 1 AND last_event_seq >= 1",
            name="ck_tool_effect_attempts_positive_versions",
        ),
        UniqueConstraint(
            "node_id",
            "kind",
            "attempt",
            name="uq_tool_effect_attempts_node_kind_attempt",
        ),
        UniqueConstraint("call_id", name="uq_tool_effect_attempts_call_id"),
        Index("ix_tool_effect_attempts_node_status", "node_id", "status"),
    )

    attempt_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    attempt: Mapped[int] = mapped_column(Integer)
    call_id: Mapped[str] = mapped_column(
        ForeignKey("tool_calls.call_id", ondelete="CASCADE"),
    )
    status: Mapped[str] = mapped_column(String(16))
    effect_id: Mapped[str | None] = mapped_column(String(68), nullable=True)
    last_event_seq: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectRecord(Base):
    __tablename__ = "tool_effects"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('forward', 'compensation')",
            name="ck_tool_effects_kind",
        ),
        CheckConstraint(
            "state IN ('applied', 'compensated', 'compensation_applied')",
            name="ck_tool_effects_state",
        ),
        UniqueConstraint("attempt_id", name="uq_tool_effects_attempt_id"),
        Index("ix_tool_effects_node_state", "node_id", "state"),
    )

    effect_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
    )
    attempt_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_attempts.attempt_id", ondelete="CASCADE"),
    )
    kind: Mapped[str] = mapped_column(String(16))
    state: Mapped[str] = mapped_column(String(32))
    receipt_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_commit_receipts.receipt_id", ondelete="RESTRICT"),
        nullable=True,
    )
    compensates_effect_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_effects.effect_id", ondelete="RESTRICT"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectTransitionRecord(Base):
    """Append-only proof that one node transition and event committed together."""

    __tablename__ = "tool_effect_transitions"
    __table_args__ = (
        UniqueConstraint("event_id", name="uq_tool_effect_transitions_event_id"),
        UniqueConstraint("graph_id", "event_seq", name="uq_tool_effect_transitions_graph_seq"),
        Index("ix_tool_effect_transitions_graph_seq", "graph_id", "event_seq"),
    )

    transition_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE"),
    )
    node_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_nodes.node_id", ondelete="CASCADE"),
    )
    attempt_id: Mapped[str | None] = mapped_column(
        ForeignKey("tool_effect_attempts.attempt_id", ondelete="SET NULL"),
        nullable=True,
    )
    event_id: Mapped[str] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="CASCADE"),
    )
    event_seq: Mapped[int] = mapped_column(Integer)
    transition_kind: Mapped[str] = mapped_column(String(64))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    graph_from_status: Mapped[str] = mapped_column(String(32))
    graph_to_status: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
        UniqueConstraint("delivery_id", name="uq_outbox_delivery_id"),
        Index("ix_outbox_pending", "published_at", "available_at", "created_at"),
        Index("ix_outbox_claim_expires_at", "claim_expires_at"),
        Index("ix_outbox_dead_lettered_at", "dead_lettered_at"),
        Index(
            "ix_outbox_claimable",
            "published_at",
            "available_at",
            "claim_expires_at",
            "created_at",
        ),
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
    delivery_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    delivery_attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dead_lettered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dead_letter_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    claim_owner_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    claim_acquired_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    claim_fencing_token: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class InboxDeliveryRecord(Base):
    """Consumer-side idempotency receipt keyed by logical Outbox message identity."""

    __tablename__ = "inbox_deliveries"
    __table_args__ = (
        UniqueConstraint("consumer_name", "message_id", name="uq_inbox_consumer_message"),
        UniqueConstraint("consumer_name", "delivery_id", name="uq_inbox_consumer_delivery"),
        Index("ix_inbox_processed_at", "processed_at"),
    )

    inbox_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    consumer_name: Mapped[str] = mapped_column(String(96))
    message_id: Mapped[str] = mapped_column(String(40))
    delivery_id: Mapped[str] = mapped_column(String(40))
    topic: Mapped[str] = mapped_column(String(80))
    payload_digest: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EffectRuntimeOperationsStateRecord(Base):
    """Singleton head for the append-only effect-runtime operations audit chain."""

    __tablename__ = "effect_runtime_operations_state"
    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND next_sequence >= 1 AND next_alert_sequence >= 1",
            name="ck_effect_runtime_operations_state_versions",
        ),
    )

    scope_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    next_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    next_alert_sequence: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    last_alert_event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    last_retention_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EffectRuntimeOperationsAuditRecord(Base):
    """Immutable, content-chained audit for metrics, retention and DLQ actions."""

    __tablename__ = "effect_runtime_operations_audit"
    __table_args__ = (
        UniqueConstraint("sequence", name="uq_effect_runtime_operations_audit_sequence"),
        UniqueConstraint(
            "action",
            "idempotency_key_digest",
            name="uq_effect_runtime_operations_audit_idempotency",
        ),
        CheckConstraint(
            "sequence >= 1",
            name="ck_effect_runtime_operations_audit_sequence",
        ),
        Index(
            "ix_effect_runtime_operations_audit_occurred",
            "occurred_at",
            "sequence",
        ),
    )

    event_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    action: Mapped[str] = mapped_column(String(64))
    actor_id: Mapped[str] = mapped_column(String(80))
    idempotency_key_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    request_digest: Mapped[str] = mapped_column(String(64))
    result_digest: Mapped[str] = mapped_column(String(64))
    previous_event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), unique=True)
    details: Mapped[dict[str, Any]] = mapped_column(JSON)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EffectRuntimeAlertStateRecord(Base):
    """Current durable lifecycle projection for one secret-free runtime alert."""

    __tablename__ = "effect_runtime_alert_states"
    __table_args__ = (
        CheckConstraint(
            "revision >= 1 AND count >= 0",
            name="ck_effect_runtime_alert_states_values",
        ),
        Index("ix_effect_runtime_alert_states_active", "active", "updated_at", "alert_code"),
    )

    alert_code: Mapped[str] = mapped_column(String(120), primary_key=True)
    domain: Mapped[str] = mapped_column(String(32))
    severity: Mapped[str] = mapped_column(String(16))
    active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    count: Mapped[int] = mapped_column(Integer)
    revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_snapshot_digest: Mapped[str] = mapped_column(String(64))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class EffectRuntimeAlertNotificationRecord(Base):
    """Append-only hash-chained lifecycle notification for runtime alerts."""

    __tablename__ = "effect_runtime_alert_notifications"
    __table_args__ = (
        CheckConstraint(
            "sequence >= 1 AND count >= 0 AND alert_revision >= 1 AND audit_sequence >= 1",
            name="ck_effect_runtime_alert_notifications_values",
        ),
        UniqueConstraint(
            "sequence",
            name="uq_effect_runtime_alert_notifications_sequence",
        ),
        Index(
            "ix_effect_runtime_alert_notifications_code_sequence",
            "alert_code",
            "sequence",
        ),
    )

    notification_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sequence: Mapped[int] = mapped_column(Integer)
    alert_code: Mapped[str] = mapped_column(String(120))
    transition: Mapped[str] = mapped_column(String(16))
    severity: Mapped[str] = mapped_column(String(16))
    domain: Mapped[str] = mapped_column(String(32))
    count: Mapped[int] = mapped_column(Integer)
    alert_revision: Mapped[int] = mapped_column(Integer)
    snapshot_digest: Mapped[str] = mapped_column(String(64))
    audit_event_id: Mapped[str] = mapped_column(
        ForeignKey("effect_runtime_operations_audit.event_id", ondelete="RESTRICT")
    )
    audit_sequence: Mapped[int] = mapped_column(Integer)
    previous_event_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_digest: Mapped[str] = mapped_column(String(64), unique=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectReadySetCheckpointRecord(Base):
    """Content-addressed proof that a DAG ready-set was valid at one graph revision."""

    __tablename__ = "tool_effect_ready_set_checkpoints"
    __table_args__ = (
        UniqueConstraint(
            "graph_id",
            "graph_revision",
            "proof_digest",
            name="uq_tool_effect_ready_checkpoint_proof",
        ),
        Index(
            "ix_tool_effect_ready_checkpoint_latest",
            "graph_id",
            "graph_revision",
            "created_at",
        ),
    )

    checkpoint_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    graph_revision: Mapped[int] = mapped_column(Integer)
    event_seq: Mapped[int] = mapped_column(Integer)
    ready_node_ids: Mapped[list[str]] = mapped_column(JSON)
    predecessor_proof: Mapped[dict[str, Any]] = mapped_column(JSON)
    proof_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ToolEffectCompensationPlanRecord(Base):
    """Content-addressed reverse-DAG compensation waves at one graph revision."""

    __tablename__ = "tool_effect_compensation_plans"
    __table_args__ = (
        UniqueConstraint(
            "graph_id",
            "graph_revision",
            "proof_digest",
            name="uq_tool_effect_compensation_plan_proof",
        ),
        Index(
            "ix_tool_effect_compensation_plan_latest",
            "graph_id",
            "graph_revision",
            "created_at",
        ),
    )

    plan_id: Mapped[str] = mapped_column(String(68), primary_key=True)
    graph_id: Mapped[str] = mapped_column(
        ForeignKey("tool_effect_graphs.graph_id", ondelete="CASCADE")
    )
    graph_revision: Mapped[int] = mapped_column(Integer)
    event_seq: Mapped[int] = mapped_column(Integer)
    waves: Mapped[list[list[str]]] = mapped_column(JSON)
    proof_digest: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


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
    graph_recovery_status: Mapped[str] = mapped_column(
        String(24), default="not_applicable", server_default="not_applicable"
    )
    graph_recovery_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    graph_recovery_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_events.event_id", ondelete="SET NULL"), nullable=True
    )
    graph_recovered_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
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
