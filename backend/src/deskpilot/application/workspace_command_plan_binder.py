"""Bind server-compiled command Plans to exact ModelPlanner nodes."""

from __future__ import annotations

from collections.abc import Iterable
from typing import cast

from deskpilot.application.model_planner_composer import RevalidatedOfferStep
from deskpilot.application.workspace_command_plan_compiler import (
    WorkspaceCommandPlanCompiler,
    WorkspaceCommandPlanRejectedError,
)
from deskpilot.domain.command_profiles import CommandProfileId
from deskpilot.domain.task_loop import ModelPlannerDraft, ModelPlannerStepBinding
from deskpilot.domain.workspace_command_plans import (
    WorkspaceCommandPlanBinding,
    WorkspaceCommandPlanNodeMapping,
)


class WorkspaceCommandPlanBindingError(ValueError):
    code = "WORKSPACE_COMMAND_PLAN_BINDING_REJECTED"


class WorkspaceCommandPlanBinder:
    """Compile contiguous command Offers and bind every step one-to-one."""

    def __init__(self, compiler: WorkspaceCommandPlanCompiler) -> None:
        self._compiler = compiler

    def bind(
        self,
        *,
        loop_id: str,
        draft: ModelPlannerDraft,
        steps: tuple[ModelPlannerStepBinding, ...],
        current_steps: tuple[RevalidatedOfferStep, ...],
    ) -> tuple[WorkspaceCommandPlanBinding, ...]:
        if len(steps) != len(current_steps) or tuple(item.ordinal for item in steps) != tuple(
            range(1, len(steps) + 1)
        ):
            raise WorkspaceCommandPlanBindingError(
                "Workspace command Plan source steps are not exact or contiguous"
            )
        groups: list[
            list[tuple[ModelPlannerStepBinding, RevalidatedOfferStep, str]]
        ] = []
        current_group: list[
            tuple[ModelPlannerStepBinding, RevalidatedOfferStep, str]
        ] = []
        current_key: tuple[str, str] | None = None
        for persisted, current in zip(steps, current_steps, strict=True):
            if current.route.route_id != "workspace_command_profile":
                if current_group:
                    groups.append(current_group)
                    current_group = []
                    current_key = None
                continue
            project_path = current.parameters.get("project_path")
            raw_profile_id = current.route.fixed_parameters.get("command_profile_id")
            if not project_path or not isinstance(raw_profile_id, str):
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command Offer lost its project or fixed Profile"
                )
            try:
                singleton = self._compiler.compile(
                    task_id=draft.source.task_id,
                    plan_generation=draft.expected_plan.plan_generation,
                    project_path=project_path,
                    command_profile_ids=(cast(CommandProfileId, raw_profile_id),),
                )
            except WorkspaceCommandPlanRejectedError as error:
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command Offer no longer has a safe current target"
                ) from error
            normalized_project_path = singleton.request.project_path
            if project_path != normalized_project_path:
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command Offer project path is not canonical"
                )
            group_key = (normalized_project_path, singleton.ecosystem)
            if current_group and current_key != group_key:
                groups.append(current_group)
                current_group = []
            current_group.append((persisted, current, normalized_project_path))
            current_key = group_key
        if current_group:
            groups.append(current_group)

        bindings: list[WorkspaceCommandPlanBinding] = []
        for group_ordinal, group in enumerate(groups, start=1):
            bindings.append(
                self._bind_group(
                    loop_id=loop_id,
                    draft=draft,
                    group_ordinal=group_ordinal,
                    group=group,
                )
            )
        return tuple(bindings)

    def revalidate(self, binding: WorkspaceCommandPlanBinding) -> None:
        request = binding.command_plan.request
        try:
            current = self._compiler.compile(
                task_id=request.task_id,
                plan_generation=request.plan_generation,
                project_path=request.project_path,
                command_profile_ids=request.command_profile_ids,
            )
        except WorkspaceCommandPlanRejectedError as error:
            raise WorkspaceCommandPlanBindingError(
                "Workspace command Plan no longer has a safe current target"
            ) from error
        if current != binding.command_plan:
            raise WorkspaceCommandPlanBindingError(
                "Workspace command Plan changed from its persisted Catalog or project proof"
            )

    def revalidate_all(
        self,
        bindings: Iterable[WorkspaceCommandPlanBinding],
    ) -> None:
        for binding in bindings:
            self.revalidate(binding)

    def _bind_group(
        self,
        *,
        loop_id: str,
        draft: ModelPlannerDraft,
        group_ordinal: int,
        group: list[tuple[ModelPlannerStepBinding, RevalidatedOfferStep, str]],
    ) -> WorkspaceCommandPlanBinding:
        project_paths = {normalized_path for _, _, normalized_path in group}
        profile_ids = tuple(
            item.route.fixed_parameters.get("command_profile_id")
            for _, item, _ in group
        )
        if len(project_paths) != 1 or None in project_paths or any(
            item is None for item in profile_ids
        ):
            raise WorkspaceCommandPlanBindingError(
                "Workspace command group changed project or fixed Profile"
            )
        try:
            command_plan = self._compiler.compile(
                task_id=draft.source.task_id,
                plan_generation=draft.expected_plan.plan_generation,
                project_path=next(iter(project_paths)),
                command_profile_ids=tuple(
                    cast(CommandProfileId, item) for item in profile_ids
                ),
            )
        except WorkspaceCommandPlanRejectedError as error:
            raise WorkspaceCommandPlanBindingError(
                "Workspace command group could not be compiled"
            ) from error
        plan_nodes = {item.node_id: item for item in draft.expected_plan.nodes}
        mappings: list[WorkspaceCommandPlanNodeMapping] = []
        for command_step, (persisted, current, _) in zip(
            command_plan.steps,
            group,
            strict=True,
        ):
            if (
                persisted.offer != current.offer.ref
                or persisted.recipe != current.offer.trusted_recipe
                or persisted.recipe.route_id != "workspace_command_profile"
            ):
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command step changed its Offer or recipe"
                )
            candidates = tuple(
                item
                for item in persisted.node_mappings
                if item.source_local_key == "workspace_command_profile"
            )
            if len(candidates) != 1:
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command Offer has no exact operational node mapping"
                )
            node_mapping = candidates[0]
            composite_node = plan_nodes.get(node_mapping.composite_node_id)
            if (
                composite_node is None
                or composite_node.node_spec_digest
                != node_mapping.composite_node_spec_digest
                or composite_node.capability is None
                or composite_node.capability.capability_id != "workspace.command.run.v1"
            ):
                raise WorkspaceCommandPlanBindingError(
                    "Workspace command mapping does not target its exact capability node"
                )
            mappings.append(
                WorkspaceCommandPlanNodeMapping.build(
                    command_step_id=command_step.step_id,
                    command_step_digest=command_step.step_digest,
                    command_step_sequence=command_step.sequence,
                    step_binding_id=persisted.step_binding_id,
                    step_binding_digest=persisted.step_binding_digest,
                    step_ordinal=persisted.ordinal,
                    offer_id=persisted.offer.offer_id,
                    offer_key=persisted.offer.offer_key,
                    offer_digest=persisted.offer.offer_digest,
                    composite_node_id=node_mapping.composite_node_id,
                    composite_node_spec_digest=node_mapping.composite_node_spec_digest,
                )
            )
        return WorkspaceCommandPlanBinding.build(
            task_id=draft.source.task_id,
            loop_id=loop_id,
            draft_id=draft.draft_id,
            group_ordinal=group_ordinal,
            expected_plan_id=draft.expected_plan.plan_id,
            expected_plan_manifest_digest=draft.expected_plan_manifest_digest,
            command_plan=command_plan,
            mappings=tuple(mappings),
            created_at=draft.created_at,
        )

__all__ = [
    "WorkspaceCommandPlanBinder",
    "WorkspaceCommandPlanBindingError",
]
