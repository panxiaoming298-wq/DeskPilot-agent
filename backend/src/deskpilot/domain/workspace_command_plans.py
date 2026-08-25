"""Immutable contracts for server-compiled workspace command plans."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.command_profiles import CommandProfile, CommandProfileId
from deskpilot.domain.task_loop import (
    MODEL_PLANNER_DRAFT_ID_PATTERN,
    MODEL_PLANNER_STEP_BINDING_ID_PATTERN,
    TASK_LOOP_ID_PATTERN,
)
from deskpilot.domain.task_plans import PLAN_ID_PATTERN, PLAN_NODE_ID_PATTERN, TASK_ID_PATTERN
from deskpilot.domain.turn_planning import (
    TURN_PLANNING_OFFER_ID_PATTERN,
    TURN_PLANNING_OFFER_KEY_PATTERN,
)

WORKSPACE_COMMAND_PLAN_ID_PATTERN = r"^wcp_[0-9a-f]{64}$"
WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN = r"^wcs_[0-9a-f]{64}$"
WORKSPACE_COMMAND_PLAN_MAPPING_ID_PATTERN = r"^wcm_[0-9a-f]{64}$"
WORKSPACE_COMMAND_PLAN_BINDING_ID_PATTERN = r"^wcb_[0-9a-f]{64}$"
WorkspaceCommandPlanStepId = Annotated[
    str,
    Field(pattern=WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN),
]


class WorkspaceCommandPlanRequest(BaseModel):
    """Structured selection accepted by the compiler; never a process specification."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan-request.v1"] = (
        "deskpilot.workspace-command-plan-request.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    plan_generation: int = Field(ge=1)
    project_path: str = Field(min_length=1, max_length=32_767)
    command_profile_ids: tuple[CommandProfileId, ...] = Field(min_length=1, max_length=6)
    request_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def selection_and_digest_match(self) -> Self:
        if len(self.command_profile_ids) != len(set(self.command_profile_ids)):
            raise ValueError("Workspace command plan request repeats one Command Profile")
        material = self.model_dump(mode="json", exclude={"request_digest"})
        if self.request_digest != sha256_digest(material):
            raise ValueError("Workspace command plan request digest does not match")
        return self

    @classmethod
    def build(cls, **values: object) -> Self:
        material = {
            "schema_version": "deskpilot.workspace-command-plan-request.v1",
            **values,
        }
        return cls.model_validate({**material, "request_digest": sha256_digest(material)})


class WorkspaceCommandPlanStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan-step.v1"] = (
        "deskpilot.workspace-command-plan-step.v1"
    )
    step_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN)
    sequence: int = Field(ge=1, le=6)
    depends_on: tuple[WorkspaceCommandPlanStepId, ...] = Field(default=(), max_length=1)
    command_profile: CommandProfile
    step_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        identity = self.model_dump(mode="json", exclude={"step_id", "step_digest"})
        if self.step_id != f"wcs_{sha256_digest(identity)}":
            raise ValueError("Workspace command plan step id does not match")
        material = self.model_dump(mode="json", exclude={"step_digest"})
        if self.step_digest != sha256_digest(material):
            raise ValueError("Workspace command plan step digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        sequence: int,
        depends_on: tuple[WorkspaceCommandPlanStepId, ...],
        command_profile: CommandProfile,
    ) -> Self:
        identity = {
            "schema_version": "deskpilot.workspace-command-plan-step.v1",
            "sequence": sequence,
            "depends_on": depends_on,
            "command_profile": command_profile,
        }
        material = {**identity, "step_id": f"wcs_{sha256_digest(identity)}"}
        return cls.model_validate({**material, "step_digest": sha256_digest(material)})


