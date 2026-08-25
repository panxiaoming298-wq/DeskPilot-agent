"""Proof-bound activation and execution records for model-planner task loops.

The stage-112B activation boundary is intentionally narrower than the generic
Agent runtime.  A composite executable node is dispatchable only after it is
bound back to one exact stage-111 Offer step and the server has calculated the
effective authority as the intersection of the composite Task Contract and
that source step.  These records are internal authority evidence; they are not
model-authored inputs and are not a public Workbench projection.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN, AgentToolGrant, BoundAgentRef
from deskpilot.domain.command_profiles import CommandProfileId
from deskpilot.domain.model_contracts import ModelLocation, PrivacyMode
from deskpilot.domain.task_loop import (
    MODEL_PLANNER_DRAFT_ID_PATTERN,
    MODEL_PLANNER_STEP_BINDING_ID_PATTERN,
    TASK_LOOP_ID_PATTERN,
    ModelPlannerNodeMapping,
)
from deskpilot.domain.task_loop_cycle import TaskLoopCycleRead
from deskpilot.domain.task_plans import (
    MESSAGE_ID_PATTERN,
    PLAN_ID_PATTERN,
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    CapabilityRef,
    DraftNodeKind,
    PlanNodeBudget,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import (
    TURN_PLANNING_OFFER_ID_PATTERN,
    TURN_PLANNING_OFFER_KEY_PATTERN,
    TurnPlanningParameterBinding,
    TurnPlanningRecipeRef,
)
from deskpilot.domain.workspace_command_plans import WORKSPACE_COMMAND_PLAN_ID_PATTERN
from deskpilot.domain.workspace_files import WorkspacePatchPreview, WorkspacePatchReceipt

TASK_LOOP_EXECUTION_ID_PATTERN = r"^tlx_[0-9a-f]{64}$"
TASK_LOOP_EXECUTION_EVENT_ID_PATTERN = r"^txe_[0-9a-f]{64}$"
MODEL_PLANNER_NODE_BINDING_ID_PATTERN = r"^mnb_[0-9a-f]{64}$"
TASK_LOOP_NODE_ATTEMPT_ID_PATTERN = r"^tla_[0-9a-f]{64}$"
TASK_LOOP_RESULT_REF_ID_PATTERN = r"^tlr_[0-9a-f]{64}$"

TaskLoopExecutionStatus = Literal[
    "active",
    "paused",
    "awaiting_user",
    "repairing",
    "failed",
    "succeeded",
    "cancelled",
]
TaskLoopExecutionEventKind = Literal[
    "activated",
    "paused",
    "resumed",
    "awaiting_user",
    "repair_started",
    "failed",
    "succeeded",
    "cancelled",
]
TaskLoopNodeAttemptStatus = Literal[
    "prepared",
    "claimed",
    "running",
    "awaiting_verification",
    "verified",
    "failed",
    "outcome_unknown",
    "cancelled",
]
TaskLoopReadStatus = Literal["observed", "planned", "failed"]
TaskLoopWorkbenchPhase = Literal[
    "observe",
    "plan",
    "execute",
    "verify",
    "awaiting_user",
    "repair",
]
TaskLoopExecutionNodeStatus = Literal[
    "pending",
    "ready",
    "claimed",
    "running",
    "awaiting_verification",
    "verified",
    "cancelled",
    "failed",
    "waiting_user",
    "waiting_children",
]


def _content_id(prefix: str, material: Any) -> str:
    return f"{prefix}_{sha256_digest(material)}"


class EffectiveNodeAuthority(BaseModel):
    """Least authority proven as ``composite Contract ∩ source-step Contract``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.effective-node-authority.v1"] = (
        "deskpilot.effective-node-authority.v1"
    )
    authority_rule: Literal["composite_intersection_source_step"] = (
        "composite_intersection_source_step"
    )
    composite_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    source_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    node_kind: DraftNodeKind
    bound_agent: BoundAgentRef | None = None
    bound_tool: AgentToolGrant | None = None
    capability: CapabilityRef | None = None
    resource_scopes: tuple[str, ...] = Field(default=(), max_length=50)
    privacy_classification: Literal["public", "internal", "sensitive"]
    allowed_provider_locations: tuple[ModelLocation, ...] = Field(min_length=1)
    allowed_privacy_modes: tuple[PrivacyMode, ...] = Field(min_length=1)
    external_egress_allowed: bool
    max_risk_level: ToolRiskLevel
    budget: PlanNodeBudget
    authority_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def shape_and_digest_match(self) -> Self:
        if self.node_kind is DraftNodeKind.AGENT:
            if self.bound_agent is None:
                raise ValueError("Effective Agent authority requires an exact Agent")
        elif self.node_kind is DraftNodeKind.CAPABILITY:
            if (
                self.capability is None
                or self.bound_agent is not None
                or self.bound_tool is not None
            ):
                raise ValueError("Effective capability authority requires only an exact capability")
        else:
            raise ValueError("Control nodes do not receive dispatch authority")
        if len(self.resource_scopes) != len(set(self.resource_scopes)):
            raise ValueError("Effective resource scopes must be unique")
        if len(self.allowed_provider_locations) != len(set(self.allowed_provider_locations)):
            raise ValueError("Effective Provider locations must be unique")
        if len(self.allowed_privacy_modes) != len(set(self.allowed_privacy_modes)):
            raise ValueError("Effective privacy modes must be unique")
        material = self.model_dump(mode="json", exclude={"authority_digest"})
        if self.authority_digest != sha256_digest(material):
            raise ValueError("Effective node authority digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.effective-node-authority.v1",
            "authority_rule": "composite_intersection_source_step",
            **values,
        }
        return cls(**material, authority_digest=sha256_digest(material))


