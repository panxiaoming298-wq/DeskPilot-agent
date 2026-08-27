"""Server-only input bindings for the first generic read-only capability set.

The catalog accepts persisted ``ModelPlannerStepBinding`` proofs rather than a
planner-authored argument dictionary.  Verified predecessor results are
checked as dependency gates; none of the first five direct capabilities
semantically consumes those payloads.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.route_recipe_catalog import RouteId, RouteRecipeCatalog
from deskpilot.application.workspace_coding_graph import (
    WORKSPACE_CODING_MIN_FILES,
    workspace_coding_file_count,
    workspace_coding_fixed_parameters,
    workspace_coding_variant_key,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.capability_execution import (
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.command_profiles import COMMAND_PROFILE_IDS, CommandProfileId
from deskpilot.domain.task_loop_execution import (
    MODEL_PLANNER_NODE_BINDING_ID_PATTERN,
    ModelPlannerNodeBinding,
)
from deskpilot.domain.task_plans import (
    PLAN_NODE_ID_PATTERN,
    TASK_ID_PATTERN,
    CapabilityRef,
)
from deskpilot.domain.workspace_coding_changes import WorkspaceCodingWriteNodeProof
from deskpilot.domain.workspace_command_plans import WorkspaceCommandPlanStepProof


class CapabilityInputBindingError(RuntimeError):
    code = "CAPABILITY_INPUT_BINDING_REJECTED"


class CapabilityInputProfileNotFoundError(CapabilityInputBindingError):
    code = "CAPABILITY_INPUT_PROFILE_NOT_FOUND"


def canonicalize_capability_parameter(
    raw_value: str,
    *,
    enum_value: bool = False,
) -> str:
    """Canonicalize one trusted recipe value for persistence and execution.

    The node binder and the execution-time catalog intentionally share this
    function so a quoted substring or case-insensitive enum cannot acquire two
    different digests at activation and dispatch.
    """

    value = raw_value.strip()
    if len(value) >= 2 and value[0] + value[-1] in {'""', "“”"}:
        value = value[1:-1]
    if enum_value:
        value = value.casefold()
    if not value:
        raise CapabilityInputLineageRejectedError(
            "Capability input contains an empty persisted parameter"
        )
    return value


class CapabilityInputLineageRejectedError(CapabilityInputBindingError):
    code = "CAPABILITY_INPUT_LINEAGE_REJECTED"


class CapabilityInputDependencyRejectedError(CapabilityInputBindingError):
    code = "CAPABILITY_INPUT_DEPENDENCY_REJECTED"


class KnowledgeLocalExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.knowledge-local-executor-input.v1"] = (
        "deskpilot.knowledge-local-executor-input.v1"
    )
    query: str = Field(min_length=1, max_length=500)
    limit: Literal[10] = 10


class McpTextMetricsExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.mcp-text-metrics-executor-input.v1"] = (
        "deskpilot.mcp-text-metrics-executor-input.v1"
    )
    text: str = Field(min_length=1, max_length=4_000)


class WorkspaceSnapshotCheckExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-check-executor-input.v1"] = (
        "deskpilot.workspace-check-executor-input.v1"
    )
    profile: Literal["python-syntax", "json-parse"]
    path: str = Field(min_length=1, max_length=32_767)


class WorkspacePythonTestExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-python-test-executor-input.v1"] = (
        "deskpilot.workspace-python-test-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)


class WorkspaceNodeTestExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-node-test-executor-input.v1"] = (
        "deskpilot.workspace-node-test-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    test_path: str = Field(min_length=1, max_length=32_767)


class WorkspaceProjectSearchExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-project-search-executor-input.v1"] = (
        "deskpilot.workspace-project-search-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    query: str = Field(min_length=1, max_length=256)


class WorkspaceProjectBatchReadExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-project-batch-read-executor-input.v1"] = (
        "deskpilot.workspace-project-batch-read-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    paths: tuple[str, ...] = Field(min_length=1, max_length=32)


class WorkspaceGitInspectExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-git-inspect-executor-input.v1"] = (
        "deskpilot.workspace-git-inspect-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    operation: Literal["status", "diff", "log"]


class WorkspaceGitCommitExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-git-commit-executor-input.v1"] = (
        "deskpilot.workspace-git-commit-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    paths: tuple[str, ...] = Field(min_length=2, max_length=8)


class WorkspaceCommandExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-command-executor-input.v1"] = (
        "deskpilot.workspace-command-executor-input.v1"
    )
    project_path: str = Field(min_length=1, max_length=32_767)
    command_profile_id: CommandProfileId


class WorkspacePatchChangeExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str = Field(min_length=1, max_length=32_767)
    old_text: str = Field(min_length=1, max_length=4_096)
    new_text: str = Field(max_length=4_096)


class WorkspacePatchBundleExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.workspace-patch-bundle-executor-input.v1"] = (
        "deskpilot.workspace-patch-bundle-executor-input.v1"
    )
    changes: tuple[WorkspacePatchChangeExecutorInput, ...] = Field(
        min_length=2,
        max_length=8,
    )


class ArtifactHtmlExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.artifact-html-executor-input.v1"] = (
        "deskpilot.artifact-html-executor-input.v1"
    )
    verified_claims_digest: str = Field(pattern=DIGEST_PATTERN)


class BrowserVerifyExecutorInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-verify-executor-input.v1"] = (
        "deskpilot.browser-verify-executor-input.v1"
    )
    artifact_digest: str = Field(pattern=DIGEST_PATTERN)


CapabilityExecutorInput = Annotated[
    KnowledgeLocalExecutorInput
    | McpTextMetricsExecutorInput
    | WorkspaceSnapshotCheckExecutorInput
    | WorkspacePythonTestExecutorInput
    | WorkspaceNodeTestExecutorInput
    | WorkspaceProjectSearchExecutorInput
    | WorkspaceProjectBatchReadExecutorInput
    | WorkspaceGitInspectExecutorInput
    | WorkspaceGitCommitExecutorInput
    | WorkspaceCommandExecutorInput
    | WorkspacePatchBundleExecutorInput
    | ArtifactHtmlExecutorInput
    | BrowserVerifyExecutorInput,
    Field(discriminator="schema_version"),
]


@dataclass(frozen=True, slots=True)
class ResolvedVerifiedCapabilityResult:
    """One persisted verified ResultRef paired with its server-resolved value."""

    result_ref: VerifiedCapabilityResultRef
    output_manifest: Mapping[str, Any]
    output_schema_digest: str

    def __post_init__(self) -> None:
        if self.output_manifest.get("result_digest") != self.result_ref.result_digest:
            raise CapabilityInputDependencyRejectedError(
                "Resolved result does not match its verified ResultRef"
            )
        if self.output_schema_digest != self.result_ref.result_schema_digest:
            raise CapabilityInputDependencyRejectedError(
                "Resolved result Schema does not match its verified ResultRef"
            )

    @classmethod
    def from_model(
        cls,
        *,
        result_ref: VerifiedCapabilityResultRef,
        value: BaseModel,
    ) -> ResolvedVerifiedCapabilityResult:
        return cls(
            result_ref=result_ref,
            output_manifest=value.model_dump(mode="json"),
            output_schema_digest=sha256_digest(type(value).model_json_schema()),
        )


class BoundCapabilityInput(BaseModel):
    """Content-addressed executor arguments and their least-authority lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.bound-capability-input.v1"] = (
        "deskpilot.bound-capability-input.v1"
    )
    task_id: str = Field(pattern=TASK_ID_PATTERN)
    node_id: str = Field(pattern=PLAN_NODE_ID_PATTERN)
    node_spec_digest: str = Field(pattern=DIGEST_PATTERN)
    node_binding_id: str = Field(pattern=MODEL_PLANNER_NODE_BINDING_ID_PATTERN)
    node_binding_digest: str = Field(pattern=DIGEST_PATTERN)
    effective_authority_digest: str = Field(pattern=DIGEST_PATTERN)
    runtime_eligibility_digest: str = Field(pattern=DIGEST_PATTERN)
    capability: CapabilityRef
    source_step_binding_id: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    source_step_binding_digest: str | None = Field(
        default=None,
        pattern=DIGEST_PATTERN,
        exclude_if=lambda value: value is None,
    )
    source_offer_key: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    route_id: str
    route_version: Literal["2"] = "2"
    route_manifest_digest: str = Field(pattern=DIGEST_PATTERN)
    parameter_bindings_digest: str = Field(pattern=DIGEST_PATTERN)
    arguments: CapabilityExecutorInput
    arguments_digest: str = Field(pattern=DIGEST_PATTERN)
    dependency_result_refs: tuple[VerifiedCapabilityResultRef, ...] = Field(
        default=(), max_length=20
    )
    consumed_result_refs: tuple[VerifiedCapabilityResultRef, ...] = Field(default=(), max_length=16)
    workspace_command_plan_step: WorkspaceCommandPlanStepProof | None = None
    workspace_coding_write_node_proof: WorkspaceCodingWriteNodeProof | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
    )
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def identity_and_digest_match(self) -> BoundCapabilityInput:
        expected_schema = _INPUT_SCHEMA_BY_CAPABILITY.get(self.capability.capability_id)
        if expected_schema is None or self.arguments.schema_version != expected_schema:
            raise ValueError("Bound capability input model does not match its capability")
        if self.arguments_digest != sha256_digest(self.arguments):
            raise ValueError("Bound capability argument digest does not match")
        dependency_digests = tuple(item.result_ref_digest for item in self.dependency_result_refs)
        consumed_digests = tuple(item.result_ref_digest for item in self.consumed_result_refs)
        if len(dependency_digests) != len(set(dependency_digests)):
            raise ValueError("Bound capability dependencies must be unique")
        if len(consumed_digests) != len(set(consumed_digests)):
            raise ValueError("Bound capability semantic inputs must be unique")
        if not set(consumed_digests).issubset(dependency_digests):
            raise ValueError("Consumed ResultRefs must be verified dependencies")
        if any(item.task_id != self.task_id for item in self.dependency_result_refs):
            raise ValueError("Bound capability dependencies cross Task scope")
        proof = self.workspace_command_plan_step
        write_proof = self.workspace_coding_write_node_proof
        if write_proof is None:
            if (
                self.source_step_binding_id is None
                or self.source_step_binding_digest is None
                or self.source_offer_key is None
            ):
                raise ValueError("Bound capability input lost its source Offer step")
        elif (
            self.route_id != "workspace_coding_loop"
            or self.source_step_binding_id is not None
            or self.source_step_binding_digest is not None
            or self.source_offer_key is not None
            or write_proof.successor_task_id != self.task_id
            or write_proof.plan_node_id != self.node_id
            or write_proof.plan_node_spec_digest != self.node_spec_digest
            or write_proof.capability != self.capability
        ):
            raise ValueError("Confirmed write proof changed from its capability input")
        if self.route_id == "workspace_command_profile":
            if not isinstance(self.arguments, WorkspaceCommandExecutorInput):
                raise ValueError("Command capability input changed its argument Schema")
            if proof is not None and (
                proof.composite_node_id != self.node_id
                or proof.composite_node_spec_digest != self.node_spec_digest
                or proof.step_binding_id != self.source_step_binding_id
                or proof.step_binding_digest != self.source_step_binding_digest
                or proof.offer_key != self.source_offer_key
                or proof.project_path != self.arguments.project_path
                or proof.command_profile_id != self.arguments.command_profile_id
            ):
                raise ValueError("Command Plan proof changed from its bound capability input")
        elif proof is not None:
            raise ValueError("Non-command capability input contains a command Plan proof")
        material = self.model_dump(mode="json", exclude={"binding_digest"})
        if proof is None:
            material.pop("workspace_command_plan_step", None)
        if write_proof is None:
            material.pop("workspace_coding_write_node_proof", None)
        if self.binding_digest != sha256_digest(material):
            raise ValueError("Bound capability input digest does not match")
        return self

    @classmethod
    def build(
        cls,
        *,
        node_binding: ModelPlannerNodeBinding,
        capability: CapabilityRef,
        arguments: CapabilityExecutorInput,
        dependency_result_refs: tuple[VerifiedCapabilityResultRef, ...],
        consumed_result_refs: tuple[VerifiedCapabilityResultRef, ...] = (),
        workspace_command_plan_step: WorkspaceCommandPlanStepProof | None = None,
    ) -> BoundCapabilityInput:
        recipe = node_binding.recipe
        if recipe is None:
            raise ValueError("Capability input has no model-planner source recipe")
        values: dict[str, object] = {
            "schema_version": "deskpilot.bound-capability-input.v1",
            "task_id": node_binding.task_id,
            "node_id": node_binding.composite_node_id,
            "node_spec_digest": node_binding.composite_node_spec_digest,
            "node_binding_id": node_binding.node_binding_id,
            "node_binding_digest": node_binding.binding_digest,
            "effective_authority_digest": (node_binding.effective_authority.authority_digest),
            "runtime_eligibility_digest": (node_binding.runtime_eligibility.eligibility_digest),
            "capability": capability,
            "route_id": recipe.route_id,
            "route_version": "2",
            "route_manifest_digest": recipe.route_manifest_digest,
            "parameter_bindings_digest": node_binding.parameter_bindings_digest,
            "arguments": arguments,
            "arguments_digest": sha256_digest(arguments),
            "dependency_result_refs": dependency_result_refs,
            "consumed_result_refs": consumed_result_refs,
        }
        if node_binding.step_binding_id is not None:
            if node_binding.step_binding_digest is None or node_binding.offer_key is None:
                raise ValueError("Capability source Offer step proof is incomplete")
            values.update(
                {
                    "source_step_binding_id": node_binding.step_binding_id,
                    "source_step_binding_digest": node_binding.step_binding_digest,
                    "source_offer_key": node_binding.offer_key,
                }
            )
        if workspace_command_plan_step is not None:
            values["workspace_command_plan_step"] = workspace_command_plan_step
        if node_binding.workspace_coding_write_node_proof is not None:
            values["workspace_coding_write_node_proof"] = (
                node_binding.workspace_coding_write_node_proof
            )
        return cls.model_validate({**values, "binding_digest": sha256_digest(values)})


