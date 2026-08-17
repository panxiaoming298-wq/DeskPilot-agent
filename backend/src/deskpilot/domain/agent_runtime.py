"""Persistent Agent execution manifests and proof-checked read projections."""

from datetime import datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, BoundAgentRef
from deskpilot.domain.task_plans import (
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    CapabilityRef,
    PlanNodeBudget,
)

RUN_ID_PATTERN = r"^run_[0-9a-f]{64}$"
HANDOFF_ID_PATTERN = r"^hnd_[0-9a-f]{64}$"
INVOCATION_ID_PATTERN = r"^inv_[0-9a-f]{64}$"
MODEL_TURN_ID_PATTERN = r"^amt_[0-9a-f]{64}$"
RESULT_ID_PATTERN = r"^res_[0-9a-f]{64}$"


class ExecutionRunStatus(StrEnum):
    ACTIVE = "active"
    AWAITING_VERIFICATION = "awaiting_verification"
    PAUSED = "paused"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"
    FAILED = "failed"
    SUCCEEDED = "succeeded"


class ExecutionNodeStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    CLAIMED = "claimed"
    RUNNING = "running"
    AWAITING_VERIFICATION = "awaiting_verification"
    VERIFIED = "verified"
    CANCELLED = "cancelled"
    FAILED = "failed"


class InvocationExecutionStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    RESULT_SUBMITTED = "result_submitted"
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_TERMINAL = "failed_terminal"
    CANCELLED = "cancelled"
    EXPIRED = "expired"


class InvocationVerificationStatus(StrEnum):
    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    VERIFIED = "verified"
    REJECTED = "rejected"


class ModelTurnStatus(StrEnum):
    PREPARED = "prepared"
    DISPATCHING = "dispatching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    OUTCOME_UNKNOWN = "outcome_unknown"


class HandoffEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.handoff.v1"] = "deskpilot.handoff.v1"
    handoff_id: str = Field(pattern=HANDOFF_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    target_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    target_agent: BoundAgentRef
    objective_ref: str = Field(min_length=1, max_length=500)
    acceptance_criteria: tuple[str, ...] = Field(max_length=50)
    constraint_refs: tuple[str, ...] = Field(max_length=50)
    allowed_context_sources: tuple[str, ...] = Field(max_length=20)
    capability: CapabilityRef | None = None
    effective_tool_scope_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    budget_allocation: PlanNodeBudget
    parent_invocation_id: str | None = Field(default=None, pattern=INVOCATION_ID_PATTERN)
    handoff_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"handoff_digest"})
        if self.handoff_digest != sha256_digest(material):
            raise ValueError("Handoff digest does not match")
        return self


class AgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-result.v1"] = "deskpilot.agent-result.v1"
    result_id: str = Field(pattern=RESULT_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    disposition: Literal["candidate"] = "candidate"
    claim_ids: tuple[str, ...] = Field(min_length=1, max_length=100)
    citation_ids: tuple[str, ...] = Field(min_length=1, max_length=200)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    model_response_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Agent Result digest does not match")
        return self


class AgentOutputResult(BaseModel):
    """Generic candidate envelope validated against the bound Agent output schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.agent-output-result.v1"] = "deskpilot.agent-output-result.v1"
    result_id: str = Field(pattern=RESULT_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    disposition: Literal["candidate"] = "candidate"
    output: dict[str, object]
    evidence_refs: tuple[str, ...] = Field(min_length=1, max_length=200)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    model_response_digest: str = Field(pattern=DIGEST_PATTERN)
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    result_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"result_digest"})
        if self.result_digest != sha256_digest(material):
            raise ValueError("Agent Output Result digest does not match")
        return self


class ExecutionNodeRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    local_key: str
    status: ExecutionNodeStatus
    revision: int = Field(ge=1)
    attempt_count: int = Field(ge=0)
    claim_owner_id: str | None
    claim_fencing_token: int = Field(ge=0)
    claim_expires_at: datetime | None
    bound_agent: BoundAgentRef | None
    runtime_enabled: bool


class AgentInvocationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    attempt: int = Field(ge=1)
    handoff_id: str = Field(pattern=HANDOFF_ID_PATTERN)
    agent: BoundAgentRef
    execution_status: InvocationExecutionStatus
    verification_status: InvocationVerificationStatus
    result_id: str | None = Field(default=None, pattern=RESULT_ID_PATTERN)
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AgentModelTurnRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    turn_id: str = Field(pattern=MODEL_TURN_ID_PATTERN)
    invocation_id: str = Field(pattern=INVOCATION_ID_PATTERN)
    turn_no: int = Field(ge=1)
    status: ModelTurnStatus
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    response_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    provider_id: str | None
    model: str | None
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cost_micros: int = Field(ge=0)
    stable_error_code: str | None


class ClaimedInvocation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    handoff: HandoffEnvelope
    invocation: AgentInvocationRead
    claim_owner_id: str
    claim_fencing_token: int = Field(ge=1)
    claim_expires_at: datetime


class ExecutionRunRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    plan_digest: str = Field(pattern=DIGEST_PATTERN)
    status: ExecutionRunStatus
    revision: int = Field(ge=1)
    nodes: tuple[ExecutionNodeRead, ...]
    invocations: tuple[AgentInvocationRead, ...]
    created_at: datetime
    updated_at: datetime


class ExecutionRunPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    runs: tuple[ExecutionRunRead, ...]
