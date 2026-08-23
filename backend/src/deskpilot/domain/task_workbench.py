"""Unified user projection and exact Artifact export contracts for phase 76."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.agent_replanning import AgentRepairLoopStatus, AgentReplanPage
from deskpilot.domain.agent_runtime import ExecutionRunPage
from deskpilot.domain.artifact_runtime import (
    ARTIFACT_ID_PATTERN,
    DELIVERY_ID_PATTERN,
    REVISION_ID_PATTERN,
    BrowserRenderRunRead,
    DeliveryManifestRead,
    TaskWorkspaceRead,
    VerificationRunRead,
)
from deskpilot.domain.context_memory import ConversationMessageRead
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.mcp import McpToolCallRead
from deskpilot.domain.research import ResearchSessionRead
from deskpilot.domain.schemas import TaskRead
from deskpilot.domain.task_plans import (
    CONVERSATION_ID_PATTERN,
    MESSAGE_ID_PATTERN,
    TASK_ID_PATTERN,
    ExecutablePlanPage,
    PlanningStateRead,
    TaskContractVersionRead,
)
from deskpilot.domain.workspace_files import (
    WorkspaceCheckRead,
    WorkspaceDirectoryRead,
    WorkspaceEditPreview,
    WorkspaceEditReceipt,
    WorkspaceFileRead,
    WorkspaceNodeTestRead,
    WorkspacePatchPreview,
    WorkspacePatchReceipt,
    WorkspacePathOperationPreview,
    WorkspacePathOperationReceipt,
    WorkspacePythonTestRead,
)

ARTIFACT_EXPORT_ID_PATTERN = r"^xpt_[0-9a-f]{64}$"


def artifact_export_receipt_digest(
    *,
    export_id: str,
    delivery_id: str,
    task_id: str,
    artifact_id: str,
    revision_id: str,
    target_path: str,
    source_digest: str,
    byte_count: int,
    committed_at: datetime,
) -> str:
    canonical_committed_at = (
        committed_at.replace(tzinfo=UTC)
        if committed_at.tzinfo is None
        else committed_at.astimezone(UTC)
    ).isoformat(timespec="microseconds")
    return sha256_digest(
        {
            "export_id": export_id,
            "delivery_id": delivery_id,
            "task_id": task_id,
            "artifact_id": artifact_id,
            "revision_id": revision_id,
            "target_path": target_path,
            "conflict_policy": "fail_if_exists",
            "source_digest": source_digest,
            "byte_count": byte_count,
            "committed_at": canonical_committed_at,
        }
    )


class WorkbenchStage(StrEnum):
    IDLE = "idle"
    PLANNED = "planned"
    RESEARCHING = "researching"
    AWAITING_VERIFICATION = "awaiting_verification"
    BUILDING_ARTIFACT = "building_artifact"
    VERIFYING_BROWSER = "verifying_browser"
    READY_TO_DELIVER = "ready_to_deliver"
    DELIVERED = "delivered"
    EXPORTED = "exported"
    EXECUTING = "executing"
    NEEDS_CLARIFICATION = "needs_clarification"
    NEEDS_USER_ACTION = "needs_user_action"
    UNSUPPORTED = "unsupported"
    BLOCKED = "blocked"


class WorkbenchAction(StrEnum):
    ACTIVATE_RESEARCH_PLAN = "activate_research_plan"
    START_EXECUTION = "start_execution"
    RUN_RESEARCH = "run_research"
    VERIFY_CLAIMS = "verify_claims"
    BUILD_ARTIFACT = "build_artifact"
    VERIFY_BROWSER = "verify_browser"
    FINALIZE_DELIVERY = "finalize_delivery"
    EXECUTE_ROUTE = "execute_route"
    REPLAN_FAILED_EXECUTION = "replan_failed_execution"
    COMMIT_WORKSPACE_EDIT = "commit_workspace_edit"
    COMMIT_WORKSPACE_PATCH = "commit_workspace_patch"
    COMMIT_WORKSPACE_PATH_OPERATION = "commit_workspace_path_operation"
    PREPARE_EXPORT = "prepare_export"
    STOP_EXECUTION = "stop_execution"


class TurnRouteDecision(StrEnum):
    ROUTED = "routed"
    NEEDS_CLARIFICATION = "needs_clarification"
    UNSUPPORTED = "unsupported"


class TurnRouteStatus(StrEnum):
    READY = "ready"
    RUNNING = "running"
    NEEDS_USER_ACTION = "needs_user_action"
    WAITING_USER_INPUT = "waiting_user_input"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    NOT_APPLICABLE = "not_applicable"


class TurnRouteRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.turn-route.v1"] = "deskpilot.turn-route.v1"
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    conversation_id: str = Field(pattern=CONVERSATION_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    decision: TurnRouteDecision
    route_id: (
        Literal[
            "research_to_html",
            "knowledge_lookup",
            "mcp_text_metrics",
            "workspace_file_read",
            "workspace_file_replace",
            "workspace_patch_bundle",
            "workspace_agent_patch_test",
            "workspace_dynamic_patch_test",
            "workspace_file_create",
            "workspace_file_rename",
            "workspace_directory_list",
            "workspace_directory_analyze",
            "workspace_snapshot_check",
            "workspace_python_test",
            "workspace_node_test",
        ]
        | None
    )
    route_version: Literal["1"] | None
    route_manifest_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    candidate_digest: str = Field(pattern=DIGEST_PATTERN)
    parameter_digest: str = Field(pattern=DIGEST_PATTERN)
    resolved_from_task_id: str | None = Field(default=None, pattern=TASK_ID_PATTERN)
    resolution_rule: str | None = Field(default=None, min_length=1, max_length=64)
    resolution_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    reason_code: str
    status: TurnRouteStatus
    result_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    error_code: str | None = None
    revision: int = Field(ge=1)
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def resolution_is_complete(self) -> Self:
        values = (
            self.resolved_from_task_id,
            self.resolution_rule,
            self.resolution_digest,
        )
        if any(value is not None for value in values) != all(value is not None for value in values):
            raise ValueError("Turn Route resolution binding must be complete")
        if self.resolved_from_task_id is not None and self.decision is not TurnRouteDecision.ROUTED:
            raise ValueError("Only routed Turns can resolve a clarification")
        return self


class WorkbenchActionRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    action: WorkbenchAction
    enabled: bool
    reason_code: str
    explanation: str
    effect_class: Literal["read_only", "workspace_write", "user_path_write", "execution_control"]


class CreateResearchWorkbenchTask(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    goal: str = Field(min_length=1, max_length=4_000)
    privacy_mode: Literal["local_preferred", "balanced"] = "local_preferred"
    constraints: tuple[str, ...] = Field(default=(), max_length=50)


class CreateConversationTurn(BaseModel):
    """A user turn that starts a new immutable Task inside a conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    message: str = Field(min_length=1, max_length=4_000)
    privacy_mode: Literal["local_preferred", "balanced"] = "local_preferred"
    constraints: tuple[str, ...] = Field(default=(), max_length=50)