class RuntimeEligibilityProof(BaseModel):
    """Exact current registry entry checked immediately before activation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.runtime-eligibility-proof.v1"] = (
        "deskpilot.runtime-eligibility-proof.v1"
    )
    runtime_kind: Literal["agent", "capability_executor"]
    bound_agent: BoundAgentRef | None = None
    capability: CapabilityRef | None = None
    executor_id: str | None = Field(default=None, min_length=1, max_length=128)
    executor_manifest_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    agent_adapter_id: str | None = Field(default=None, min_length=1, max_length=128)
    agent_adapter_manifest_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
    )
    registry_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    runtime_enabled: Literal[True] = True
    eligibility_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def shape_and_digest_match(self) -> Self:
        if self.runtime_kind == "agent":
            if (
                self.bound_agent is None
                or self.capability is not None
                or self.executor_id is not None
                or self.executor_manifest_digest is not None
                or self.agent_adapter_id is None
                or self.agent_adapter_manifest_digest is None
            ):
                raise ValueError("Agent eligibility proof shape is invalid")
        elif (
            self.bound_agent is not None
            or self.capability is None
            or self.executor_id is None
            or self.executor_manifest_digest is None
            or self.agent_adapter_id is not None
            or self.agent_adapter_manifest_digest is not None
        ):
            raise ValueError("Capability executor eligibility proof shape is invalid")
        material = self.model_dump(mode="json", exclude={"eligibility_digest"})
        if self.eligibility_digest != sha256_digest(material):
            raise ValueError("Runtime eligibility digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.runtime-eligibility-proof.v1",
            "runtime_enabled": True,
            "agent_adapter_id": None,
            "agent_adapter_manifest_digest": None,
            **values,
        }
        return cls(**material, eligibility_digest=sha256_digest(material))


class ModelPlannerNodeBinding(BaseModel):
    """Exact source Offer/input/policy/mapping for one runnable composite node."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.model-planner-node-binding.v1"] = (
        "deskpilot.model-planner-node-binding.v1"
    )
    node_binding_id: str = Field(pattern=MODEL_PLANNER_NODE_BINDING_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    user_message_id: str = Field(pattern=MESSAGE_ID_PATTERN)
    draft_id: str = Field(pattern=MODEL_PLANNER_DRAFT_ID_PATTERN)
    step_binding_id: str = Field(pattern=MODEL_PLANNER_STEP_BINDING_ID_PATTERN)
    step_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    step_ordinal: int = Field(ge=1, le=8)
    offer_id: str = Field(pattern=TURN_PLANNING_OFFER_ID_PATTERN)
    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    offer_digest: str = Field(pattern=DIGEST_PATTERN)
    recipe: TurnPlanningRecipeRef
    policy_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    source_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    source_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    source_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    source_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    source_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_contract_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    composite_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    composite_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    mapping: ModelPlannerNodeMapping
    parameter_bindings: tuple[TurnPlanningParameterBinding, ...] = Field(default=(), max_length=32)
    parameter_bindings_digest: str = Field(pattern=DIGEST_PATTERN)
    bound_input_manifest: dict[str, str] = Field(default_factory=dict)
    bound_input_digest: str = Field(pattern=DIGEST_PATTERN)
    effective_authority: EffectiveNodeAuthority
    runtime_eligibility: RuntimeEligibilityProof
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lineage_and_digest_match(self) -> Self:
        if (
            self.mapping.source_node_id != self.source_node_id
            or self.mapping.source_node_spec_digest != self.source_node_spec_digest
            or self.mapping.composite_node_id != self.composite_node_id
            or self.mapping.composite_node_spec_digest != self.composite_node_spec_digest
        ):
            raise ValueError("Node binding mapping changed")
        if any(item.offer_key != self.offer_key for item in self.parameter_bindings):
            raise ValueError("Node binding input crosses its Offer")
        expected_inputs = sha256_digest(
            {
                "parameter_bindings": [
                    item.model_dump(mode="json") for item in self.parameter_bindings
                ]
            }
        )
        if self.parameter_bindings_digest != expected_inputs:
            raise ValueError("Node binding input digest does not match")
        if self.bound_input_digest != sha256_digest(
            {"parameters": dict(sorted(self.bound_input_manifest.items()))}
        ):
            raise ValueError("Node binding canonical input digest does not match")
        if (
            self.effective_authority.composite_contract_digest != self.composite_contract_digest
            or self.effective_authority.source_contract_digest != self.source_contract_digest
        ):
            raise ValueError("Node binding authority crosses its Contract lineage")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"node_binding_id", "binding_digest"}
        }
        if self.node_binding_id != _content_id("mnb", identity):
            raise ValueError("Model Planner node binding id does not match")
        digest_material = {key: value for key, value in values.items() if key != "binding_digest"}
        if self.binding_digest != sha256_digest(digest_material):
            raise ValueError("Model Planner node binding digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material: dict[str, Any] = {
            "schema_version": "deskpilot.model-planner-node-binding.v1",
            **values,
        }
        identity = dict(material)
        node_binding_id = _content_id("mnb", identity)
        digest_material = {**material, "node_binding_id": node_binding_id}
        return cls(
            **digest_material,
            binding_digest=sha256_digest(digest_material),
        )


