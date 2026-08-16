"""Protected, versioned state needed to resume the trusted single-Tool graph."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.effect_graph import (
    EFFECT_DAG_SCHEMA_VERSION,
    EFFECT_GRAPH_SCHEMA_VERSION,
    EffectExecutionMode,
)
from deskpilot.domain.planning import TaskClassification, TaskPlan
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationRequest,
)
from deskpilot.domain.reconciliations import ReconciliationOutcome
from deskpilot.domain.schemas import (
    DiskPressureGuardedFileMoveRequest,
    FileMoveCompensationRequest,
    FileMoveDagRequest,
    FileMoveSagaRequest,
    FileMoveTaskRequest,
)

TaskToolRequest = (
    FileMoveTaskRequest
    | FileMoveSagaRequest
    | FileMoveDagRequest
    | DiskPressureGuardedFileMoveRequest
    | FileMoveCompensationRequest
)


def initial_tool_call_id(task_id: str) -> str:
    """Derive the stable pre-dispatch call identity without storing another secret."""
    if not task_id.startswith("tsk_") or len(task_id) != 36:
        raise ValueError("Task ID cannot produce a stable Tool call identity")
    return f"call-{task_id[4:]}"


class TaskCheckpointPayload(BaseModel):
    """Encrypted runtime facts; event_seq is stored beside the ciphertext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-checkpoint.v1"] = "deskpilot.task-checkpoint.v1"
    task_id: str = Field(min_length=1, max_length=40)
    next_stage: int = Field(ge=0, le=8)
    tool_call_id: str = Field(min_length=1, max_length=128)
    tool_request: TaskToolRequest | None = None
    classification: TaskClassification | None = None
    plan: TaskPlan | None = None
    planner_provider_id: str | None = Field(default=None, min_length=1, max_length=128)
    tool_arguments: dict[str, str] | None = None
    tool_resources: tuple[PolicyResource, ...] = ()
    expected_resource_versions: dict[str, str] = Field(default_factory=dict)
    tool_idempotency_key: str | None = Field(default=None, min_length=16, max_length=256)
    policy_request: ToolAuthorizationRequest | None = None
    policy_decision: PolicyDecision | None = None
    approval_id: str | None = Field(default=None, min_length=1, max_length=128)
    dag_approval_ids: tuple[str, ...] = Field(default=(), max_length=20)
    graph_id: str | None = Field(default=None, pattern=r"^teg_[0-9a-f]{64}$")
    graph_schema_version: str | None = Field(default=None, max_length=64)
    graph_fencing_token: int | None = Field(default=None, ge=1)
    current_node_id: str | None = Field(default=None, pattern=r"^ten_[0-9a-f]{64}$")
    current_node_index: int = Field(default=0, ge=0, le=19)
    execution_mode: EffectExecutionMode = EffectExecutionMode.FORWARD
    failure_node_id: str | None = Field(default=None, pattern=r"^ten_[0-9a-f]{64}$")
    reconciled_call_id: str | None = Field(default=None, min_length=1, max_length=128)
    reconciled_outcome: ReconciliationOutcome | None = None

    @model_validator(mode="after")
    def validate_stage_facts(self) -> Self:
        if self.next_stage >= 2 and (self.classification is None or self.plan is None):
            raise ValueError("planned checkpoint stages require classification and plan")
        if self.next_stage >= 5 and (self.tool_arguments is None or not self.tool_resources):
            raise ValueError("requested Tool stages require arguments and resources")
        if self.next_stage >= 6 and (self.policy_request is None or self.policy_decision is None):
            raise ValueError("authorized Tool stages require policy facts")
        if (
            self.policy_decision is not None
            and self.policy_decision.effect is PolicyEffect.REQUIRE_APPROVAL
            and self.next_stage >= 6
            and self.approval_id is None
        ):
            raise ValueError("approval checkpoint is missing its approval identity")
        if self.tool_request is not None and self.next_stage >= 5:
            if self.tool_idempotency_key is None:
                raise ValueError("writable Tool checkpoint is missing its idempotency key")
        elif self.tool_request is None and self.tool_idempotency_key is not None:
            raise ValueError("read-only checkpoint cannot contain a Tool idempotency key")
        graph_identity = (self.graph_id, self.graph_schema_version)
        if any(value is not None for value in graph_identity) and not all(
            value is not None for value in graph_identity
        ):
            raise ValueError("effect graph checkpoint identity must be complete")
        if self.graph_schema_version is not None and self.graph_schema_version not in {
            EFFECT_GRAPH_SCHEMA_VERSION,
            EFFECT_DAG_SCHEMA_VERSION,
        }:
            raise ValueError("effect graph checkpoint version is unsupported")
        if self.graph_schema_version == EFFECT_GRAPH_SCHEMA_VERSION and (
            self.graph_fencing_token is None or self.current_node_id is None
        ):
            raise ValueError("v1 effect graph checkpoint is missing its active node lease")
        if self.graph_schema_version == EFFECT_DAG_SCHEMA_VERSION:
            if self.current_node_id is not None or self.graph_fencing_token is not None:
                raise ValueError("v2 DAG checkpoint cannot retain a dispatcher claim")
            if self.approval_id is not None:
                raise ValueError("v2 DAG approvals must use dag_approval_ids")
        if len(self.dag_approval_ids) != len(set(self.dag_approval_ids)):
            raise ValueError("v2 DAG approval identities must be unique")
        if self.dag_approval_ids and self.graph_schema_version != EFFECT_DAG_SCHEMA_VERSION:
            raise ValueError("v2 DAG approval identities require a v2 graph")
        if self.execution_mode is EffectExecutionMode.COMPENSATING:
            if self.graph_id is None or self.failure_node_id is None:
                raise ValueError("compensating checkpoint is missing saga failure identity")
        if (self.reconciled_call_id is None) != (self.reconciled_outcome is None):
            raise ValueError("reconciled checkpoint binding must be complete")
        if self.reconciled_call_id is not None and self.reconciled_call_id != self.tool_call_id:
            raise ValueError("reconciled checkpoint call does not match current Tool call")
        return self


class DurableTaskCheckpoint(BaseModel):
    """Decrypted checkpoint plus its exact durable event-stream binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: TaskCheckpointPayload
    event_seq: int = Field(ge=1)
    revision: int = Field(ge=1)