class ContinueConversationTurn(BaseModel):
    """A follow-up turn; the service creates a replacement Task in the same conversation."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    message: str = Field(min_length=1, max_length=4_000)


class PrepareArtifactExport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    target_path: str = Field(min_length=1, max_length=32_767)
    artifact_id: str | None = Field(default=None, pattern=ARTIFACT_ID_PATTERN)


class CommitArtifactExport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)


class ArtifactExportRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.artifact-export.v1"] = "deskpilot.artifact-export.v1"
    export_id: str = Field(pattern=ARTIFACT_EXPORT_ID_PATTERN)
    delivery_id: str = Field(pattern=DELIVERY_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    artifact_id: str = Field(pattern=ARTIFACT_ID_PATTERN)
    revision_id: str = Field(pattern=REVISION_ID_PATTERN)
    target_path: str
    conflict_policy: Literal["fail_if_exists"] = "fail_if_exists"
    status: Literal["prepared", "committing", "committed", "failed"]
    source_digest: str = Field(pattern=DIGEST_PATTERN)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    confirmation_digest: str = Field(pattern=DIGEST_PATTERN)
    receipt_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    byte_count: int = Field(ge=1)
    error_code: str | None = None
    requested_at: datetime
    committed_at: datetime | None = None

    @model_validator(mode="after")
    def receipt_matches(self) -> Self:
        if self.status == "committed":
            if self.receipt_digest is None or self.committed_at is None:
                raise ValueError("Committed export requires an immutable receipt")
            expected = artifact_export_receipt_digest(
                export_id=self.export_id,
                delivery_id=self.delivery_id,
                task_id=self.task_id,
                artifact_id=self.artifact_id,
                revision_id=self.revision_id,
                target_path=self.target_path,
                source_digest=self.source_digest,
                byte_count=self.byte_count,
                committed_at=self.committed_at,
            )
            if self.receipt_digest != expected:
                raise ValueError("Artifact export receipt digest does not match")
        elif self.receipt_digest is not None or self.committed_at is not None:
            raise ValueError("Uncommitted export cannot carry a receipt")
        return self


class TaskWorkbenchRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.task-workbench.v1"] = "deskpilot.task-workbench.v1"
    task: TaskRead
    stage: WorkbenchStage
    actions: tuple[WorkbenchActionRead, ...]
    conversation: tuple[ConversationMessageRead, ...]
    route: TurnRouteRead | None
    planning: PlanningStateRead | None
    contract: TaskContractVersionRead | None
    plans: ExecutablePlanPage
    executions: ExecutionRunPage
    replans: AgentReplanPage
    repair_loop: AgentRepairLoopStatus | None
    research: ResearchSessionRead | None
    verification: VerificationRunRead | None
    workspace: TaskWorkspaceRead | None
    browser: BrowserRenderRunRead | None
    delivery: DeliveryManifestRead | None
    knowledge: KnowledgeSearchRead | None
    mcp: McpToolCallRead | None
    workspace_file: WorkspaceFileRead | None
    workspace_edit: WorkspaceEditPreview | WorkspaceEditReceipt | None
    workspace_patch: WorkspacePatchPreview | WorkspacePatchReceipt | None
    workspace_path_operation: WorkspacePathOperationPreview | WorkspacePathOperationReceipt | None
    workspace_directory: WorkspaceDirectoryRead | None
    workspace_check: WorkspaceCheckRead | None
    workspace_python_test: WorkspacePythonTestRead | None = None
    workspace_node_test: WorkspaceNodeTestRead | None = None
    exports: tuple[ArtifactExportRead, ...]
    projection_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"projection_digest"})
        if self.projection_digest != sha256_digest(material):
            raise ValueError("Task Workbench projection digest does not match")
        return self
