"""Versioned, queryable identities for durable Tool effects and saga transitions."""

import hashlib
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EFFECT_GRAPH_SCHEMA_VERSION: Literal["deskpilot.tool-effect-graph.v1"] = (
    "deskpilot.tool-effect-graph.v1"
)
EFFECT_DAG_SCHEMA_VERSION: Literal["deskpilot.tool-effect-graph.v2"] = (
    "deskpilot.tool-effect-graph.v2"
)
EFFECT_DAG_MAX_NODES = 1_000
EFFECT_DAG_MAX_PREDECESSORS = 128
EFFECT_DAG_MAX_DEPENDENCIES = 10_000
EffectGraphSchemaVersion = Literal[
    "deskpilot.tool-effect-graph.v1",
    "deskpilot.tool-effect-graph.v2",
]


class EffectGraphStatus(StrEnum):
    ACTIVE = "active"
    COMPENSATING = "compensating"
    SUCCEEDED = "succeeded"
    COMPENSATED = "compensated"
    FAILED = "failed"
    CANCELLED = "cancelled"
    BLOCKED_UNKNOWN = "blocked_unknown"
    BLOCKED_NON_COMPENSABLE = "blocked_non_compensable"
    BLOCKED_COMPENSATION_FAILED = "blocked_compensation_failed"
    BLOCKED_COMPENSATION_UNKNOWN = "blocked_compensation_unknown"


class EffectNodeStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    WAITING_APPROVAL = "waiting_approval"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNKNOWN = "unknown"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    COMPENSATION_FAILED = "compensation_failed"
    COMPENSATION_UNKNOWN = "compensation_unknown"
    SKIPPED = "skipped"
    CANCELLED = "cancelled"


class EffectAttemptKind(StrEnum):
    FORWARD = "forward"
    COMPENSATION = "compensation"


class EffectAttemptStatus(StrEnum):
    REQUESTED = "requested"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


class EffectState(StrEnum):
    APPLIED = "applied"
    COMPENSATED = "compensated"
    COMPENSATION_APPLIED = "compensation_applied"


class EffectEdgeKind(StrEnum):
    SUCCESS = "success"
    CONDITIONAL = "conditional"
    COMPENSATION_ORDER = "compensation_order"


class CompensationStrategy(StrEnum):
    NONE = "none"
    RECEIPT_BOUND_REVERSE = "receipt_bound_reverse"


class EffectExecutionMode(StrEnum):
    FORWARD = "forward"
    COMPENSATING = "compensating"


class EffectGraphLeaseRead(BaseModel):
    """Current database-backed graph ownership and monotonic fence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    owner_id: str = Field(min_length=1, max_length=80)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


def _stable_identity(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(material).hexdigest()}"


def effect_graph_id(
    task_id: str,
    schema_version: EffectGraphSchemaVersion = EFFECT_GRAPH_SCHEMA_VERSION,
) -> str:
    return _stable_identity("teg", schema_version, task_id)


def effect_node_id(graph_id: str, node_key: str) -> str:
    return _stable_identity("ten", graph_id, node_key)


def effect_edge_id(
    graph_id: str,
    from_node_id: str,
    to_node_id: str,
    kind: EffectEdgeKind,
    decision_key: str | None = None,
    expected_outcome: str | None = None,
) -> str:
    if kind is EffectEdgeKind.CONDITIONAL:
        if decision_key is None or expected_outcome is None:
            raise ValueError("Conditional effect edges require a decision and outcome")
        return _stable_identity(
            "tee",
            graph_id,
            from_node_id,
            to_node_id,
            kind.value,
            decision_key,
            expected_outcome,
        )
    if decision_key is not None or expected_outcome is not None:
        raise ValueError("Only conditional effect edges may carry branch metadata")
    return _stable_identity("tee", graph_id, from_node_id, to_node_id, kind.value)


def effect_branch_decision_id(proof_digest: str) -> str:
    if len(proof_digest) != 64:
        raise ValueError("Effect branch decision proof digest is invalid")
    return f"tbd_{proof_digest}"


def effect_attempt_id(
    node_id: str,
    kind: EffectAttemptKind,
    attempt: int = 1,
) -> str:
    if attempt < 1:
        raise ValueError("Effect attempt number must be positive")
    return _stable_identity("tea", node_id, kind.value, attempt)


def effect_call_id(
    node_id: str,
    kind: EffectAttemptKind,
    attempt: int = 1,
) -> str:
    return _stable_identity("call", effect_attempt_id(node_id, kind, attempt))


def tool_effect_id(attempt_id: str) -> str:
    return _stable_identity("tef", attempt_id)


class EffectEdgeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    edge_id: str
    from_node_id: str
    to_node_id: str
    kind: EffectEdgeKind
    decision_key: str | None = None
    expected_outcome: str | None = None


class EffectAttemptRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt_id: str
    kind: EffectAttemptKind
    attempt: int = Field(ge=1)
    call_id: str
    status: EffectAttemptStatus
    effect_id: str | None = None
    last_event_seq: int = Field(ge=1)


class ToolEffectRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    effect_id: str
    attempt_id: str
    kind: EffectAttemptKind
    state: EffectState
    receipt_id: str | None = None
    compensates_effect_id: str | None = None


class EffectNodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    node_key: str
    ordinal: int = Field(ge=0)
    step_id: str
    tool_name: str
    tool_version: str
    contract_digest: str
    compensation_strategy: CompensationStrategy
    status: EffectNodeStatus
    revision: int = Field(ge=1)
    last_event_seq: int = Field(ge=1)
    claim_owner_id: str | None = None
    claim_acquired_at: datetime | None = None
    claim_heartbeat_at: datetime | None = None
    claim_expires_at: datetime | None = None
    claim_fencing_token: int = Field(ge=0)
    attempts: tuple[EffectAttemptRead, ...] = ()
    effects: tuple[ToolEffectRead, ...] = ()


class EffectTransitionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transition_id: str
    node_id: str
    attempt_id: str | None = None
    event_id: str
    event_seq: int = Field(ge=1)
    transition_kind: str
    from_status: EffectNodeStatus
    to_status: EffectNodeStatus
    graph_from_status: EffectGraphStatus
    graph_to_status: EffectGraphStatus
    created_at: datetime


class EffectNodeDefinition(BaseModel):
    """Trusted application projection used to persist an immutable graph definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    node_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    step_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    tool_name: str = Field(min_length=1, max_length=200)
    tool_version: str = Field(min_length=1, max_length=32)
    contract_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    compensation_strategy: CompensationStrategy