_INPUT_SCHEMA_BY_CAPABILITY = {
    "knowledge.local.v1": "deskpilot.knowledge-local-executor-input.v1",
    "mcp.text.metrics.v1": "deskpilot.mcp-text-metrics-executor-input.v1",
    "workspace.snapshot.check.v1": "deskpilot.workspace-check-executor-input.v1",
    "workspace.python.test.v1": "deskpilot.workspace-python-test-executor-input.v1",
    "workspace.node.test.v1": "deskpilot.workspace-node-test-executor-input.v1",
    "workspace.project.search.v1": "deskpilot.workspace-project-search-executor-input.v1",
    "workspace.project.read_many.v1": ("deskpilot.workspace-project-batch-read-executor-input.v1"),
    "workspace.git.inspect.v1": "deskpilot.workspace-git-inspect-executor-input.v1",
    "workspace.git.commit.v1": "deskpilot.workspace-git-commit-executor-input.v1",
    "workspace.command.run.v1": "deskpilot.workspace-command-executor-input.v1",
    "workspace.patch.bundle.v1": "deskpilot.workspace-patch-bundle-executor-input.v1",
    "artifact.html.v1": "deskpilot.artifact-html-executor-input.v1",
    "browser.verify.v1": "deskpilot.browser-verify-executor-input.v1",
}