class TaskLoopExecutionEvent(BaseModel):
    """Immutable event in a task-loop execution digest chain."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-execution-event.v1"] = (
        "deskpilot.task-loop-execution-event.v1"
    )
    event_id: str = Field(pattern=TASK_LOOP_EXECUTION_EVENT_ID_PATTERN)
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    sequence: int = Field(ge=1)
    previous_event_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    kind: TaskLoopExecutionEventKind
    plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{64}$")
    binding_set_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    event_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Task-loop execution event timestamp must be timezone-aware")
        if self.kind == "activated" and (
            self.sequence != 1 or self.previous_event_digest is not None
        ):
            raise ValueError("Activation must be the first execution event")
        values = self.model_dump(mode="json")
        identity = {
            key: value for key, value in values.items() if key not in {"event_id", "event_digest"}
        }
        if self.event_id != _content_id("txe", identity):
            raise ValueError("Task-loop execution event id does not match")
        digest_material = {key: value for key, value in values.items() if key != "event_digest"}
        if self.event_digest != sha256_digest(digest_material):
            raise ValueError("Task-loop execution event digest does not match")
        return self

    @classmethod
    def activated(
        cls,
        *,
        execution_id: str,
        task_id: str,
        plan_manifest_digest: str,
        run_id: str,
        binding_set_digest: str,
        created_at: datetime,
    ) -> Self:
        material: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-execution-event.v1",
            "execution_id": execution_id,
            "task_id": task_id,
            "sequence": 1,
            "previous_event_digest": None,
            "kind": "activated",
            "plan_manifest_digest": plan_manifest_digest,
            "run_id": run_id,
            "binding_set_digest": binding_set_digest,
            "created_at": created_at,
        }
        event_id = _content_id("txe", material)
        digest_material = {**material, "event_id": event_id}
        return cls(**digest_material, event_digest=sha256_digest(digest_material))

    @classmethod
    def appended(
        cls,
        *,
        execution: TaskLoopExecution,
        kind: TaskLoopExecutionEventKind,
        created_at: datetime,
    ) -> Self:
        if kind == "activated":
            raise ValueError("Activation cannot be appended to an execution event chain")
        material: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-execution-event.v1",
            "execution_id": execution.execution_id,
            "task_id": execution.task_id,
            "sequence": execution.event_count + 1,
            "previous_event_digest": execution.latest_event_digest,
            "kind": kind,
            "plan_manifest_digest": execution.plan_manifest_digest,
            "run_id": execution.run_id,
            "binding_set_digest": execution.binding_set_digest,
            "created_at": created_at,
        }
        event_id = _content_id("txe", material)
        digest_material = {**material, "event_id": event_id}
        return cls(**digest_material, event_digest=sha256_digest(digest_material))


class TaskLoopExecution(BaseModel):
    """Mutable execution pointer whose activation is backed by immutable proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-execution.v1"] = "deskpilot.task-loop-execution.v1"
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    draft_id: str = Field(pattern=MODEL_PLANNER_DRAFT_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    plan_generation: Literal[1] = 1
    plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{64}$")
    status: TaskLoopExecutionStatus
    revision: int = Field(ge=1)
    event_count: int = Field(ge=1)
    latest_event_id: str = Field(pattern=TASK_LOOP_EXECUTION_EVENT_ID_PATTERN)
    latest_event_digest: str = Field(pattern=DIGEST_PATTERN)
    node_binding_count: int = Field(ge=1, le=18)
    binding_set_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    updated_at: datetime
    execution_digest: str = Field(pattern=DIGEST_PATTERN)

    def _identity(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "loop_id": self.loop_id,
            "draft_id": self.draft_id,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "plan_generation": self.plan_generation,
            "plan_manifest_digest": self.plan_manifest_digest,
            "run_id": self.run_id,
            "binding_set_digest": self.binding_set_digest,
            "created_at": self.created_at,
        }

    @model_validator(mode="after")
    def lifecycle_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Task-loop execution timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Task-loop execution update predates creation")
        if self.revision == 1 and (self.status != "active" or self.event_count != 1):
            raise ValueError("Initial task-loop execution lifecycle is invalid")
        if self.execution_id != _content_id("tlx", self._identity()):
            raise ValueError("Task-loop execution id does not match")
        values = self.model_dump(mode="json")
        digest_material = {key: value for key, value in values.items() if key != "execution_digest"}
        if self.execution_digest != sha256_digest(digest_material):
            raise ValueError("Task-loop execution digest does not match")
        return self

    @classmethod
    def activate(
        cls,
        *,
        loop_id: str,
        draft_id: str,
        task_id: str,
        plan_id: str,
        plan_manifest_digest: str,
        run_id: str,
        bindings: tuple[ModelPlannerNodeBinding, ...],
        created_at: datetime,
    ) -> tuple[Self, TaskLoopExecutionEvent]:
        if not bindings:
            raise ValueError("Task-loop activation requires runnable node bindings")
        if any(
            item.task_id != task_id
            or item.draft_id != draft_id
            or item.composite_plan_id != plan_id
            or item.composite_plan_manifest_digest != plan_manifest_digest
            for item in bindings
        ):
            raise ValueError("Task-loop activation bindings cross their sealed Draft")
        binding_set_digest = sha256_digest(
            {
                "node_bindings": [
                    {
                        "node_binding_id": item.node_binding_id,
                        "binding_digest": item.binding_digest,
                    }
                    for item in sorted(bindings, key=lambda value: value.composite_node_id)
                ]
            }
        )
        identity: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-execution.v1",
            "loop_id": loop_id,
            "draft_id": draft_id,
            "task_id": task_id,
            "plan_id": plan_id,
            "plan_generation": 1,
            "plan_manifest_digest": plan_manifest_digest,
            "run_id": run_id,
            "binding_set_digest": binding_set_digest,
            "created_at": created_at,
        }
        execution_id = _content_id("tlx", identity)
        event = TaskLoopExecutionEvent.activated(
            execution_id=execution_id,
            task_id=task_id,
            plan_manifest_digest=plan_manifest_digest,
            run_id=run_id,
            binding_set_digest=binding_set_digest,
            created_at=created_at,
        )
        material: dict[str, Any] = {
            **identity,
            "execution_id": execution_id,
            "status": "active",
            "revision": 1,
            "event_count": 1,
            "latest_event_id": event.event_id,
            "latest_event_digest": event.event_digest,
            "node_binding_count": len(bindings),
            "updated_at": created_at,
        }
        digest_material = dict(material)
        return (
            cls(
                **digest_material,
                execution_digest=sha256_digest(digest_material),
            ),
            event,
        )

    def transition(
        self,
        *,
        status: TaskLoopExecutionStatus,
        kind: TaskLoopExecutionEventKind,
        updated_at: datetime,
    ) -> tuple[Self, TaskLoopExecutionEvent]:
        expected_status = {
            "paused": "paused",
            "resumed": "active",
            "awaiting_user": "awaiting_user",
            "repair_started": "repairing",
            "failed": "failed",
            "succeeded": "succeeded",
            "cancelled": "cancelled",
        }.get(kind)
        if (
            expected_status != status
            or kind == "activated"
            or self.status in {"failed", "succeeded", "cancelled"}
            or updated_at.tzinfo is None
            or updated_at < self.updated_at
        ):
            raise ValueError("Task Loop execution transition is invalid")
        event = TaskLoopExecutionEvent.appended(
            execution=self,
            kind=kind,
            created_at=updated_at,
        )
        material = self.model_dump(mode="python", exclude={"execution_digest"})
        material.update(
            {
                "status": status,
                "revision": self.revision + 1,
                "event_count": event.sequence,
                "latest_event_id": event.event_id,
                "latest_event_digest": event.event_digest,
                "updated_at": updated_at,
            }
        )
        return (
            type(self)(**material, execution_digest=sha256_digest(material)),
            event,
        )