class EffectDagBranchCondition(BaseModel):
    """Immutable branch gate controlled by one predecessor decision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    predecessor_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    decision_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    expected_outcome: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")


class EffectBranchDecisionProof(BaseModel):
    """Immutable decision fact embedded in a ready-set proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str
    source_node_id: str
    source_node_key: str
    decision_key: str
    outcome: str
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_node_revision: int = Field(ge=1)
    source_event_seq: int = Field(ge=1)
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EffectBranchDecisionRead(EffectBranchDecisionProof):
    """Content-addressed persisted branch decision and journal position."""

    graph_id: str
    event_seq: int = Field(ge=1)
    created_at: datetime


class EffectDagNodeDefinition(EffectNodeDefinition):
    """Trusted v2 DAG node with immutable predecessor keys and branch gates."""

    depends_on: tuple[str, ...] = Field(
        default=(),
        max_length=EFFECT_DAG_MAX_PREDECESSORS,
    )
    conditional_depends_on: tuple[EffectDagBranchCondition, ...] = Field(
        default=(),
        max_length=EFFECT_DAG_MAX_PREDECESSORS,
    )


class EffectGraphRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    task_id: str
    schema_version: EffectGraphSchemaVersion
    status: EffectGraphStatus
    execution_mode: EffectExecutionMode
    current_node_id: str | None = None
    failure_node_id: str | None = None
    fencing_token: int = Field(ge=0)
    lease_owner_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_heartbeat_at: datetime | None = None
    lease_expires_at: datetime | None = None
    cancel_requested_at: datetime | None = None
    revision: int = Field(ge=1)
    last_event_seq: int = Field(ge=1)
    nodes: tuple[EffectNodeRead, ...]
    edges: tuple[EffectEdgeRead, ...]
    branch_decisions: tuple[EffectBranchDecisionRead, ...] = ()
    transitions: tuple[EffectTransitionRead, ...]


class EffectPredecessorProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    node_key: str
    status: EffectNodeStatus
    revision: int = Field(ge=1)
    last_event_seq: int = Field(ge=1)


class EffectReadyNodeProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    node_id: str
    node_key: str
    ordinal: int = Field(ge=0)
    status: EffectNodeStatus
    revision: int = Field(ge=1)
    last_event_seq: int = Field(ge=1)
    prior_claim_fencing_token: int = Field(ge=0)
    prior_claim_expires_at: datetime | None = None
    predecessors: tuple[EffectPredecessorProof, ...] = ()
    branch_decisions: tuple[EffectBranchDecisionProof, ...] = ()


class EffectReadySetCheckpointRead(BaseModel):
    """Durable, content-addressed page of one DAG ready-set and its joins."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    checkpoint_id: str
    graph_id: str
    graph_revision: int = Field(ge=1)
    graph_fencing_token: int = Field(ge=1)
    event_seq: int = Field(ge=1)
    projection_revision: int = Field(ge=1)
    projection_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    ready_nodes: tuple[EffectReadyNodeProof, ...]
    ready_set_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    cursor: str | None = Field(default=None, pattern=r"^ter_[0-9a-f]{64}$")
    page_size: int = Field(ge=1, le=1_000)
    after_ordinal: int | None = Field(default=None, ge=0)
    last_ordinal: int | None = Field(default=None, ge=0)
    next_cursor: str | None = Field(default=None, pattern=r"^ter_[0-9a-f]{64}$")
    total_ready: int = Field(ge=0)
    has_more: bool
    database_time: datetime
    created_at: datetime


class EffectDagAdmissionProof(BaseModel):
    """Exact cluster capacity grant validated in the node-claim transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    admission_id: str = Field(pattern=r"^eda_[0-9a-f]{32}$")
    owner_id: str = Field(min_length=1, max_length=80)
    fencing_token: int = Field(ge=1)


class EffectNodeClaimRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    node_id: str
    node_key: str
    owner_id: str = Field(min_length=1, max_length=96)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime
    ready_proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class EffectNodeLeaseRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    graph_id: str
    node_id: str
    owner_id: str = Field(min_length=1, max_length=96)
    fencing_token: int = Field(ge=1)
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


class EffectCompensationWaveRead(BaseModel):
    """One parallel-safe wave in a reverse dependency compensation plan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=0)
    node_ids: tuple[str, ...] = Field(min_length=1)


class EffectCompensationPlanRead(BaseModel):
    """Durable content-addressed proof of the reverse DAG execution order."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_id: str
    graph_id: str
    graph_revision: int = Field(ge=1)
    event_seq: int = Field(ge=1)
    waves: tuple[EffectCompensationWaveRead, ...]
    proof_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime
