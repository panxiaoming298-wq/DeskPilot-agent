"""Exact, persistent user authority for stage-112C capability effects."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_runtime import RUN_ID_PATTERN
from deskpilot.domain.task_loop_execution import (
    MODEL_PLANNER_NODE_BINDING_ID_PATTERN,
    TASK_LOOP_EXECUTION_ID_PATTERN,
    TASK_LOOP_NODE_ATTEMPT_ID_PATTERN,
)
from deskpilot.domain.task_plans import PLAN_NODE_ID_PATTERN, TASK_ID_PATTERN
from deskpilot.domain.workspace_files import WorkspacePatchPreview

TASK_LOOP_CAPABILITY_APPROVAL_ID_PATTERN = r"^tlca_[0-9a-f]{64}$"
TaskLoopCapabilityApprovalStatus = Literal["pending", "approved", "consumed"]


class TaskLoopCapabilityApproval(BaseModel):
    """One approval request bound to an exact Task Loop node revision."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-capability-approval.v1"] = (
        "deskpilot.task-loop-capability-approval.v1"
    )
    approval_id: str = Field(pattern=TASK_LOOP_CAPABILITY_APPROVAL_ID_PATTERN)
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    run_id: str = Field(pattern=RUN_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    node_binding_id: str = Field(pattern=MODEL_PLANNER_NODE_BINDING_ID_PATTERN)
    attempt_id: str = Field(pattern=TASK_LOOP_NODE_ATTEMPT_ID_PATTERN)
    attempt: int = Field(ge=1)
    plan_generation: int = Field(ge=1, le=3)
    input_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    executor_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    preview_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    preview_manifest: dict[str, Any]
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    requested_execution_revision: int = Field(ge=2)
    status: TaskLoopCapabilityApprovalStatus
    revision: int = Field(ge=1, le=3)
    approved_at: datetime | None = None
    consumed_at: datetime | None = None
    result_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    created_at: datetime
    updated_at: datetime
    approval_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digest_match(self) -> Self:
        preview = WorkspacePatchPreview.model_validate(self.preview_manifest)
        if (
            preview.task_id != self.task_id
            or preview.confirmation_digest != self.confirmation_digest
            or self.preview_schema_digest
            != sha256_digest(WorkspacePatchPreview.model_json_schema())
        ):
            raise ValueError("Task-loop approval preview proof changed")
        expected_revision = {"pending": 1, "approved": 2, "consumed": 3}[self.status]
        if self.revision != expected_revision:
            raise ValueError("Task-loop approval revision does not match its state")
        if self.status == "pending":
            if self.approved_at is not None or self.consumed_at is not None:
                raise ValueError("Pending Task-loop approval contains a decision")
            if self.result_digest is not None:
                raise ValueError("Pending Task-loop approval contains a result")
        elif self.status == "approved":
            if self.approved_at is None or self.consumed_at is not None:
                raise ValueError("Approved Task-loop authority proof is incomplete")
            if self.result_digest is not None:
                raise ValueError("Unconsumed Task-loop approval contains a result")
        elif (
            self.approved_at is None
            or self.consumed_at is None
            or self.result_digest is None
        ):
            raise ValueError("Consumed Task-loop authority proof is incomplete")
        timestamps = (self.created_at, self.updated_at, self.approved_at, self.consumed_at)
        if any(item is not None and item.tzinfo is None for item in timestamps):
            raise ValueError("Task-loop approval timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Task-loop approval update predates its request")
        if self.approved_at is not None and self.approved_at < self.created_at:
            raise ValueError("Task-loop approval decision predates its request")
        if self.consumed_at is not None and (
            self.approved_at is None or self.consumed_at < self.approved_at
        ):
            raise ValueError("Task-loop approval consumption predates approval")
        identity = {
            "execution_id": self.execution_id,
            "node_id": self.node_id,
            "attempt_id": self.attempt_id,
            "confirmation_digest": self.confirmation_digest,
        }
        if self.approval_id != f"tlca_{sha256_digest(identity)}":
            raise ValueError("Task-loop approval id does not match")
        material = self.model_dump(mode="json", exclude={"approval_digest"})
        if self.approval_digest != sha256_digest(material):
            raise ValueError("Task-loop approval digest does not match")
        return self

    @classmethod
    def request(
        cls,
        *,
        execution_id: str,
        task_id: str,
        run_id: str,
        node_id: str,
        node_binding_id: str,
        attempt_id: str,
        attempt: int,
        plan_generation: int,
        input_binding_digest: str,
        executor_manifest_digest: str,
        preview: WorkspacePatchPreview,
        requested_execution_revision: int,
        created_at: datetime,
    ) -> Self:
        identity = {
            "execution_id": execution_id,
            "node_id": node_id,
            "attempt_id": attempt_id,
            "confirmation_digest": preview.confirmation_digest,
        }
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-capability-approval.v1",
            "approval_id": f"tlca_{sha256_digest(identity)}",
            "execution_id": execution_id,
            "task_id": task_id,
            "run_id": run_id,
            "node_id": node_id,
            "node_binding_id": node_binding_id,
            "attempt_id": attempt_id,
            "attempt": attempt,
            "plan_generation": plan_generation,
            "input_binding_digest": input_binding_digest,
            "executor_manifest_digest": executor_manifest_digest,
            "preview_schema_digest": sha256_digest(WorkspacePatchPreview.model_json_schema()),
            "preview_manifest": preview.model_dump(mode="json"),
            "confirmation_digest": preview.confirmation_digest,
            "requested_execution_revision": requested_execution_revision,
            "status": "pending",
            "revision": 1,
            "approved_at": None,
            "consumed_at": None,
            "result_digest": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        return cls(**values, approval_digest=sha256_digest(values))

    def approve(
        self,
        *,
        confirmation_digest: str,
        expected_execution_revision: int,
        approved_at: datetime,
    ) -> Self:
        if (
            self.status != "pending"
            or confirmation_digest != self.confirmation_digest
            or expected_execution_revision != self.requested_execution_revision
        ):
            raise ValueError("Task-loop approval no longer matches the waiting revision")
        values = self.model_dump(mode="python", exclude={"approval_digest"})
        values.update(
            status="approved",
            revision=2,
            approved_at=approved_at,
            updated_at=approved_at,
        )
        return type(self)(**values, approval_digest=sha256_digest(values))

    def consume(self, *, result_digest: str, consumed_at: datetime) -> Self:
        if self.status != "approved":
            raise ValueError("Only approved Task-loop authority can be consumed")
        values = self.model_dump(mode="python", exclude={"approval_digest"})
        values.update(
            status="consumed",
            revision=3,
            consumed_at=consumed_at,
            result_digest=result_digest,
            updated_at=consumed_at,
        )
        return type(self)(**values, approval_digest=sha256_digest(values))


__all__ = [
    "TASK_LOOP_CAPABILITY_APPROVAL_ID_PATTERN",
    "TaskLoopCapabilityApproval",
    "TaskLoopCapabilityApprovalStatus",
]