class TaskLoopExecutionNodeRead(BaseModel):
    """Internal proof-derived node state used to build a safe projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-execution-node-read.v1"] = (
        "deskpilot.task-loop-execution-node-read.v1"
    )
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    local_key: str = Field(min_length=1, max_length=64)
    kind: DraftNodeKind
    status: TaskLoopExecutionNodeStatus
    depends_on: tuple[str, ...] = Field(default=(), max_length=20)
    verified_dependency_node_ids: tuple[str, ...] = Field(default=(), max_length=20)
    dependency_count: int = Field(ge=0, le=20)
    verified_dependency_count: int = Field(ge=0, le=20)
    dependencies_verified: bool
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    candidate_present: bool
    verified_result_present: bool
    verified_failure_result_count: int = Field(default=0, ge=0)
    command_plan_id: str | None = Field(
        default=None,
        pattern=WORKSPACE_COMMAND_PLAN_ID_PATTERN,
    )
    command_step_sequence: int | None = Field(default=None, ge=1, le=6)
    command_profile_id: CommandProfileId | None = None
    created_at: datetime
    updated_at: datetime
    state_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def state_and_digest_match(self) -> Self:
        if (
            len(self.depends_on) != len(set(self.depends_on))
            or len(self.verified_dependency_node_ids) != len(set(self.verified_dependency_node_ids))
            or not set(self.verified_dependency_node_ids).issubset(self.depends_on)
            or self.dependency_count != len(self.depends_on)
            or self.verified_dependency_count != len(self.verified_dependency_node_ids)
            or self.dependencies_verified
            is not (self.dependency_count == self.verified_dependency_count)
        ):
            raise ValueError("Task-loop node dependency proof is invalid")
        if self.attempt_count > self.max_attempts:
            raise ValueError("Task-loop node attempt budget was exceeded")
        if self.candidate_present and (
            self.status != "awaiting_verification" or self.verified_result_present
        ):
            raise ValueError("Task-loop candidate state is invalid")
        if self.kind in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY}:
            if self.verified_result_present is not (self.status == "verified"):
                raise ValueError("Runnable node verification proof is incomplete")
        elif self.verified_result_present:
            raise ValueError("Control node cannot expose a verified ResultRef")
        command_fields = (
            self.command_plan_id,
            self.command_step_sequence,
            self.command_profile_id,
        )
        if any(item is None for item in command_fields) != all(
            item is None for item in command_fields
        ):
            raise ValueError("Task-loop command node projection is incomplete")
        if self.verified_failure_result_count and (
            self.kind is not DraftNodeKind.CAPABILITY
        ):
            raise ValueError("Only capability nodes may expose failure ResultRefs")
        if (
            self.created_at.tzinfo is None
            or self.updated_at.tzinfo is None
            or self.updated_at < self.created_at
        ):
            raise ValueError("Task-loop node read timestamps are invalid")
        material = self.model_dump(mode="json", exclude={"state_digest"})
        if self.state_digest != sha256_digest(material):
            raise ValueError("Task-loop node read digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.task-loop-execution-node-read.v1",
            **values,
        }
        material.setdefault("verified_failure_result_count", 0)
        material.setdefault("command_plan_id", None)
        material.setdefault("command_step_sequence", None)
        material.setdefault("command_profile_id", None)
        return cls(**material, state_digest=sha256_digest(material))


class TaskLoopExecutionRead(BaseModel):
    """Internal, proof-checked read across Observe through terminal execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-execution-read.v1"] = (
        "deskpilot.task-loop-execution-read.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    loop_status: TaskLoopReadStatus
    phase: TaskLoopWorkbenchPhase
    loop_revision: int = Field(ge=1, le=2)
    loop_event_count: int = Field(ge=1, le=2)
    loop_progress_digest: str = Field(pattern=DIGEST_PATTERN)
    execution: TaskLoopExecution | None = None
    cycle: TaskLoopCycleRead | None = None
    workspace_patch: WorkspacePatchPreview | WorkspacePatchReceipt | None = None
    nodes: tuple[TaskLoopExecutionNodeRead, ...] = Field(default=(), max_length=18)
    recoverable: bool
    created_at: datetime
    updated_at: datetime
    read_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_and_digest_match(self) -> Self:
        if len({item.node_id for item in self.nodes}) != len(self.nodes) or len(
            {item.local_key for item in self.nodes}
        ) != len(self.nodes):
            raise ValueError("Task-loop execution read repeats a node")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Task-loop execution read timestamps must be timezone-aware")
        if self.updated_at < self.created_at:
            raise ValueError("Task-loop execution read update predates creation")
        if self.execution is None:
            expected_phase: TaskLoopWorkbenchPhase = (
                "observe" if self.loop_status == "observed" else "plan"
            )
            expected_recoverable = self.loop_status in {"observed", "planned"}
            if self.phase != expected_phase or self.recoverable is not expected_recoverable:
                raise ValueError("Pre-execution Task Loop read lifecycle is invalid")
            if self.loop_status != "planned" and self.nodes:
                raise ValueError("Only a sealed Plan may expose preview nodes")
            if self.cycle is not None:
                raise ValueError("Pre-execution Task Loop cannot expose cycle state")
            if self.workspace_patch is not None:
                raise ValueError("Pre-execution Task Loop cannot expose an approval")
        else:
            if (
                self.loop_status != "planned"
                or self.execution.task_id != self.task_id
                or self.execution.loop_id != self.loop_id
                or self.phase in {"observe", "plan"}
            ):
                raise ValueError("Execution read crosses its planned Task Loop")
            expected_recoverable = self.execution.status in {
                "active",
                "paused",
                "awaiting_user",
                "repairing",
            }
            if self.recoverable is not expected_recoverable:
                raise ValueError("Execution recovery state is invalid")
            if self.cycle is None:
                raise ValueError("Execution read has no persistent cycle summary")
        material = self.model_dump(mode="json", exclude={"read_digest"})
        if self.read_digest != sha256_digest(material):
            raise ValueError("Task-loop execution read digest does not match")
        return self

    @classmethod
    def build(cls, **values: Any) -> Self:
        material = {
            "schema_version": "deskpilot.task-loop-execution-read.v1",
            **values,
        }
        material.setdefault("cycle", None)
        material.setdefault("workspace_patch", None)
        if material.get("execution") is not None and material.get("cycle") is None:
            material["cycle"] = TaskLoopCycleRead.build(
                no_progress_count=0,
                repair_count=0,
                budget_exhausted=False,
                latest_event_kind=None,
                latest_event_sequence=0,
            )
        return cls(**material, read_digest=sha256_digest(material))

    @property
    def workbench(self) -> TaskLoopExecutionWorkbenchRead:
        return TaskLoopExecutionWorkbenchRead.from_internal(self)