@dataclass(frozen=True, slots=True)
class _InputProfile:
    capability: CapabilityRef
    route_id: RouteId
    route_manifest_digest: str
    source_local_key: str
    parameter_names: tuple[str, ...]
    input_model: type[BaseModel]
    enum_parameters: frozenset[str] = frozenset()
    consumes: tuple[CapabilityResultKind, ...] = ()
    fixed_parameter_names: frozenset[str] = frozenset()


class CapabilityInputBindingCatalog:
    """Reconstruct exact arguments from persisted stage-111 substring proofs."""

    def __init__(self, capabilities: CapabilityCatalog) -> None:
        definitions: tuple[
            tuple[
                RouteId,
                str,
                str,
                tuple[str, ...],
                type[BaseModel],
                frozenset[str],
                tuple[CapabilityResultKind, ...],
                frozenset[str],
            ],
            ...,
        ] = (
            (
                "knowledge_lookup",
                "knowledge.local.v1",
                "knowledge_lookup",
                ("query",),
                KnowledgeLocalExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "mcp_text_metrics",
                "mcp.text.metrics.v1",
                "mcp_text_metrics",
                ("text",),
                McpTextMetricsExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_snapshot_check",
                "workspace.snapshot.check.v1",
                "workspace_snapshot_check",
                ("profile", "path"),
                WorkspaceSnapshotCheckExecutorInput,
                frozenset({"profile"}),
                (),
                frozenset(),
            ),
            (
                "workspace_python_test",
                "workspace.python.test.v1",
                "workspace_python_test",
                ("project_path", "test_path"),
                WorkspacePythonTestExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_node_test",
                "workspace.node.test.v1",
                "workspace_node_test",
                ("project_path", "test_path"),
                WorkspaceNodeTestExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_project_search",
                "workspace.project.search.v1",
                "workspace_project_search",
                ("project_path", "query"),
                WorkspaceProjectSearchExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_project_batch_read",
                "workspace.project.read_many.v1",
                "workspace_project_batch_read",
                ("project_path", "paths_json"),
                WorkspaceProjectBatchReadExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_git_inspect",
                "workspace.git.inspect.v1",
                "workspace_git_inspect",
                ("project_path", "operation"),
                WorkspaceGitInspectExecutorInput,
                frozenset({"operation"}),
                (),
                frozenset(),
            ),
            (
                "workspace_command_profile",
                "workspace.command.run.v1",
                "workspace_command_profile",
                ("project_path",),
                WorkspaceCommandExecutorInput,
                frozenset(),
                (),
                frozenset({"command_profile_id"}),
            ),
            (
                "workspace_patch_bundle",
                "workspace.patch.bundle.v1",
                "workspace_patch_bundle",
                ("changes_json",),
                WorkspacePatchBundleExecutorInput,
                frozenset(),
                (),
                frozenset(),
            ),
            (
                "workspace_coding_loop",
                "workspace.patch.bundle.v1",
                "apply_patch",
                ("changes_json",),
                WorkspacePatchBundleExecutorInput,
                frozenset(),
                (),
                frozenset({"test_kind"}),
            ),
            (
                "workspace_coding_loop",
                "workspace.python.test.v1",
                "run_fixed_test",
                ("project_path", "test_path"),
                WorkspacePythonTestExecutorInput,
                frozenset(),
                (),
                frozenset({"test_kind"}),
            ),
            (
                "workspace_coding_loop",
                "workspace.node.test.v1",
                "run_fixed_test",
                ("project_path", "test_path"),
                WorkspaceNodeTestExecutorInput,
                frozenset(),
                (),
                frozenset({"test_kind"}),
            ),
            (
                "workspace_coding_loop",
                "workspace.git.commit.v1",
                "commit_git",
                ("project_path", "changes_json"),
                WorkspaceGitCommitExecutorInput,
                frozenset(),
                (),
                frozenset({"test_kind"}),
            ),
            (
                "research_to_html",
                "artifact.html.v1",
                "build_html",
                (),
                ArtifactHtmlExecutorInput,
                frozenset(),
                (CapabilityResultKind.VERIFIED_CLAIMS,),
                frozenset(),
            ),
            (
                "research_to_html",
                "browser.verify.v1",
                "browser_verify",
                (),
                BrowserVerifyExecutorInput,
                frozenset(),
                (CapabilityResultKind.ARTIFACT,),
                frozenset(),
            ),
        )
        profiles: dict[tuple[str, str, str, str, str], _InputProfile] = {}
        for (
            route_id,
            capability_id,
            local_key,
            names,
            model,
            enum_names,
            consumes,
            fixed_parameter_names,
        ) in definitions:
            pack = capabilities.resolve_preferred(capability_id)
            capability = CapabilityRef(
                capability_id=pack.capability_id,
                version=pack.version,
                digest=pack.digest,
            )
            fixed_parameters: dict[str, str] = {}
            variant_key: str = route_id
            if route_id == "workspace_coding_loop":
                test_kind = (
                    "python"
                    if capability_id == "workspace.python.test.v1"
                    else "node"
                    if capability_id == "workspace.node.test.v1"
                    else None
                )
                # The Patch capability is present in both fixed variants; its
                # exact recipe digest is checked dynamically from the binding.
                if test_kind is not None:
                    fixed_parameters = {"test_kind": test_kind}
                    variant_key = f"{route_id}:{test_kind}"
            profiles[
                (
                    pack.capability_id,
                    pack.version,
                    pack.digest,
                    route_id,
                    local_key,
                )
            ] = _InputProfile(
                capability=capability,
                route_id=route_id,
                route_manifest_digest=sha256_digest(
                    {
                        **RouteRecipeCatalog.manifest(route_id, "2"),
                        "variant_key": variant_key,
                        "fixed_parameters": fixed_parameters,
                    }
                ),
                source_local_key=local_key,
                parameter_names=names,
                input_model=model,
                enum_parameters=enum_names,
                consumes=consumes,
                fixed_parameter_names=fixed_parameter_names,
            )
        self._profiles = profiles

    def bind_node(
        self,
        *,
        node_binding: ModelPlannerNodeBinding,
        dependencies: tuple[ResolvedVerifiedCapabilityResult, ...] = (),
        workspace_command_plan_step: WorkspaceCommandPlanStepProof | None = None,
    ) -> BoundCapabilityInput:
        authority = node_binding.effective_authority
        eligibility = node_binding.runtime_eligibility
        capability = authority.capability
        recipe = node_binding.recipe
        if (
            authority.node_kind.value != "capability"
            or capability is None
            or recipe is None
            or eligibility.runtime_kind != "capability_executor"
            or eligibility.capability != capability
            or not eligibility.runtime_enabled
        ):
            raise CapabilityInputLineageRejectedError(
                "Capability input requires exact eligible capability-node authority"
            )
        profile = self._profiles.get(
            (
                capability.capability_id,
                capability.version,
                capability.digest,
                recipe.route_id,
                node_binding.mapping.source_local_key,
            )
        )
        if profile is None:
            raise CapabilityInputProfileNotFoundError(
                "Capability input profile has no exact registration"
            )
        if profile.route_id == "workspace_command_profile":
            if workspace_command_plan_step is None:
                raise CapabilityInputLineageRejectedError(
                    "Command Profile input has no persisted command Plan proof"
                )
        elif workspace_command_plan_step is not None:
            raise CapabilityInputLineageRejectedError(
                "Non-command capability input contains a command Plan proof"
            )
        expected_recipe_digest = profile.route_manifest_digest
        if profile.route_id == "workspace_command_profile":
            profile_id = node_binding.bound_input_manifest.get("command_profile_id")
            if profile_id not in COMMAND_PROFILE_IDS:
                raise CapabilityInputLineageRejectedError(
                    "Command Profile input has no registered fixed variant"
                )
            expected_recipe_digest = sha256_digest(
                {
                    **RouteRecipeCatalog.manifest(profile.route_id, "2"),
                    "variant_key": f"{profile.route_id}:{profile_id}",
                    "fixed_parameters": {"command_profile_id": profile_id},
                }
            )
        elif profile.route_id == "workspace_coding_loop":
            test_kind = node_binding.bound_input_manifest.get("test_kind")
            if test_kind not in {"python", "node"}:
                raise CapabilityInputLineageRejectedError("Coding-loop fixed test kind is invalid")
            try:
                file_count = workspace_coding_file_count(node_binding.bound_input_manifest)
            except ValueError as error:
                raise CapabilityInputLineageRejectedError(
                    "Coding-loop fixed file count is invalid"
                ) from error
            fixed = workspace_coding_fixed_parameters(test_kind, file_count)
            expected_recipe_digest = sha256_digest(
                RouteRecipeCatalog.variant_manifest(
                    profile.route_id,
                    workspace_coding_variant_key(test_kind, file_count),
                    fixed,
                )
            )
        if (
            recipe.route_id != profile.route_id
            or recipe.route_version != "2"
            or recipe.route_manifest_digest != expected_recipe_digest
        ):
            raise CapabilityInputLineageRejectedError("Capability input source recipe changed")
        if (
            node_binding.mapping.source_local_key != profile.source_local_key
            or node_binding.mapping.composite_node_id != node_binding.composite_node_id
            or node_binding.mapping.composite_node_spec_digest
            != node_binding.composite_node_spec_digest
        ):
            raise CapabilityInputLineageRejectedError(
                "Capability input source node mapping changed"
            )
        if not all(isinstance(item, ResolvedVerifiedCapabilityResult) for item in dependencies):
            raise CapabilityInputDependencyRejectedError(
                "Capability dependencies must be resolved verified ResultRefs"
            )
        dependency_refs = tuple(item.result_ref for item in dependencies)
        if any(item.task_id != node_binding.task_id for item in dependency_refs):
            raise CapabilityInputDependencyRejectedError("Capability dependency crosses Task scope")
        consumed = self._consumed_dependencies(profile, dependencies)
        parameters = self._parameters(node_binding, profile, consumed)
        try:
            arguments = profile.input_model.model_validate(parameters)
        except ValueError as error:
            raise CapabilityInputLineageRejectedError(
                "Capability input parameters failed the trusted Schema"
            ) from error
        return BoundCapabilityInput.build(
            node_binding=node_binding,
            capability=profile.capability,
            arguments=cast(CapabilityExecutorInput, arguments),
            dependency_result_refs=dependency_refs,
            consumed_result_refs=tuple(item.result_ref for item in consumed),
            workspace_command_plan_step=workspace_command_plan_step,
        )

    @staticmethod
    def _consumed_dependencies(
        profile: _InputProfile,
        dependencies: tuple[ResolvedVerifiedCapabilityResult, ...],
    ) -> tuple[ResolvedVerifiedCapabilityResult, ...]:
        selected: list[ResolvedVerifiedCapabilityResult] = []
        for result_kind in profile.consumes:
            matches = tuple(
                item for item in dependencies if item.result_ref.result_kind is result_kind
            )
            if len(matches) != 1:
                raise CapabilityInputDependencyRejectedError(
                    "Capability semantic input has no unique verified ResultRef"
                )
            selected.append(matches[0])
        return tuple(selected)

    @staticmethod
    def _parameters(
        node_binding: ModelPlannerNodeBinding,
        profile: _InputProfile,
        consumed: tuple[ResolvedVerifiedCapabilityResult, ...],
    ) -> dict[str, object]:
        if profile.consumes:
            if profile.parameter_names or len(consumed) != 1:
                raise CapabilityInputLineageRejectedError(
                    "Derived capability input profile is invalid"
                )
            digest = consumed[0].result_ref.result_digest
            if profile.capability.capability_id == "artifact.html.v1":
                return {"verified_claims_digest": digest}
            if profile.capability.capability_id == "browser.verify.v1":
                return {"artifact_digest": digest}
            raise CapabilityInputProfileNotFoundError(
                "Derived capability input has no server binding"
            )
        write_proof = node_binding.workspace_coding_write_node_proof
        by_name = {item.parameter_name: item for item in node_binding.parameter_bindings}
        if len(by_name) != len(node_binding.parameter_bindings):
            raise CapabilityInputLineageRejectedError(
                "Capability input repeats a persisted parameter proof"
            )
        all_normalized = (
            dict(write_proof.parameters)
            if write_proof is not None
            else {
                name: canonicalize_capability_parameter(
                    item.value,
                    enum_value=name in profile.enum_parameters,
                )
                for name, item in by_name.items()
            }
        )
        if profile.route_id not in {"workspace_command_profile", "workspace_coding_loop"} and set(
            all_normalized
        ) != set(profile.parameter_names):
            raise CapabilityInputLineageRejectedError(
                "Capability input contains parameters outside its trusted recipe"
            )
        expected_bound = dict(all_normalized)
        for name in profile.fixed_parameter_names:
            fixed_value = node_binding.bound_input_manifest.get(name)
            if not isinstance(fixed_value, str) or not fixed_value:
                raise CapabilityInputLineageRejectedError("Capability fixed parameter is missing")
            expected_bound[name] = fixed_value
        if (
            profile.route_id == "workspace_coding_loop"
            and "file_count" in node_binding.bound_input_manifest
        ):
            try:
                file_count = workspace_coding_file_count(node_binding.bound_input_manifest)
            except ValueError as error:
                raise CapabilityInputLineageRejectedError(
                    "Coding-loop fixed file count is invalid"
                ) from error
            if file_count == WORKSPACE_CODING_MIN_FILES:
                raise CapabilityInputLineageRejectedError(
                    "Legacy coding-loop input unexpectedly contains a file count"
                )
            expected_bound["file_count"] = str(file_count)
        if expected_bound != node_binding.bound_input_manifest:
            raise CapabilityInputLineageRejectedError(
                "Capability normalized input changed from its persisted node binding"
            )
        if not set(profile.parameter_names).issubset(all_normalized):
            raise CapabilityInputLineageRejectedError(
                "Capability node parameters do not match the trusted recipe"
            )
        normalized = {name: all_normalized[name] for name in profile.parameter_names}
        result: dict[str, object] = dict(normalized)
        if profile.route_id == "knowledge_lookup":
            result["limit"] = 10
        elif profile.route_id in {"workspace_patch_bundle", "workspace_coding_loop"} and (
            profile.capability.capability_id == "workspace.patch.bundle.v1"
        ):
            try:
                decoded = json.loads(normalized["changes_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise CapabilityInputLineageRejectedError(
                    "Workspace patch changes are not valid JSON"
                ) from error
            if not isinstance(decoded, list):
                raise CapabilityInputLineageRejectedError(
                    "Workspace patch changes must be one JSON list"
                )
            result = {"changes": decoded}
        elif profile.route_id == "workspace_coding_loop" and (
            profile.capability.capability_id == "workspace.git.commit.v1"
        ):
            try:
                decoded = json.loads(normalized["changes_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise CapabilityInputLineageRejectedError(
                    "Git commit changes are not valid JSON"
                ) from error
            if not isinstance(decoded, list) or any(
                not isinstance(item, dict) or not isinstance(item.get("path"), str)
                for item in decoded
            ):
                raise CapabilityInputLineageRejectedError(
                    "Git commit changes must contain exact paths"
                )
            result = {
                "project_path": normalized["project_path"],
                "paths": [item["path"] for item in decoded],
            }
        elif profile.route_id == "workspace_project_batch_read":
            try:
                decoded = json.loads(normalized["paths_json"])
            except (TypeError, json.JSONDecodeError) as error:
                raise CapabilityInputLineageRejectedError(
                    "Project batch-read paths are not valid JSON"
                ) from error
            if not isinstance(decoded, list):
                raise CapabilityInputLineageRejectedError(
                    "Project batch-read paths must be one JSON list"
                )
            result = {
                "project_path": normalized["project_path"],
                "paths": decoded,
            }
        elif profile.route_id == "workspace_command_profile":
            profile_id = node_binding.bound_input_manifest.get("command_profile_id")
            if profile_id not in COMMAND_PROFILE_IDS:
                raise CapabilityInputLineageRejectedError(
                    "Command Profile fixed input is not registered"
                )
            expected: dict[str, object] = {
                "project_path": normalized["project_path"],
                "command_profile_id": profile_id,
            }
            if expected != node_binding.bound_input_manifest:
                raise CapabilityInputLineageRejectedError(
                    "Command Profile fixed input changed from its persisted Offer"
                )
            result = expected
        return result

    def capabilities(self) -> tuple[CapabilityRef, ...]:
        return tuple(self._profiles[key].capability for key in sorted(self._profiles))


__all__ = [
    "ArtifactHtmlExecutorInput",
    "BoundCapabilityInput",
    "BrowserVerifyExecutorInput",
    "CapabilityExecutorInput",
    "CapabilityInputBindingCatalog",
    "CapabilityInputBindingError",
    "CapabilityInputDependencyRejectedError",
    "CapabilityInputLineageRejectedError",
    "CapabilityInputProfileNotFoundError",
    "KnowledgeLocalExecutorInput",
    "McpTextMetricsExecutorInput",
    "ResolvedVerifiedCapabilityResult",
    "WorkspaceNodeTestExecutorInput",
    "WorkspaceCommandExecutorInput",
    "WorkspaceGitInspectExecutorInput",
    "WorkspaceGitCommitExecutorInput",
    "WorkspacePatchBundleExecutorInput",
    "WorkspacePatchChangeExecutorInput",
    "WorkspacePythonTestExecutorInput",
    "WorkspaceProjectBatchReadExecutorInput",
    "WorkspaceProjectSearchExecutorInput",
    "WorkspaceSnapshotCheckExecutorInput",
    "canonicalize_capability_parameter",
]
