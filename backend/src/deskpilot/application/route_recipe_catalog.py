"""Versioned server-owned recipes behind deterministic and proposed Turn routes."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Literal, cast

from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.plan_compiler import (
    knowledge_lookup_contract,
    knowledge_lookup_draft,
    mcp_text_metrics_contract,
    mcp_text_metrics_draft,
    research_to_html_contract,
    research_to_html_draft,
    workspace_agent_patch_test_contract,
    workspace_agent_patch_test_draft,
    workspace_coding_loop_contract,
    workspace_coding_loop_draft,
    workspace_command_profile_contract,
    workspace_command_profile_draft,
    workspace_directory_analyze_contract,
    workspace_directory_analyze_draft,
    workspace_directory_list_contract,
    workspace_directory_list_draft,
    workspace_dynamic_patch_test_contract,
    workspace_dynamic_patch_test_draft,
    workspace_file_create_contract,
    workspace_file_create_draft,
    workspace_file_read_contract,
    workspace_file_read_draft,
    workspace_file_rename_contract,
    workspace_file_rename_draft,
    workspace_file_replace_contract,
    workspace_file_replace_draft,
    workspace_git_inspect_contract,
    workspace_git_inspect_draft,
    workspace_node_test_contract,
    workspace_node_test_draft,
    workspace_patch_bundle_contract,
    workspace_patch_bundle_draft,
    workspace_project_batch_read_contract,
    workspace_project_batch_read_draft,
    workspace_project_search_contract,
    workspace_project_search_draft,
    workspace_python_test_contract,
    workspace_python_test_draft,
    workspace_snapshot_check_contract,
    workspace_snapshot_check_draft,
)
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.command_profiles import COMMAND_PROFILE_IDS
from deskpilot.domain.task_plans import DraftPlan, TaskContract

RouteId = Literal[
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
    "workspace_project_search",
    "workspace_project_batch_read",
    "workspace_git_inspect",
    "workspace_command_profile",
    "workspace_coding_loop",
]
RouteRecipeVersion = Literal["1", "2"]


class RouteRecipeError(RuntimeError):
    code = "TURN_ROUTE_RECIPE_REJECTED"


@dataclass(frozen=True, slots=True)
class RouteParameterSpec:
    name: str
    required: bool = True
    allowed_values: tuple[str, ...] = ()

    def manifest(self) -> dict[str, object]:
        return {
            "name": self.name,
            "required": self.required,
            "source": "persisted_user_message_exact_substring",
            "normalization": "server_unquote_then_casefold_enum"
            if self.allowed_values
            else "server_unquote",
            "allowed_values": list(self.allowed_values),
        }


@dataclass(frozen=True, slots=True)
class CompiledRouteRecipe:
    route_id: RouteId
    route_version: RouteRecipeVersion
    recipe_manifest: dict[str, object]
    recipe_digest: str
    parameter_binding_manifest: dict[str, object]
    parameter_binding_digest: str
    parameters: dict[str, str]
    contract: TaskContract
    draft: DraftPlan


@dataclass(frozen=True, slots=True)
class RouteOfferDraft:
    """A fully compiled server recipe before it receives an opaque offer key."""

    variant_key: str
    route_id: RouteId
    route_version: Literal["2"]
    recipe_manifest: dict[str, object]
    recipe_digest: str
    parameter_specs: tuple[RouteParameterSpec, ...]
    fixed_parameters: dict[str, str]
    contract: TaskContract
    draft: DraftPlan


# This material is deliberately byte-for-byte equivalent at the canonical JSON
# layer to the former phase-78 in-module mapping. Historical v1 route digests
# therefore continue to replay without migration or baseline changes.
_LEGACY_ROUTE_SPECS: Mapping[RouteId, dict[str, object]] = MappingProxyType(
    {
        "research_to_html": {
            "route_id": "research_to_html",
            "version": "1",
            "producer_ref": "research_to_html.v1",
            "capabilities": ("research.read.v1", "artifact.html.v1", "browser.verify.v1"),
            "max_risk": "R1",
        },
        "knowledge_lookup": {
            "route_id": "knowledge_lookup",
            "version": "1",
            "producer_ref": "knowledge_lookup.v1",
            "capabilities": ("knowledge.local.v1",),
            "max_risk": "R0",
        },
        "mcp_text_metrics": {
            "route_id": "mcp_text_metrics",
            "version": "1",
            "producer_ref": "mcp_text_metrics.v1",
            "capabilities": ("mcp.text.metrics.v1",),
            "max_risk": "R0",
        },
        "workspace_file_read": {
            "route_id": "workspace_file_read",
            "version": "1",
            "producer_ref": "workspace_file_read.v1",
            "capabilities": ("workspace.file.read.v1",),
            "max_risk": "R0",
        },
        "workspace_file_replace": {
            "route_id": "workspace_file_replace",
            "version": "1",
            "producer_ref": "workspace_file_replace.v1",
            "capabilities": ("workspace.file.replace.v1",),
            "max_risk": "R1",
        },
        "workspace_patch_bundle": {
            "route_id": "workspace_patch_bundle",
            "version": "1",
            "producer_ref": "workspace_patch_bundle.v1",
            "capabilities": ("workspace.patch.bundle.v1",),
            "max_risk": "R1",
        },
        "workspace_agent_patch_test": {
            "route_id": "workspace_agent_patch_test",
            "version": "1",
            "producer_ref": "workspace_agent_patch_test.v1",
            "capabilities": (
                "workspace.file.read.v1",
                "workspace.patch.propose.v1",
                "workspace.patch.bundle.v1",
                "workspace.python.test.v1",
                "workspace.node.test.v1",
            ),
            "max_risk": "R1",
        },
        "workspace_dynamic_patch_test": {
            "route_id": "workspace_dynamic_patch_test",
            "version": "1",
            "producer_ref": "workspace_dynamic_patch_test.v1",
            "capabilities": (
                "workspace.directory.read.v1",
                "workspace.file.read.v1",
                "workspace.patch.propose.v1",
                "workspace.patch.bundle.v1",
                "workspace.python.test.v1",
                "workspace.node.test.v1",
            ),
            "max_risk": "R1",
        },
        "workspace_file_create": {
            "route_id": "workspace_file_create",
            "version": "1",
            "producer_ref": "workspace_file_create.v1",
            "capabilities": ("workspace.file.create.v1",),
            "max_risk": "R1",
        },
        "workspace_file_rename": {
            "route_id": "workspace_file_rename",
            "version": "1",
            "producer_ref": "workspace_file_rename.v1",
            "capabilities": ("workspace.file.rename.v1",),
            "max_risk": "R1",
        },
        "workspace_directory_list": {
            "route_id": "workspace_directory_list",
            "version": "1",
            "producer_ref": "workspace_directory_list.v1",
            "capabilities": ("workspace.directory.read.v1",),
            "max_risk": "R0",
        },
        "workspace_directory_analyze": {
            "route_id": "workspace_directory_analyze",
            "version": "1",
            "producer_ref": "workspace_directory_analyze.v1",
            "capabilities": ("workspace.directory.read.v1", "workspace.file.read.v1"),
            "max_risk": "R0",
        },
        "workspace_snapshot_check": {
            "route_id": "workspace_snapshot_check",
            "version": "1",
            "producer_ref": "workspace_snapshot_check.v1",
            "capabilities": ("workspace.snapshot.check.v1",),
            "max_risk": "R0",
        },
        "workspace_python_test": {
            "route_id": "workspace_python_test",
            "version": "1",
            "producer_ref": "workspace_python_test.v1",
            "capabilities": ("workspace.python.test.v1",),
            "max_risk": "R0",
        },
        "workspace_node_test": {
            "route_id": "workspace_node_test",
            "version": "1",
            "producer_ref": "workspace_node_test.v1",
            "capabilities": ("workspace.node.test.v1",),
            "max_risk": "R0",
        },
    }
)

_PLANNER_ONLY_ROUTE_SPECS: Mapping[RouteId, dict[str, object]] = MappingProxyType(
    {
        "workspace_project_search": {
            "route_id": "workspace_project_search",
            "producer_ref": "workspace_project_search.v1",
            "capabilities": ("workspace.project.search.v1",),
            "max_risk": "R0",
        },
        "workspace_project_batch_read": {
            "route_id": "workspace_project_batch_read",
            "producer_ref": "workspace_project_batch_read.v1",
            "capabilities": ("workspace.project.read_many.v1",),
            "max_risk": "R0",
        },
        "workspace_git_inspect": {
            "route_id": "workspace_git_inspect",
            "producer_ref": "workspace_git_inspect.v1",
            "capabilities": ("workspace.git.inspect.v1",),
            "max_risk": "R0",
        },
        "workspace_command_profile": {
            "route_id": "workspace_command_profile",
            "producer_ref": "workspace_command_profile.v1",
            "capabilities": ("workspace.command.run.v1",),
            "max_risk": "R0",
        },
        "workspace_coding_loop": {
            "route_id": "workspace_coding_loop",
            "producer_ref": "workspace_coding_loop.v1",
            "capabilities": (
                "workspace.file.read.v1",
                "workspace.patch.bundle.v1",
                "workspace.python.test.v1",
                "workspace.node.test.v1",
                "workspace.git.commit.v1",
            ),
            "max_risk": "R1",
        },
    }
)

_PARAMETERS: Mapping[RouteId, tuple[RouteParameterSpec, ...]] = MappingProxyType(
    {
        "research_to_html": (RouteParameterSpec("goal"),),
        "knowledge_lookup": (RouteParameterSpec("query"),),
        "mcp_text_metrics": (RouteParameterSpec("text"),),
        "workspace_file_read": (RouteParameterSpec("path"),),
        "workspace_file_replace": (
            RouteParameterSpec("path"),
            RouteParameterSpec("old_text"),
            RouteParameterSpec("new_text"),
        ),
        "workspace_patch_bundle": (RouteParameterSpec("changes_json"),),
        "workspace_agent_patch_test": (
            RouteParameterSpec("path"),
            RouteParameterSpec("project_path"),
            RouteParameterSpec("test_path"),
            RouteParameterSpec("test_kind", allowed_values=("python", "node")),
            RouteParameterSpec("objective"),
        ),
        "workspace_dynamic_patch_test": (
            RouteParameterSpec("path"),
            RouteParameterSpec("patch_path"),
            RouteParameterSpec("project_path"),
            RouteParameterSpec("test_path"),
            RouteParameterSpec("test_kind", allowed_values=("python", "node")),
            RouteParameterSpec("objective"),
        ),
        "workspace_file_create": (
            RouteParameterSpec("target_path"),
            RouteParameterSpec("content"),
        ),
        "workspace_file_rename": (
            RouteParameterSpec("source_path"),
            RouteParameterSpec("target_path"),
        ),
        "workspace_directory_list": (RouteParameterSpec("path"),),
        "workspace_directory_analyze": (
            RouteParameterSpec("path"),
            RouteParameterSpec("file_path", required=False),
            RouteParameterSpec("python_project_path", required=False),
            RouteParameterSpec("python_test_path", required=False),
            RouteParameterSpec("node_project_path", required=False),
            RouteParameterSpec("node_test_path", required=False),
        ),
        "workspace_snapshot_check": (
            RouteParameterSpec("profile", allowed_values=("python-syntax", "json-parse")),
            RouteParameterSpec("path"),
        ),
        "workspace_python_test": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("test_path"),
        ),
        "workspace_node_test": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("test_path"),
        ),
        "workspace_project_search": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("query"),
        ),
        "workspace_project_batch_read": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("paths_json"),
        ),
        "workspace_git_inspect": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("operation", allowed_values=("status", "diff", "log")),
        ),
        "workspace_command_profile": (
            RouteParameterSpec("project_path"),
            RouteParameterSpec("command_profile_id", allowed_values=COMMAND_PROFILE_IDS),
        ),
        "workspace_coding_loop": (
            RouteParameterSpec("primary_path"),
            RouteParameterSpec("secondary_path"),
            RouteParameterSpec("changes_json"),
            RouteParameterSpec("project_path"),
            RouteParameterSpec("test_path"),
            RouteParameterSpec("test_kind", allowed_values=("python", "node")),
        ),
    }
)


class RouteRecipeCatalog:
    """Resolve immutable recipes without treating a model selection as authority."""

    legacy_version: Literal["1"] = "1"
    planner_version: Literal["2"] = "2"

    @staticmethod
    def route_ids() -> tuple[RouteId, ...]:
        return tuple(_LEGACY_ROUTE_SPECS)

    @staticmethod
    def planner_route_ids() -> tuple[RouteId, ...]:
        return (*_LEGACY_ROUTE_SPECS, *_PLANNER_ONLY_ROUTE_SPECS)

    @staticmethod
    def planner_only_route_ids() -> tuple[RouteId, ...]:
        return tuple(_PLANNER_ONLY_ROUTE_SPECS)

    @staticmethod
    def is_planner_route(route_id: str) -> bool:
        return route_id in _LEGACY_ROUTE_SPECS or route_id in _PLANNER_ONLY_ROUTE_SPECS

    @staticmethod
    def is_planner_only_route(route_id: str) -> bool:
        """Return whether a Route may execute only through the generic Task Loop."""

        return route_id in _PLANNER_ONLY_ROUTE_SPECS

    @staticmethod
    def intent_description(draft: RouteOfferDraft) -> str:
        """Describe an opaque Offer while distinguishing server-fixed variants."""

        operational = next(
            (
                node
                for node in draft.draft.nodes
                if node.agent_selector is not None
                or node.capability_selector is not None
            ),
            draft.draft.nodes[0],
        )
        if draft.route_id == "workspace_coding_loop":
            test_kind = draft.fixed_parameters.get("test_kind")
            if test_kind not in {"python", "node"}:
                raise RouteRecipeError("Coding-loop Offer lacks its fixed test kind")
            return f"{operational.objective} Fixed test ecosystem: {test_kind}."
        if draft.route_id != "workspace_command_profile":
            return operational.objective
        profile_id = draft.fixed_parameters.get("command_profile_id")
        if profile_id is None:
            raise RouteRecipeError("Command Profile Offer lacks its server-fixed identity")
        return f"{operational.objective} Fixed Command Profile: {profile_id}."

    @staticmethod
    def parameter_specs(route_id: RouteId) -> tuple[RouteParameterSpec, ...]:
        return _PARAMETERS[route_id]

    @classmethod
    def offers_for(
        cls,
        *,
        task_id: str,
        capabilities: CapabilityCatalog,
        eligible_variant_keys: frozenset[str] | None = None,
    ) -> tuple[RouteOfferDraft, ...]:
        """Compile every currently available v2 recipe before any model call."""

        variants: list[tuple[RouteId, str, dict[str, str]]] = []
        for route_id in cls.planner_route_ids():
            if route_id == "workspace_command_profile":
                variants.extend(
                    (
                        route_id,
                        f"{route_id}:{profile_id}",
                        {"command_profile_id": profile_id},
                    )
                    for profile_id in COMMAND_PROFILE_IDS
                )
            elif route_id in {
                "workspace_agent_patch_test",
                "workspace_dynamic_patch_test",
                "workspace_coding_loop",
            }:
                variants.extend(
                    (
                        (route_id, f"{route_id}:python", {"test_kind": "python"}),
                        (route_id, f"{route_id}:node", {"test_kind": "node"}),
                    )
                )
            else:
                variants.append((route_id, route_id, {}))
        result: list[RouteOfferDraft] = []
        for route_id, variant_key, fixed_parameters in variants:
            if eligible_variant_keys is not None and variant_key not in eligible_variant_keys:
                continue
            contract, draft = cls.compile(
                task_id=task_id,
                route_id=route_id,
                parameters=fixed_parameters,
                capabilities=capabilities,
            )
            if not all(
                capabilities.resolve(item.capability_id, item.version, item.digest).runtime_enabled
                for item in contract.capabilities
            ):
                continue
            manifest = {
                **cls.manifest(route_id, cls.planner_version),
                "variant_key": variant_key,
                "fixed_parameters": fixed_parameters,
            }
            result.append(
                RouteOfferDraft(
                    variant_key=variant_key,
                    route_id=route_id,
                    route_version=cls.planner_version,
                    recipe_manifest=manifest,
                    recipe_digest=sha256_digest(manifest),
                    parameter_specs=tuple(
                        item
                        for item in cls.parameter_specs(route_id)
                        if item.name not in fixed_parameters
                    ),
                    fixed_parameters=dict(fixed_parameters),
                    contract=contract,
                    draft=draft,
                )
            )
        return tuple(result)

    @staticmethod
    def manifest(route_id: RouteId, version: RouteRecipeVersion) -> dict[str, object]:
        if version == "1":
            try:
                return dict(_LEGACY_ROUTE_SPECS[route_id])
            except KeyError as error:
                raise RouteRecipeError(
                    "Planner-only Route has no deterministic manifest"
                ) from error
        try:
            current = _LEGACY_ROUTE_SPECS.get(route_id) or _PLANNER_ONLY_ROUTE_SPECS[route_id]
        except KeyError as error:
            raise RouteRecipeError("Turn Route recipe is not registered") from error
        capabilities = tuple(cast(tuple[str, ...], current["capabilities"]))
        if route_id == "workspace_directory_analyze":
            # v1 predated fixed test nodes even though the trusted Contract later
            # bound them. v2 records the exact four-capability surface without
            # rewriting any historical route digest.
            capabilities = (
                "workspace.directory.read.v1",
                "workspace.file.read.v1",
                "workspace.python.test.v1",
                "workspace.node.test.v1",
            )
        return {
            "schema_version": "deskpilot.route-recipe.v2",
            "route_id": route_id,
            "version": "2",
            "trusted_template_ref": current["producer_ref"],
            "capabilities": capabilities,
            "max_risk": current["max_risk"],
            "parameter_binding": [
                item.manifest() for item in RouteRecipeCatalog.parameter_specs(route_id)
            ],
            "proposal_grants_authority": False,
        }

    @classmethod
    def digest(cls, route_id: RouteId, version: RouteRecipeVersion) -> str:
        return sha256_digest(cls.manifest(route_id, version))

    @classmethod
    def bind_parameters(
        cls,
        route_id: RouteId,
        message: str,
        proposed: Mapping[str, str],
        *,
        fixed_parameters: Mapping[str, str] | None = None,
    ) -> tuple[dict[str, str], dict[str, object], str]:
        fixed = dict(fixed_parameters or {})
        specs = cls.parameter_specs(route_id)
        by_name = {item.name: item for item in specs}
        if set(proposed) - set(by_name) or set(proposed).intersection(fixed):
            raise RouteRecipeError("Turn proposal contains an unknown parameter")
        missing = {item.name for item in specs if item.required} - set(proposed) - set(fixed)
        if missing:
            raise RouteRecipeError("Turn proposal omitted a required parameter")
        normalized: dict[str, str] = dict(fixed)
        evidence: list[dict[str, object]] = [
            {
                "name": name,
                "source": "server_offer",
                "value_digest": _text_digest(value),
            }
            for name, value in sorted(fixed.items())
        ]
        for name, raw_value in proposed.items():
            if not raw_value or len(raw_value) > 4_000 or raw_value not in message:
                raise RouteRecipeError(
                    "Turn proposal parameter is not an exact persisted-message substring"
                )
            value = _unquote(raw_value)
            spec = by_name[name]
            if spec.allowed_values:
                value = value.casefold()
                if value not in spec.allowed_values:
                    raise RouteRecipeError("Turn proposal parameter enum is not allowed")
            normalized[name] = value
            evidence.append(
                {
                    "name": name,
                    "source": "persisted_user_message",
                    "source_start": message.index(raw_value),
                    "source_length": len(raw_value),
                    "source_digest": _text_digest(raw_value),
                }
            )
        cls._validate_shape(route_id, normalized)
        if route_id == "workspace_dynamic_patch_test":
            normalized["patch_paths_json"] = canonical_json_bytes(
                {"paths": [normalized["patch_path"]]}
            ).decode("utf-8")
        binding: dict[str, object] = {
            "schema_version": "deskpilot.route-parameter-binding.v1",
            "route_id": route_id,
            "recipe_version": cls.planner_version,
            "message_digest": _text_digest(message),
            "parameters": normalized,
            "substring_evidence": evidence,
        }
        return normalized, binding, sha256_digest(binding)

    @classmethod
    def compile(
        cls,
        *,
        task_id: str,
        route_id: RouteId,
        parameters: Mapping[str, str],
        capabilities: CapabilityCatalog,
    ) -> tuple[TaskContract, DraftPlan]:
        params = dict(parameters)
        if route_id == "research_to_html":
            return (
                research_to_html_contract(task_id, capabilities, allow_user_path_export=True),
                research_to_html_draft(task_id),
            )
        if route_id == "knowledge_lookup":
            return knowledge_lookup_contract(task_id, capabilities), knowledge_lookup_draft(task_id)
        if route_id == "mcp_text_metrics":
            return mcp_text_metrics_contract(task_id, capabilities), mcp_text_metrics_draft(task_id)
        if route_id == "workspace_file_read":
            return workspace_file_read_contract(task_id, capabilities), workspace_file_read_draft(
                task_id
            )
        if route_id == "workspace_directory_list":
            return workspace_directory_list_contract(
                task_id, capabilities
            ), workspace_directory_list_draft(task_id)
        if route_id == "workspace_directory_analyze":
            return workspace_directory_analyze_contract(
                task_id, capabilities
            ), workspace_directory_analyze_draft(task_id)
        if route_id == "workspace_snapshot_check":
            return workspace_snapshot_check_contract(
                task_id, capabilities
            ), workspace_snapshot_check_draft(task_id)
        if route_id == "workspace_python_test":
            return workspace_python_test_contract(
                task_id, capabilities
            ), workspace_python_test_draft(task_id)
        if route_id == "workspace_node_test":
            return workspace_node_test_contract(task_id, capabilities), workspace_node_test_draft(
                task_id
            )
        if route_id == "workspace_project_search":
            return workspace_project_search_contract(
                task_id, capabilities
            ), workspace_project_search_draft(task_id)
        if route_id == "workspace_project_batch_read":
            return workspace_project_batch_read_contract(
                task_id, capabilities
            ), workspace_project_batch_read_draft(task_id)
        if route_id == "workspace_git_inspect":
            return workspace_git_inspect_contract(
                task_id, capabilities
            ), workspace_git_inspect_draft(task_id)
        if route_id == "workspace_command_profile":
            return workspace_command_profile_contract(
                task_id, capabilities
            ), workspace_command_profile_draft(task_id)
        if route_id == "workspace_coding_loop":
            test_kind = cast(Literal["python", "node"], params["test_kind"])
            return workspace_coding_loop_contract(
                task_id,
                capabilities,
                test_kind=test_kind,
            ), workspace_coding_loop_draft(task_id, test_kind=test_kind)
        if route_id == "workspace_file_replace":
            return workspace_file_replace_contract(
                task_id, capabilities
            ), workspace_file_replace_draft(task_id)
        if route_id == "workspace_file_create":
            return workspace_file_create_contract(
                task_id, capabilities
            ), workspace_file_create_draft(task_id)
        if route_id == "workspace_file_rename":
            return workspace_file_rename_contract(
                task_id, capabilities
            ), workspace_file_rename_draft(task_id)
        if route_id == "workspace_patch_bundle":
            return workspace_patch_bundle_contract(
                task_id, capabilities
            ), workspace_patch_bundle_draft(task_id)
        if route_id == "workspace_agent_patch_test":
            test_kind = cast(Literal["python", "node"], params["test_kind"])
            return workspace_agent_patch_test_contract(
                task_id, capabilities, test_kind=test_kind
            ), workspace_agent_patch_test_draft(task_id)
        if route_id == "workspace_dynamic_patch_test":
            test_kind = cast(Literal["python", "node"], params["test_kind"])
            return workspace_dynamic_patch_test_contract(
                task_id, capabilities, test_kind=test_kind
            ), workspace_dynamic_patch_test_draft(task_id)
        raise RouteRecipeError("Turn Route recipe is not registered")

    @classmethod
    def precompile(
        cls,
        *,
        task_id: str,
        route_id: RouteId,
        message: str,
        proposed: Mapping[str, str],
        capabilities: CapabilityCatalog,
        fixed_parameters: Mapping[str, str] | None = None,
    ) -> CompiledRouteRecipe:
        parameters, binding, binding_digest = cls.bind_parameters(
            route_id,
            message,
            proposed,
            fixed_parameters=fixed_parameters,
        )
        contract, draft = cls.compile(
            task_id=task_id,
            route_id=route_id,
            parameters=parameters,
            capabilities=capabilities,
        )
        manifest = cls.manifest(route_id, cls.planner_version)
        return CompiledRouteRecipe(
            route_id=route_id,
            route_version=cls.planner_version,
            recipe_manifest=manifest,
            recipe_digest=sha256_digest(manifest),
            parameter_binding_manifest=binding,
            parameter_binding_digest=binding_digest,
            parameters=parameters,
            contract=contract,
            draft=draft,
        )

    @staticmethod
    def _validate_shape(route_id: RouteId, parameters: Mapping[str, str]) -> None:
        if route_id == "workspace_directory_analyze":
            file_bound = "file_path" in parameters
            test_names = {
                "python_project_path",
                "python_test_path",
                "node_project_path",
                "node_test_path",
            }
            test_bound = test_names.issubset(parameters)
            if not file_bound and not test_bound:
                raise RouteRecipeError(
                    "Directory analysis requires a file or both fixed test bindings"
                )
            if set(parameters).intersection(test_names) and not test_bound:
                raise RouteRecipeError("Directory fixed test binding is incomplete")
        if route_id == "workspace_project_batch_read":
            try:
                paths = json.loads(parameters["paths_json"])
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise RouteRecipeError("Project batch-read paths are not valid JSON") from error
            if (
                not isinstance(paths, list)
                or not 1 <= len(paths) <= 32
                or any(not isinstance(item, str) or not item for item in paths)
            ):
                raise RouteRecipeError("Project batch-read paths are invalid")
        if route_id == "workspace_coding_loop":
            try:
                changes = json.loads(parameters["changes_json"])
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                raise RouteRecipeError("Coding-loop Patch changes are not valid JSON") from error
            if (
                not isinstance(changes, list)
                or len(changes) != 2
                or any(
                    not isinstance(item, dict)
                    or set(item) != {"path", "old_text", "new_text"}
                    or any(not isinstance(item[key], str) for key in item)
                    for item in changes
                )
            ):
                raise RouteRecipeError("Coding-loop Patch changes are invalid")
            paths = [str(item["path"]) for item in changes]
            if len(paths) != len(set(paths)):
                raise RouteRecipeError("Coding-loop Patch paths must be unique")
            primary_path = parameters["primary_path"]
            secondary_path = parameters["secondary_path"]
            project_path = parameters["project_path"]
            test_path = parameters["test_path"]
            bound_paths = {primary_path, secondary_path}
            if len(bound_paths) != 2 or set(paths) != bound_paths:
                raise RouteRecipeError(
                    "Coding-loop Patch paths must exactly match both Reader paths"
                )
            project = RouteRecipeCatalog._relative_path(project_path, allow_dot=True)
            for value in (*bound_paths, *paths):
                candidate = RouteRecipeCatalog._relative_path(value)
                if project.parts and candidate.parts[: len(project.parts)] != project.parts:
                    raise RouteRecipeError(
                        "Coding-loop Reader and Patch paths must stay inside the project"
                    )
            RouteRecipeCatalog._relative_path(test_path)

    @staticmethod
    def _relative_path(value: str, *, allow_dot: bool = False) -> PurePosixPath:
        normalized = value.replace("\\", "/")
        path = PurePosixPath(normalized)
        if (
            not normalized
            or path.is_absolute()
            or (bool(path.parts) and ":" in path.parts[0])
            or ".." in path.parts
            or (not allow_dot and not path.parts)
            or any(part in {"", "."} for part in path.parts)
        ):
            raise RouteRecipeError("Coding-loop path is not a safe relative path")
        return PurePosixPath() if allow_dot and normalized == "." else path


def _unquote(value: str) -> str:
    stripped = value.strip()
    if len(stripped) >= 2 and stripped[0] + stripped[-1] in {'""', "“”"}:
        return stripped[1:-1]
    return stripped


def _text_digest(value: str) -> str:
    return sha256_digest({"text": value})