class WorkspaceCommandPlan(BaseModel):
    """Exact server-owned command chain bound to one task plan generation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan.v1"] = (
        "deskpilot.workspace-command-plan.v1"
    )
    plan_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_ID_PATTERN)
    request: WorkspaceCommandPlanRequest
    ecosystem: Literal["python", "node"]
    catalog_digest: str = Field(pattern=DIGEST_PATTERN)
    steps: tuple[WorkspaceCommandPlanStep, ...] = Field(min_length=1, max_length=6)
    total_timeout_seconds: int = Field(ge=5, le=3_600)
    stop_on_failure: Literal[True] = True
    network_access: Literal[False] = False
    temporary_snapshot_per_step: Literal[True] = True
    caller_supplies_process_fields: Literal[False] = False
    plan_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def chain_identity_and_digest_match(self) -> Self:
        if tuple(step.sequence for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("Workspace command plan sequences are not contiguous")
        step_ids = tuple(step.step_id for step in self.steps)
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("Workspace command plan contains duplicate steps")
        for index, step in enumerate(self.steps):
            expected_dependency = () if index == 0 else (self.steps[index - 1].step_id,)
            if step.depends_on != expected_dependency:
                raise ValueError("Workspace command plan is not one fail-closed serial chain")
        profiles = tuple(step.command_profile for step in self.steps)
        if tuple(profile.command_profile_id for profile in profiles) != (
            self.request.command_profile_ids
        ):
            raise ValueError("Workspace command plan profiles changed from its request")
        if any(profile.ecosystem != self.ecosystem for profile in profiles):
            raise ValueError("Workspace command plan mixes ecosystems")
        if sum(profile.timeout_seconds for profile in profiles) != self.total_timeout_seconds:
            raise ValueError("Workspace command plan timeout budget does not match")
        identity = self.model_dump(mode="json", exclude={"plan_id", "plan_digest"})
        if self.plan_id != f"wcp_{sha256_digest(identity)}":
            raise ValueError("Workspace command plan id does not match")
        material = self.model_dump(mode="json", exclude={"plan_digest"})
        if self.plan_digest != sha256_digest(material):
            raise ValueError("Workspace command plan digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        request: WorkspaceCommandPlanRequest,
        ecosystem: Literal["python", "node"],
        catalog_digest: str,
        steps: tuple[WorkspaceCommandPlanStep, ...],
    ) -> Self:
        identity = {
            "schema_version": "deskpilot.workspace-command-plan.v1",
            "request": request,
            "ecosystem": ecosystem,
            "catalog_digest": catalog_digest,
            "steps": steps,
            "total_timeout_seconds": sum(
                step.command_profile.timeout_seconds for step in steps
            ),
            "stop_on_failure": True,
            "network_access": False,
            "temporary_snapshot_per_step": True,
            "caller_supplies_process_fields": False,
        }
        material = {**identity, "plan_id": f"wcp_{sha256_digest(identity)}"}
        return cls.model_validate({**material, "plan_digest": sha256_digest(material)})


class WorkspaceCommandPlanNodeMapping(BaseModel):
    """Exact command step to ModelPlanner step and composite-node proof."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan-node-mapping.v1"] = (
        "deskpilot.workspace-command-plan-node-mapping.v1"
    )
    mapping_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_MAPPING_ID_PATTERN)
    command_step_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN)
    command_step_digest: str = Field(pattern=DIGEST_PATTERN)
    command_step_sequence: int = Field(ge=1, le=6)
    step_binding_id: str = Field(pattern=MODEL_PLANNER_STEP_BINDING_ID_PATTERN)
    step_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    step_ordinal: int = Field(ge=1, le=8)
    offer_id: str = Field(pattern=TURN_PLANNING_OFFER_ID_PATTERN)
    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    offer_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    composite_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    mapping_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> Self:
        identity = self.model_dump(mode="json", exclude={"mapping_id", "mapping_digest"})
        if self.mapping_id != f"wcm_{sha256_digest(identity)}":
            raise ValueError("Workspace command Plan node mapping id does not match")
        material = self.model_dump(mode="json", exclude={"mapping_digest"})
        if self.mapping_digest != sha256_digest(material):
            raise ValueError("Workspace command Plan node mapping digest does not match")
        return self

    @classmethod
    def build(cls, **values: object) -> Self:
        identity = {
            "schema_version": "deskpilot.workspace-command-plan-node-mapping.v1",
            **values,
        }
        material = {**identity, "mapping_id": f"wcm_{sha256_digest(identity)}"}
        return cls.model_validate({**material, "mapping_digest": sha256_digest(material)})


