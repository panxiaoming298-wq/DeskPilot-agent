"""Protected, versioned state needed to resume the trusted single-Tool graph."""

from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.planning import TaskClassification, TaskPlan
from deskpilot.domain.policy import (
    PolicyDecision,
    PolicyEffect,
    PolicyResource,
    ToolAuthorizationRequest,
)
from deskpilot.domain.schemas import FileMoveCompensationRequest, FileMoveTaskRequest

TaskToolRequest = FileMoveTaskRequest | FileMoveCompensationRequest


def initial_tool_call_id(task_id: str) -> str:
    """Derive the stable pre-dispatch call identity without storing another secret."""
    if not task_id.startswith("tsk_") or len(task_id) != 36:
        raise ValueError("Task ID cannot produce a stable Tool call identity")
    return f"call-{task_id[4:]}"


class TaskCheckpointPayload(BaseModel):
    """Encrypted runtime facts; event_seq is stored beside the ciphertext."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-checkpoint.v1"] = (
        "deskpilot.task-checkpoint.v1"
    )
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

    @model_validator(mode="after")
    def validate_stage_facts(self) -> Self:
        if self.next_stage >= 2 and (
            self.classification is None or self.plan is None
        ):
            raise ValueError("planned checkpoint stages require classification and plan")
        if self.next_stage >= 5 and (
            self.tool_arguments is None or not self.tool_resources
        ):
            raise ValueError("requested Tool stages require arguments and resources")
        if self.next_stage >= 6 and (
            self.policy_request is None or self.policy_decision is None
        ):
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
        return self


class DurableTaskCheckpoint(BaseModel):
    """Decrypted checkpoint plus its exact durable event-stream binding."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: TaskCheckpointPayload
    event_seq: int = Field(ge=1)
    revision: int = Field(ge=1)
