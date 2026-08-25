"""Compile structured command selections into exact server-owned command chains."""

from pathlib import Path
from typing import Protocol

from deskpilot.application.command_profile_catalog import (
    CommandProfileCatalog,
    CommandProfileNotFoundError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.command_profiles import CommandProfileId
from deskpilot.domain.workspace_command_plans import (
    WorkspaceCommandPlan,
    WorkspaceCommandPlanRequest,
    WorkspaceCommandPlanStep,
)


class WorkspaceCommandPlanRejectedError(ValueError):
    code = "WORKSPACE_COMMAND_PLAN_REJECTED"


class WorkspaceCommandProjectResolver(Protocol):
    def resolve_project_directory(self, relative_path: str) -> tuple[Path, str]: ...


class WorkspaceCommandPlanCompiler:
    """Server compiler; its public input surface deliberately has no process fields."""

    def __init__(
        self,
        profiles: CommandProfileCatalog,
        projects: WorkspaceCommandProjectResolver,
    ) -> None:
        self._profiles = profiles
        self._projects = projects

    @property
    def catalog_digest(self) -> str:
        return sha256_digest(
            {
                "schema_version": "deskpilot.command-profile-catalog.v1",
                "profiles": self._profiles.list(),
            }
        )

    def compile(
        self,
        *,
        task_id: str,
        plan_generation: int,
        project_path: str,
        command_profile_ids: tuple[CommandProfileId, ...],
    ) -> WorkspaceCommandPlan:
        try:
            _, normalized_project_path = self._projects.resolve_project_directory(project_path)
        except (OSError, RuntimeError, ValueError) as error:
            raise WorkspaceCommandPlanRejectedError(
                "Workspace command target is not one safe project directory"
            ) from error
        try:
            request = WorkspaceCommandPlanRequest.build(
                task_id=task_id,
                plan_generation=plan_generation,
                project_path=normalized_project_path,
                command_profile_ids=command_profile_ids,
            )
        except ValueError as error:
            raise WorkspaceCommandPlanRejectedError(
                "Workspace command selection is invalid"
            ) from error
        try:
            profiles = tuple(
                self._profiles.resolve(profile_id) for profile_id in command_profile_ids
            )
        except CommandProfileNotFoundError as error:
            raise WorkspaceCommandPlanRejectedError(
                "Workspace command selection is not registered"
            ) from error
        ecosystems = {profile.ecosystem for profile in profiles}
        if len(ecosystems) != 1:
            raise WorkspaceCommandPlanRejectedError(
                "Workspace command plan cannot mix project ecosystems"
            )
        steps: list[WorkspaceCommandPlanStep] = []
        for sequence, profile in enumerate(profiles, start=1):
            depends_on = (
                ()
                if not steps
                else (steps[-1].step_id,)
            )
            steps.append(
                WorkspaceCommandPlanStep.build(
                    sequence=sequence,
                    depends_on=depends_on,
                    command_profile=profile,
                )
            )
        return WorkspaceCommandPlan.build(
            request=request,
            ecosystem=profiles[0].ecosystem,
            catalog_digest=self.catalog_digest,
            steps=tuple(steps),
        )


__all__ = [
    "WorkspaceCommandPlanCompiler",
    "WorkspaceCommandPlanRejectedError",
    "WorkspaceCommandProjectResolver",
]