class WorkspaceCommandPlanBinding(BaseModel):
    """Persisted authority binding from one command Plan to one sealed Draft."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan-binding.v1"] = (
        "deskpilot.workspace-command-plan-binding.v1"
    )
    binding_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_BINDING_ID_PATTERN)
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    loop_id: str = Field(pattern=TASK_LOOP_ID_PATTERN)
    draft_id: str = Field(pattern=MODEL_PLANNER_DRAFT_ID_PATTERN)
    group_ordinal: int = Field(ge=1, le=8)
    expected_plan_id: str = Field(pattern=PLAN_ID_PATTERN)
    expected_plan_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    command_plan: WorkspaceCommandPlan
    mappings: tuple[WorkspaceCommandPlanNodeMapping, ...] = Field(
        min_length=1,
        max_length=6,
    )
    mappings_digest: str = Field(pattern=DIGEST_PATTERN)
    created_at: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def scope_identity_and_digest_match(self) -> Self:
        if self.created_at.tzinfo is None:
            raise ValueError("Workspace command Plan binding timestamp must be timezone-aware")
        if (
            self.command_plan.request.task_id != self.task_id
            or self.command_plan.request.plan_generation != 1
            or len(self.mappings) != len(self.command_plan.steps)
        ):
            raise ValueError("Workspace command Plan binding crosses its Task or generation")
        if tuple(item.command_step_sequence for item in self.mappings) != tuple(
            range(1, len(self.mappings) + 1)
        ):
            raise ValueError("Workspace command Plan mapping sequence is not contiguous")
        if any(
            mapping.command_step_id != step.step_id
            or mapping.command_step_digest != step.step_digest
            for mapping, step in zip(self.mappings, self.command_plan.steps, strict=True)
        ):
            raise ValueError("Workspace command Plan mapping changed its exact step")
        unique_groups = (
            tuple(item.mapping_id for item in self.mappings),
            tuple(item.step_binding_id for item in self.mappings),
            tuple(item.step_ordinal for item in self.mappings),
            tuple(item.offer_id for item in self.mappings),
            tuple(item.offer_key for item in self.mappings),
            tuple(item.composite_node_id for item in self.mappings),
        )
        if any(len(values) != len(set(values)) for values in unique_groups):
            raise ValueError("Workspace command Plan mapping is not one-to-one")
        expected_mappings_digest = sha256_digest(
            {"mappings": [item.model_dump(mode="json") for item in self.mappings]}
        )
        if self.mappings_digest != expected_mappings_digest:
            raise ValueError("Workspace command Plan mapping-set digest does not match")
        values = self.model_dump(mode="json")
        identity = {
            key: value
            for key, value in values.items()
            if key not in {"binding_id", "binding_digest"}
        }
        if self.binding_id != f"wcb_{sha256_digest(identity)}":
            raise ValueError("Workspace command Plan binding id does not match")
        material = {key: value for key, value in values.items() if key != "binding_digest"}
        if self.binding_digest != sha256_digest(material):
            raise ValueError("Workspace command Plan binding digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        task_id: str,
        loop_id: str,
        draft_id: str,
        group_ordinal: int,
        expected_plan_id: str,
        expected_plan_manifest_digest: str,
        command_plan: WorkspaceCommandPlan,
        mappings: tuple[WorkspaceCommandPlanNodeMapping, ...],
        created_at: datetime,
    ) -> Self:
        values = {
            "schema_version": "deskpilot.workspace-command-plan-binding.v1",
            "task_id": task_id,
            "loop_id": loop_id,
            "draft_id": draft_id,
            "group_ordinal": group_ordinal,
            "expected_plan_id": expected_plan_id,
            "expected_plan_manifest_digest": expected_plan_manifest_digest,
            "command_plan": command_plan,
            "mappings": mappings,
            "mappings_digest": sha256_digest(
                {"mappings": [item.model_dump(mode="json") for item in mappings]}
            ),
            "created_at": created_at,
        }
        binding_id = f"wcb_{sha256_digest(values)}"
        material = {**values, "binding_id": binding_id}
        return cls.model_validate(
            {**material, "binding_digest": sha256_digest(material)}
        )

    def proof_for_node(self, node_id: str) -> WorkspaceCommandPlanStepProof:
        matches = tuple(item for item in self.mappings if item.composite_node_id == node_id)
        if len(matches) != 1:
            raise ValueError("Workspace command Plan node has no exact step mapping")
        mapping = matches[0]
        step = self.command_plan.steps[mapping.command_step_sequence - 1]
        return WorkspaceCommandPlanStepProof.build(
            binding=self,
            mapping=mapping,
            step=step,
        )


class WorkspaceCommandPlanStepProof(BaseModel):
    """Minimal persisted proof projected into one command capability input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-plan-step-proof.v1"] = (
        "deskpilot.workspace-command-plan-step-proof.v1"
    )
    binding_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_BINDING_ID_PATTERN)
    binding_digest: str = Field(pattern=DIGEST_PATTERN)
    command_plan_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_ID_PATTERN)
    command_plan_digest: str = Field(pattern=DIGEST_PATTERN)
    request_digest: str = Field(pattern=DIGEST_PATTERN)
    catalog_digest: str = Field(pattern=DIGEST_PATTERN)
    plan_generation: int = Field(ge=1)
    project_path: str = Field(min_length=1, max_length=32_767)
    command_step_id: str = Field(pattern=WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN)
    command_step_digest: str = Field(pattern=DIGEST_PATTERN)
    command_step_sequence: int = Field(ge=1, le=6)
    command_profile_id: CommandProfileId
    command_profile_digest: str = Field(pattern=DIGEST_PATTERN)
    step_binding_id: str = Field(pattern=MODEL_PLANNER_STEP_BINDING_ID_PATTERN)
    step_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    offer_id: str = Field(pattern=TURN_PLANNING_OFFER_ID_PATTERN)
    offer_key: str = Field(pattern=TURN_PLANNING_OFFER_KEY_PATTERN)
    offer_digest: str = Field(pattern=DIGEST_PATTERN)
    composite_node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    composite_node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    proof_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def digest_matches(self) -> Self:
        material = self.model_dump(mode="json", exclude={"proof_digest"})
        if self.proof_digest != sha256_digest(material):
            raise ValueError("Workspace command Plan step proof digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        binding: WorkspaceCommandPlanBinding,
        mapping: WorkspaceCommandPlanNodeMapping,
        step: WorkspaceCommandPlanStep,
    ) -> Self:
        plan = binding.command_plan
        values = {
            "schema_version": "deskpilot.workspace-command-plan-step-proof.v1",
            "binding_id": binding.binding_id,
            "binding_digest": binding.binding_digest,
            "command_plan_id": plan.plan_id,
            "command_plan_digest": plan.plan_digest,
            "request_digest": plan.request.request_digest,
            "catalog_digest": plan.catalog_digest,
            "plan_generation": plan.request.plan_generation,
            "project_path": plan.request.project_path,
            "command_step_id": step.step_id,
            "command_step_digest": step.step_digest,
            "command_step_sequence": step.sequence,
            "command_profile_id": step.command_profile.command_profile_id,
            "command_profile_digest": step.command_profile.profile_digest,
            "step_binding_id": mapping.step_binding_id,
            "step_binding_digest": mapping.step_binding_digest,
            "offer_id": mapping.offer_id,
            "offer_key": mapping.offer_key,
            "offer_digest": mapping.offer_digest,
            "composite_node_id": mapping.composite_node_id,
            "composite_node_spec_digest": mapping.composite_node_spec_digest,
        }
        return cls.model_validate({**values, "proof_digest": sha256_digest(values)})


__all__ = [
    "WORKSPACE_COMMAND_PLAN_ID_PATTERN",
    "WORKSPACE_COMMAND_PLAN_BINDING_ID_PATTERN",
    "WORKSPACE_COMMAND_PLAN_MAPPING_ID_PATTERN",
    "WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN",
    "WorkspaceCommandPlan",
    "WorkspaceCommandPlanBinding",
    "WorkspaceCommandPlanNodeMapping",
    "WorkspaceCommandPlanRequest",
    "WorkspaceCommandPlanStep",
    "WorkspaceCommandPlanStepId",
    "WorkspaceCommandPlanStepProof",
]
