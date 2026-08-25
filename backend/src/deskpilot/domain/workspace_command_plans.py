"""Immutable contracts for server-compiled workspace command plans."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.command_profiles import CommandProfile, CommandProfileId
from deskpilot.domain.task_plans import TASK_ID_PATTERN

WORKSPACE_COMMAND_PLAN_ID_PATTERN = r"^wcp_[0-9a-f]{64}$"
WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN = r"^wcs_[0-9a-f]{64}$"
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


__all__ = [
    "WORKSPACE_COMMAND_PLAN_ID_PATTERN",
    "WORKSPACE_COMMAND_PLAN_STEP_ID_PATTERN",
    "WorkspaceCommandPlan",
    "WorkspaceCommandPlanRequest",
    "WorkspaceCommandPlanStep",
    "WorkspaceCommandPlanStepId",
]