class TaskLoopExecutionWorkbenchNodeRead(BaseModel):
    """Strictly sanitized node summary; it contains no execution inputs."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-workbench-node.v1"] = (
        "deskpilot.task-loop-workbench-node.v1"
    )
    local_key: str = Field(min_length=1, max_length=64)
    kind: DraftNodeKind
    status: TaskLoopExecutionNodeStatus
    dependency_count: int = Field(ge=0, le=20)
    verified_dependency_count: int = Field(ge=0, le=20)
    dependencies_verified: bool
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(ge=1)
    candidate_present: bool
    verified_result_present: bool
    verified_failure_result_count: int = Field(default=0, ge=0)
    command_plan_id: str | None = Field(
        default=None,
        pattern=WORKSPACE_COMMAND_PLAN_ID_PATTERN,
    )
    command_step_sequence: int | None = Field(default=None, ge=1, le=6)
    command_profile_id: CommandProfileId | None = None
    updated_at: datetime
    summary_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def summary_and_digest_match(self) -> Self:
        command_fields = (
            self.command_plan_id,
            self.command_step_sequence,
            self.command_profile_id,
        )
        if (
            self.verified_dependency_count > self.dependency_count
            or self.dependencies_verified
            is not (self.verified_dependency_count == self.dependency_count)
            or self.attempt_count > self.max_attempts
            or self.updated_at.tzinfo is None
        ):
            raise ValueError("Task-loop Workbench node summary is invalid")
        if any(item is not None for item in command_fields) != all(
            item is not None for item in command_fields
        ):
            raise ValueError("Task-loop Workbench command summary is incomplete")
        if self.verified_failure_result_count and self.command_plan_id is None:
            raise ValueError("Only command nodes may expose verified failure receipts")
        material = self.model_dump(mode="json", exclude={"summary_digest"})
        if self.summary_digest != sha256_digest(material):
            raise ValueError("Task-loop Workbench node digest does not match")
        return self

    @classmethod
    def from_internal(cls, node: TaskLoopExecutionNodeRead) -> Self:
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-workbench-node.v1",
            "local_key": node.local_key,
            "kind": node.kind,
            "status": node.status,
            "dependency_count": node.dependency_count,
            "verified_dependency_count": node.verified_dependency_count,
            "dependencies_verified": node.dependencies_verified,
            "attempt_count": node.attempt_count,
            "max_attempts": node.max_attempts,
            "candidate_present": node.candidate_present,
            "verified_result_present": node.verified_result_present,
            "verified_failure_result_count": node.verified_failure_result_count,
            "command_plan_id": node.command_plan_id,
            "command_step_sequence": node.command_step_sequence,
            "command_profile_id": node.command_profile_id,
            "updated_at": node.updated_at,
        }
        return cls(**values, summary_digest=sha256_digest(values))


class TaskLoopExecutionWorkbenchRead(BaseModel):
    """Workbench-safe Task Loop progress without input or authority material."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-execution-workbench.v1"] = (
        "deskpilot.task-loop-execution-workbench.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    phase: TaskLoopWorkbenchPhase
    loop_status: TaskLoopReadStatus
    execution_status: TaskLoopExecutionStatus | None = None
    loop_revision: int = Field(ge=1, le=2)
    loop_event_count: int = Field(ge=1, le=2)
    execution_revision: int | None = Field(default=None, ge=1)
    execution_event_count: int = Field(default=0, ge=0)
    node_count: int = Field(ge=0, le=18)
    pending_count: int = Field(ge=0, le=18)
    ready_count: int = Field(ge=0, le=18)
    active_count: int = Field(ge=0, le=18)
    awaiting_verification_count: int = Field(ge=0, le=18)
    verified_count: int = Field(ge=0, le=18)
    waiting_user_count: int = Field(ge=0, le=18)
    failed_count: int = Field(ge=0, le=18)
    cancelled_count: int = Field(ge=0, le=18)
    candidate_count: int = Field(ge=0, le=18)
    verified_result_count: int = Field(ge=0, le=18)
    verified_failure_result_count: int = Field(default=0, ge=0)
    no_progress_count: int = Field(ge=0, le=3)
    no_progress_limit: Literal[3] = 3
    repair_count: int = Field(ge=0, le=2)
    maximum_plan_generations: Literal[3] = 3
    budget_exhausted: bool
    nodes: tuple[TaskLoopExecutionWorkbenchNodeRead, ...] = Field(default=(), max_length=18)
    recoverable: bool
    created_at: datetime
    updated_at: datetime
    projection_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def counts_and_digest_match(self) -> Self:
        if self.node_count != len(self.nodes):
            raise ValueError("Task-loop Workbench node count changed")
        expected = {
            "pending_count": sum(item.status == "pending" for item in self.nodes),
            "ready_count": sum(item.status == "ready" for item in self.nodes),
            "active_count": sum(item.status in {"claimed", "running"} for item in self.nodes),
            "awaiting_verification_count": sum(
                item.status == "awaiting_verification" for item in self.nodes
            ),
            "verified_count": sum(item.status == "verified" for item in self.nodes),
            "waiting_user_count": sum(item.status == "waiting_user" for item in self.nodes),
            "failed_count": sum(item.status == "failed" for item in self.nodes),
            "cancelled_count": sum(item.status == "cancelled" for item in self.nodes),
            "candidate_count": sum(item.candidate_present for item in self.nodes),
            "verified_result_count": sum(item.verified_result_present for item in self.nodes),
            "verified_failure_result_count": sum(
                item.verified_failure_result_count for item in self.nodes
            ),
        }
        if any(getattr(self, name) != value for name, value in expected.items()):
            raise ValueError("Task-loop Workbench counts changed")
        if (self.execution_status is None) != (self.execution_revision is None):
            raise ValueError("Task-loop Workbench execution summary is incomplete")
        if self.execution_status is None and self.execution_event_count != 0:
            raise ValueError("Pre-execution Workbench read has execution events")
        if self.execution_status is None and (
            self.no_progress_count
            or self.repair_count
            or self.budget_exhausted
        ):
            raise ValueError("Pre-execution Workbench read has cycle state")
        if self.created_at.tzinfo is None or self.updated_at.tzinfo is None:
            raise ValueError("Task-loop Workbench timestamps must be timezone-aware")
        material = self.model_dump(mode="json", exclude={"projection_digest"})
        if self.projection_digest != sha256_digest(material):
            raise ValueError("Task-loop Workbench projection digest does not match")
        return self

    @classmethod
    def from_internal(cls, read: TaskLoopExecutionRead) -> Self:
        nodes = tuple(TaskLoopExecutionWorkbenchNodeRead.from_internal(item) for item in read.nodes)
        execution = read.execution
        values: dict[str, Any] = {
            "schema_version": "deskpilot.task-loop-execution-workbench.v1",
            "task_id": read.task_id,
            "phase": read.phase,
            "loop_status": read.loop_status,
            "execution_status": execution.status if execution is not None else None,
            "loop_revision": read.loop_revision,
            "loop_event_count": read.loop_event_count,
            "execution_revision": execution.revision if execution is not None else None,
            "execution_event_count": (execution.event_count if execution is not None else 0),
            "node_count": len(nodes),
            "pending_count": sum(item.status == "pending" for item in nodes),
            "ready_count": sum(item.status == "ready" for item in nodes),
            "active_count": sum(item.status in {"claimed", "running"} for item in nodes),
            "awaiting_verification_count": sum(
                item.status == "awaiting_verification" for item in nodes
            ),
            "verified_count": sum(item.status == "verified" for item in nodes),
            "waiting_user_count": sum(item.status == "waiting_user" for item in nodes),
            "failed_count": sum(item.status == "failed" for item in nodes),
            "cancelled_count": sum(item.status == "cancelled" for item in nodes),
            "candidate_count": sum(item.candidate_present for item in nodes),
            "verified_result_count": sum(item.verified_result_present for item in nodes),
            "verified_failure_result_count": sum(
                item.verified_failure_result_count for item in nodes
            ),
            "no_progress_count": (
                read.cycle.no_progress_count if read.cycle is not None else 0
            ),
            "no_progress_limit": 3,
            "repair_count": read.cycle.repair_count if read.cycle is not None else 0,
            "maximum_plan_generations": 3,
            "budget_exhausted": (
                read.cycle.budget_exhausted if read.cycle is not None else False
            ),
            "nodes": nodes,
            "recoverable": read.recoverable,
            "created_at": read.created_at,
            "updated_at": read.updated_at,
        }
        return cls(**values, projection_digest=sha256_digest(values))


class TaskLoopNodeAttempt(BaseModel):
    """Persistent fenced attempt scaffold consumed by the stage-112B reducer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-node-attempt.v1"] = (
        "deskpilot.task-loop-node-attempt.v1"
    )
    attempt_id: str = Field(pattern=TASK_LOOP_NODE_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    node_binding_id: str = Field(pattern=MODEL_PLANNER_NODE_BINDING_ID_PATTERN)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{64}$")
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    attempt: int = Field(ge=1)
    status: TaskLoopNodeAttemptStatus
    revision: int = Field(ge=1)
    claim_owner_id: str | None = Field(default=None, min_length=1, max_length=128)
    claim_fencing_token: int = Field(ge=0)
    claim_acquired_at: datetime | None = None
    claim_expires_at: datetime | None = None
    input_manifest: dict[str, Any]
    input_digest: str = Field(pattern=DIGEST_PATTERN)
    context_manifest: dict[str, Any]
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_manifest: dict[str, Any] | None = None
    candidate_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    candidate_recorded_at: datetime | None = None
    verification_manifest: dict[str, Any] | None = None
    verification_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    verified_at: datetime | None = None
    receipt_manifest: dict[str, Any] | None = None
    receipt_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    error_code: str | None = Field(default=None, min_length=1, max_length=100)
    error_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    created_at: datetime
    updated_at: datetime
    attempt_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def lifecycle_and_digest_match(self) -> Self:
        candidate_fields = (
            self.candidate_manifest,
            self.candidate_digest,
            self.candidate_recorded_at,
        )
        verification_fields = (
            self.verification_manifest,
            self.verification_digest,
            self.verified_at,
        )
        if any(item is None for item in candidate_fields) != all(
            item is None for item in candidate_fields
        ):
            raise ValueError("Task-loop candidate persistence is incomplete")
        if any(item is None for item in verification_fields) != all(
            item is None for item in verification_fields
        ):
            raise ValueError("Task-loop verification persistence is incomplete")
        if self.verification_manifest is not None and self.candidate_manifest is None:
            raise ValueError("Task-loop verification has no persisted candidate")
        if self.candidate_manifest is not None and (
            self.candidate_manifest.get("candidate_digest") != self.candidate_digest
        ):
            raise ValueError("Task-loop candidate digest changed")
        if self.verification_manifest is not None and (
            self.verification_manifest.get("verification_digest") != self.verification_digest
        ):
            raise ValueError("Task-loop verification digest changed")
        if (self.receipt_manifest is None) != (self.receipt_digest is None):
            raise ValueError("Task-loop attempt receipt persistence is incomplete")
        if (
            self.created_at.tzinfo is None
            or self.updated_at.tzinfo is None
            or self.updated_at < self.created_at
            or (
                self.candidate_recorded_at is not None and self.candidate_recorded_at.tzinfo is None
            )
            or (self.verified_at is not None and self.verified_at.tzinfo is None)
        ):
            raise ValueError("Task-loop attempt timestamps are invalid")
        material = self.model_dump(mode="json", exclude={"attempt_digest"})
        if self.attempt_digest != sha256_digest(material):
            raise ValueError("Task-loop node attempt digest does not match")
        return self


class TaskLoopVerifiedResult(BaseModel):
    """Immutable verified ResultRef; unverified candidates never enter this table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.task-loop-verified-result.v1"] = (
        "deskpilot.task-loop-verified-result.v1"
    )
    result_ref_id: str = Field(pattern=TASK_LOOP_RESULT_REF_ID_PATTERN)
    attempt_id: str = Field(pattern=TASK_LOOP_NODE_ATTEMPT_ID_PATTERN)
    execution_id: str = Field(pattern=TASK_LOOP_EXECUTION_ID_PATTERN)
    node_binding_id: str = Field(pattern=MODEL_PLANNER_NODE_BINDING_ID_PATTERN)
    node_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    run_id: str = Field(pattern=r"^run_[0-9a-f]{64}$")
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    producer_kind: Literal["capability_executor", "agent_bridge"]
    capability_manifest: dict[str, Any]
    capability_digest: str = Field(pattern=DIGEST_PATTERN)
    agent_binding_manifest: dict[str, Any] | None = None
    agent_binding_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    executor_manifest_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    agent_result_proof_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    input_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    context_digest: str = Field(pattern=DIGEST_PATTERN)
    candidate_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    result_kind: str = Field(min_length=1, max_length=64)
    output_manifest: dict[str, Any]
    output_schema_digest: str = Field(pattern=DIGEST_PATTERN)
    output_digest: str = Field(pattern=DIGEST_PATTERN)
    verification_manifest: dict[str, Any]
    verification_digest: str = Field(pattern=DIGEST_PATTERN)
    result_ref_manifest: dict[str, Any]
    result_ref_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime

    @model_validator(mode="after")
    def producer_lineage_and_digests_match(self) -> Self:
        if self.capability_digest != sha256_digest(self.capability_manifest):
            raise ValueError("Verified result capability digest does not match")
        if self.producer_kind == "capability_executor":
            if (
                self.agent_binding_manifest is not None
                or self.agent_binding_digest is not None
                or self.agent_result_proof_digest is not None
                or self.executor_manifest_digest is None
                or self.candidate_digest is None
            ):
                raise ValueError("Capability-executor result producer proof is invalid")
        elif (
            self.agent_binding_manifest is None
            or self.agent_binding_digest is None
            or self.agent_result_proof_digest is None
            or self.executor_manifest_digest is not None
            or self.candidate_digest is not None
        ):
            raise ValueError("Agent-bridge result producer proof is invalid")
        if self.agent_binding_manifest is not None and (
            self.agent_binding_digest != sha256_digest(self.agent_binding_manifest)
        ):
            raise ValueError("Verified result Agent binding digest does not match")
        if self.output_manifest.get("result_digest") != self.output_digest:
            raise ValueError("Verified result output digest changed")
        if self.verification_manifest.get("verification_digest") != self.verification_digest:
            raise ValueError("Verified result verification digest changed")
        if self.result_ref_manifest.get("result_ref_digest") != self.result_ref_digest:
            raise ValueError("Verified ResultRef digest column changed")
        ref_material = {
            key: value
            for key, value in self.result_ref_manifest.items()
            if key != "result_ref_digest"
        }
        if self.result_ref_digest != sha256_digest(ref_material):
            raise ValueError("Verified ResultRef manifest is not content addressed")
        if self.created_at.tzinfo is None:
            raise ValueError("Verified result timestamp must be timezone-aware")
        return self
