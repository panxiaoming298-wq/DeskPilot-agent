from datetime import UTC, datetime
from typing import Any

import pytest

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.capability_input_binding_catalog import (
    ArtifactHtmlExecutorInput,
    BrowserVerifyExecutorInput,
    CapabilityInputBindingCatalog,
    CapabilityInputDependencyRejectedError,
    CapabilityInputLineageRejectedError,
    KnowledgeLocalExecutorInput,
    McpTextMetricsExecutorInput,
    ResolvedVerifiedCapabilityResult,
    WorkspaceCommandExecutorInput,
    WorkspaceGitInspectExecutorInput,
    WorkspaceNodeTestExecutorInput,
    WorkspaceProjectBatchReadExecutorInput,
    WorkspaceProjectSearchExecutorInput,
    WorkspacePythonTestExecutorInput,
    WorkspaceSnapshotCheckExecutorInput,
)
from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.route_recipe_catalog import RouteId, RouteRecipeCatalog
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.capability_execution import (
    CapabilityResultKind,
    VerifiedCapabilityResultRef,
)
from deskpilot.domain.knowledge import KnowledgeSearchRead
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
)
from deskpilot.domain.task_loop import (
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
    TaskLoopSourceRef,
)
from deskpilot.domain.task_loop_execution import (
    EffectiveNodeAuthority,
    ModelPlannerNodeBinding,
    RuntimeEligibilityProof,
)
from deskpilot.domain.task_plans import CapabilityRef, DraftNodeKind
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import (
    TurnPlanningOfferRef,
    TurnPlanningParameterBinding,
    TurnPlanningRecipeRef,
)
from deskpilot.domain.workspace_command_plans import (
    WorkspaceCommandPlan,
    WorkspaceCommandPlanBinding,
    WorkspaceCommandPlanNodeMapping,
    WorkspaceCommandPlanRequest,
    WorkspaceCommandPlanStep,
)
from deskpilot.tools import create_builtin_registry

TASK_ID = f"tsk_{'1' * 32}"
NOW = datetime(2026, 8, 24, tzinfo=UTC)


def _provider() -> ModelProviderDescriptor:
    return ModelProviderDescriptor(
        provider_id="local_test",
        display_name="Local test provider",
        model="test-model",
        protocol=ModelProtocol.FAKE,
        location=ModelLocation.LOCAL,
        capabilities=ModelCapabilities(
            structured_output=True,
            strict_json_schema=True,
        ),
    )


def _compiler() -> PlanCompiler:
    tools = create_builtin_registry()
    capabilities = create_builtin_capability_catalog()
    agents = create_builtin_agent_registry(tools, (_provider(),))
    return PlanCompiler(agents, tools, capabilities)


def _source() -> TaskLoopSourceRef:
    return TaskLoopSourceRef(
        task_id=TASK_ID,
        user_message_id=f"msg_{'2' * 32}",
        user_message_digest="3" * 64,
        turn_planner_run_id=f"tpr_{'4' * 64}",
        turn_planner_run_digest="5" * 64,
        adjudication_id=f"tpa_{'6' * 64}",
        adjudication_digest="7" * 64,
        turn_plan_binding_id=f"tpb_{'8' * 64}",
        turn_plan_binding_digest="9" * 64,
    )


def _planner_recipe_digest(
    route_id: RouteId,
    *,
    variant_key: str | None = None,
    fixed_parameters: dict[str, str] | None = None,
) -> str:
    return sha256_digest(
        {
            **RouteRecipeCatalog.manifest(route_id, "2"),
            "variant_key": variant_key or route_id,
            "fixed_parameters": fixed_parameters or {},
        }
    )


def _step(
    route_id: RouteId,
    *,
    normalized_parameters: dict[str, str],
    raw_parameters: dict[str, str] | None = None,
    digit: str = "a",
    source_local_key: str | None = None,
    fixed_parameters: dict[str, str] | None = None,
) -> tuple[ModelPlannerNodeBinding, CapabilityRef, str, str]:
    capabilities = create_builtin_capability_catalog()
    contract, draft = RouteRecipeCatalog.compile(
        task_id=TASK_ID,
        route_id=route_id,
        parameters=normalized_parameters,
        capabilities=capabilities,
    )
    plan = _compiler().compile(contract, draft, generation=1)
    node = next(
        item
        for item in plan.nodes
        if item.kind is DraftNodeKind.CAPABILITY
        and (source_local_key is None or item.local_key == source_local_key)
    )
    assert node.capability is not None
    offer = TurnPlanningOfferRef(
        offer_id=f"tpo_{digit * 64}",
        offer_key=f"ofk_{digit * 64}",
        offer_digest=digit * 64,
    )
    cursor = 0
    bindings: list[TurnPlanningParameterBinding] = []
    for name, value in (raw_parameters or normalized_parameters).items():
        bindings.append(
            TurnPlanningParameterBinding.build(
                offer_key=offer.offer_key,
                parameter_name=name,
                value=value,
                source_start=cursor,
                source_end=cursor + len(value),
            )
        )
        cursor += len(value) + 1
    composite_node_id = f"pnd_{digit * 64}"
    composite_spec_digest = ("b" if digit != "b" else "c") * 64
    mapping = ModelPlannerNodeMapping.build(
        source_node_id=node.node_id,
        source_local_key=node.local_key,
        source_node_spec_digest=node.node_spec_digest,
        composite_node_id=composite_node_id,
        composite_local_key=f"s01_{node.local_key}",
        composite_node_spec_digest=composite_spec_digest,
    )
    step = ModelPlannerStepBinding.build(
        source=_source(),
        ordinal=1,
        offer=offer,
        recipe=TurnPlanningRecipeRef(
            route_id=route_id,
            route_version="2",
            route_manifest_digest=_planner_recipe_digest(
                route_id,
                variant_key=(
                    f"{route_id}:{next(iter(fixed_parameters.values()))}"
                    if fixed_parameters
                    else None
                ),
                fixed_parameters=fixed_parameters,
            ),
        ),
        policy_snapshot_digest="d" * 64,
        source_plan_id=plan.plan_id,
        source_plan_manifest_digest=plan.plan_manifest_digest,
        source_plan_binding_snapshot_digest=plan.binding_snapshot_digest,
        budget=node.budget,
        parameter_bindings=tuple(bindings),
        node_mappings=(mapping,),
        created_at=NOW,
    )
    authority = EffectiveNodeAuthority.build(
        composite_contract_digest=contract.digest,
        source_contract_digest=contract.digest,
        node_kind=DraftNodeKind.CAPABILITY,
        bound_agent=None,
        bound_tool=None,
        capability=node.capability,
        resource_scopes=(),
        privacy_classification=contract.privacy_policy.classification,
        allowed_provider_locations=contract.privacy_policy.allowed_provider_locations,
        allowed_privacy_modes=contract.privacy_policy.allowed_privacy_modes,
        external_egress_allowed=contract.privacy_policy.external_egress_allowed,
        max_risk_level=ToolRiskLevel.R0,
        budget=node.budget,
    )
    eligibility = RuntimeEligibilityProof.build(
        runtime_kind="capability_executor",
        bound_agent=None,
        capability=node.capability,
        executor_id=f"builtin.{route_id}.v1",
        executor_manifest_digest="e" * 64,
        registry_snapshot_digest="f" * 64,
    )
    node_binding = ModelPlannerNodeBinding.build(
        task_id=TASK_ID,
        user_message_id=step.source.user_message_id,
        draft_id=f"mpd_{digit * 64}",
        step_binding_id=step.step_binding_id,
        step_binding_digest=step.step_binding_digest,
        step_ordinal=step.ordinal,
        offer_id=step.offer.offer_id,
        offer_key=step.offer.offer_key,
        offer_digest=step.offer.offer_digest,
        recipe=step.recipe,
        policy_snapshot_digest=step.policy_snapshot_digest,
        source_contract_digest=contract.digest,
        source_plan_id=step.source_plan_id,
        source_plan_manifest_digest=step.source_plan_manifest_digest,
        source_node_id=mapping.source_node_id,
        source_node_spec_digest=mapping.source_node_spec_digest,
        composite_contract_digest=contract.digest,
        composite_plan_id=plan.plan_id,
        composite_plan_manifest_digest=plan.plan_manifest_digest,
        composite_node_id=mapping.composite_node_id,
        composite_node_spec_digest=mapping.composite_node_spec_digest,
        mapping=mapping,
        parameter_bindings=step.parameter_bindings,
        parameter_bindings_digest=step.parameter_bindings_digest,
        bound_input_manifest=dict(sorted(normalized_parameters.items())),
        bound_input_digest=sha256_digest(
            {"parameters": dict(sorted(normalized_parameters.items()))}
        ),
        effective_authority=authority,
        runtime_eligibility=eligibility,
    )
    return node_binding, node.capability, composite_node_id, composite_spec_digest


@pytest.mark.parametrize(
    ("route_id", "normalized", "raw", "expected_type", "expected"),
    (
        (
            "knowledge_lookup",
            {"query": "cats"},
            {"query": '"cats"'},
            KnowledgeLocalExecutorInput,
            {"query": "cats", "limit": 10},
        ),
        (
            "mcp_text_metrics",
            {"text": "one two"},
            None,
            McpTextMetricsExecutorInput,
            {"text": "one two"},
        ),
        (
            "workspace_snapshot_check",
            {"profile": "python-syntax", "path": "src/app.py"},
            {"profile": "PYTHON-SYNTAX", "path": '"src/app.py"'},
            WorkspaceSnapshotCheckExecutorInput,
            {"profile": "python-syntax", "path": "src/app.py"},
        ),
        (
            "workspace_python_test",
            {"project_path": "backend", "test_path": "tests/test_one.py"},
            None,
            WorkspacePythonTestExecutorInput,
            {"project_path": "backend", "test_path": "tests/test_one.py"},
        ),
        (
            "workspace_node_test",
            {"project_path": "frontend", "test_path": "src/app.test.ts"},
            None,
            WorkspaceNodeTestExecutorInput,
            {"project_path": "frontend", "test_path": "src/app.test.ts"},
        ),
        (
            "workspace_project_search",
            {"project_path": "backend", "query": "CapabilityRef"},
            None,
            WorkspaceProjectSearchExecutorInput,
            {"project_path": "backend", "query": "CapabilityRef"},
        ),
        (
            "workspace_project_batch_read",
            {
                "project_path": "backend",
                "paths_json": '["pyproject.toml","src/deskpilot/main.py"]',
            },
            None,
            WorkspaceProjectBatchReadExecutorInput,
            {
                "project_path": "backend",
                "paths": ["pyproject.toml", "src/deskpilot/main.py"],
            },
        ),
        (
            "workspace_git_inspect",
            {"project_path": ".", "operation": "status"},
            {"project_path": ".", "operation": "STATUS"},
            WorkspaceGitInspectExecutorInput,
            {"project_path": ".", "operation": "status"},
        ),
    ),
)
def test_catalog_binds_only_exact_persisted_step_parameters(
    route_id: RouteId,
    normalized: dict[str, str],
    raw: dict[str, str] | None,
    expected_type: type[object],
    expected: dict[str, object],
) -> None:
    node_binding, capability, _node_id, _node_spec_digest = _step(
        route_id,
        normalized_parameters=normalized,
        raw_parameters=raw,
    )
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())

    bound = catalog.bind_node(
        node_binding=node_binding,
    )

    assert isinstance(bound.arguments, expected_type)
    arguments = bound.arguments.model_dump(mode="json", exclude={"schema_version"})
    assert arguments == expected
    assert bound.capability == capability
    assert bound.dependency_result_refs == ()
    assert bound.consumed_result_refs == ()
    serialized = bound.arguments.model_dump_json()
    for reserved in ("argv", "cwd", "env", "approval", "result_ref", "capability_ref"):
        assert reserved not in serialized


def test_catalog_rejects_recipe_node_and_reserved_parameter_drift() -> None:
    node_binding, _capability, _node_id, _node_spec_digest = _step(
        "knowledge_lookup",
        normalized_parameters={"query": "cats"},
    )
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())
    changed_recipe = node_binding.model_copy(
        update={
            "recipe": node_binding.recipe.model_copy(update={"route_manifest_digest": "0" * 64})
        }
    )
    with pytest.raises(CapabilityInputLineageRejectedError, match="recipe changed"):
        catalog.bind_node(node_binding=changed_recipe)

    cwd = TurnPlanningParameterBinding.build(
        offer_key=node_binding.offer_key,
        parameter_name="cwd",
        value="C:/unsafe",
        source_start=20,
        source_end=29,
    )
    changed_values = node_binding.model_dump(
        mode="python", exclude={"schema_version", "node_binding_id", "binding_digest"}
    )
    changed_bindings = (*node_binding.parameter_bindings, cwd)
    changed_values["parameter_bindings"] = changed_bindings
    changed_values["parameter_bindings_digest"] = sha256_digest(
        {"parameter_bindings": [item.model_dump(mode="json") for item in changed_bindings]}
    )
    changed_parameters = ModelPlannerNodeBinding.build(**changed_values)
    with pytest.raises(CapabilityInputLineageRejectedError, match="trusted recipe"):
        catalog.bind_node(node_binding=changed_parameters)

    with pytest.raises(CapabilityInputLineageRejectedError, match="node mapping"):
        catalog.bind_node(
            node_binding=node_binding.model_copy(
                update={
                    "mapping": node_binding.mapping.model_copy(
                        update={"composite_node_spec_digest": "f" * 64}
                    )
                }
            )
        )


def test_command_profile_is_bound_only_from_the_fixed_server_offer() -> None:
    profile_id = "python.ruff.v1"
    node_binding, capability, _node_id, _node_spec_digest = _step(
        "workspace_command_profile",
        normalized_parameters={
            "project_path": "backend",
            "command_profile_id": profile_id,
        },
        raw_parameters={"project_path": "backend"},
        fixed_parameters={"command_profile_id": profile_id},
    )
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())
    profile = CommandProfileCatalog().resolve(profile_id)
    request = WorkspaceCommandPlanRequest.build(
        task_id=node_binding.task_id,
        plan_generation=1,
        project_path="backend",
        command_profile_ids=(profile_id,),
    )
    step = WorkspaceCommandPlanStep.build(
        sequence=1,
        depends_on=(),
        command_profile=profile,
    )
    plan = WorkspaceCommandPlan.build(
        request=request,
        ecosystem="python",
        catalog_digest="a" * 64,
        steps=(step,),
    )
    mapping = WorkspaceCommandPlanNodeMapping.build(
        command_step_id=step.step_id,
        command_step_digest=step.step_digest,
        command_step_sequence=1,
        step_binding_id=node_binding.step_binding_id,
        step_binding_digest=node_binding.step_binding_digest,
        step_ordinal=node_binding.step_ordinal,
        offer_id=node_binding.offer_id,
        offer_key=node_binding.offer_key,
        offer_digest=node_binding.offer_digest,
        composite_node_id=node_binding.composite_node_id,
        composite_node_spec_digest=node_binding.composite_node_spec_digest,
    )
    plan_binding = WorkspaceCommandPlanBinding.build(
        task_id=node_binding.task_id,
        loop_id=f"tlp_{'a' * 64}",
        draft_id=node_binding.draft_id,
        group_ordinal=1,
        expected_plan_id=node_binding.composite_plan_id,
        expected_plan_manifest_digest=node_binding.composite_plan_manifest_digest,
        command_plan=plan,
        mappings=(mapping,),
        created_at=datetime(2026, 8, 24, tzinfo=UTC),
    )
    proof = plan_binding.proof_for_node(node_binding.composite_node_id)

    with pytest.raises(CapabilityInputLineageRejectedError, match="Plan proof"):
        catalog.bind_node(node_binding=node_binding)

    bound = catalog.bind_node(
        node_binding=node_binding,
        workspace_command_plan_step=proof,
    )

    assert isinstance(bound.arguments, WorkspaceCommandExecutorInput)
    assert bound.arguments.project_path == "backend"
    assert bound.arguments.command_profile_id == profile_id
    assert bound.capability == capability
    assert tuple(item.parameter_name for item in node_binding.parameter_bindings) == (
        "project_path",
    )
    serialized = bound.arguments.model_dump_json()
    for forbidden in ("executable", "argv", "cwd", "environment", "shell"):
        assert forbidden not in serialized

    changed_manifest = node_binding.model_copy(
        update={
            "bound_input_manifest": {
                "project_path": "backend",
                "command_profile_id": "python.mypy.v1",
            }
        }
    )
    with pytest.raises(CapabilityInputLineageRejectedError, match="recipe changed"):
        catalog.bind_node(
            node_binding=changed_manifest,
            workspace_command_plan_step=proof,
        )


def _knowledge_result() -> KnowledgeSearchRead:
    material: dict[str, Any] = {
        "query_digest": sha256_digest({"query": "cats"}),
        "citations": (),
        "searched_sources": 0,
        "stale_source_ids": (),
    }
    return KnowledgeSearchRead(
        **material,
        result_digest=sha256_digest(material),
    )


def test_dependency_gate_requires_resolved_verified_result_but_is_not_consumed() -> None:
    node_binding, _capability, _node_id, _node_spec_digest = _step(
        "mcp_text_metrics",
        normalized_parameters={"text": "hello"},
    )
    value = _knowledge_result()
    result_ref = VerifiedCapabilityResultRef.build(
        task_id=TASK_ID,
        run_id=f"run_{'1' * 64}",
        plan_generation=1,
        producer_node_id=f"pnd_{'2' * 64}",
        producer_attempt=1,
        capability=CapabilityRef(
            capability_id="knowledge.local.v1",
            version="1.0.0",
            digest="3" * 64,
        ),
        result_kind=CapabilityResultKind.KNOWLEDGE,
        result_schema_digest=sha256_digest(KnowledgeSearchRead.model_json_schema()),
        result_digest=value.result_digest,
        verification_digest="4" * 64,
    )
    dependency = ResolvedVerifiedCapabilityResult.from_model(
        result_ref=result_ref,
        value=value,
    )
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())

    bound = catalog.bind_node(
        node_binding=node_binding,
        dependencies=(dependency,),
    )

    assert bound.dependency_result_refs == (result_ref,)
    assert bound.consumed_result_refs == ()
    assert "hello" in bound.arguments.model_dump_json()
    assert value.result_digest not in bound.arguments.model_dump_json()

    with pytest.raises(CapabilityInputDependencyRejectedError, match="resolved verified"):
        catalog.bind_node(
            node_binding=node_binding,
            dependencies=(object(),),  # type: ignore[arg-type]
        )


def test_catalog_consumes_only_exact_server_selected_artifact_dependencies() -> None:
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())
    cases = (
        (
            "build_html",
            "b",
            CapabilityResultKind.VERIFIED_CLAIMS,
            ArtifactHtmlExecutorInput,
            "verified_claims_digest",
        ),
        (
            "browser_verify",
            "c",
            CapabilityResultKind.ARTIFACT,
            BrowserVerifyExecutorInput,
            "artifact_digest",
        ),
    )
    for local_key, digit, result_kind, input_type, digest_field in cases:
        node_binding, _capability, _node_id, _node_spec_digest = _step(
            "research_to_html",
            normalized_parameters={"goal": "cats"},
            digit=digit,
            source_local_key=local_key,
        )
        result_digest = digit * 64
        result_ref = VerifiedCapabilityResultRef.build(
            task_id=TASK_ID,
            run_id=f"run_{digit * 64}",
            plan_generation=1,
            producer_node_id=f"pnd_{('d' if digit != 'd' else 'e') * 64}",
            producer_attempt=1,
            capability=CapabilityRef(
                capability_id="research.read.v1",
                version="1.1.0",
                digest="e" * 64,
            ),
            result_kind=result_kind,
            result_schema_digest="f" * 64,
            result_digest=result_digest,
            verification_digest="1" * 64,
        )
        dependency = ResolvedVerifiedCapabilityResult(
            result_ref=result_ref,
            output_manifest={"result_digest": result_digest},
            output_schema_digest="f" * 64,
        )

        bound = catalog.bind_node(
            node_binding=node_binding,
            dependencies=(dependency,),
        )

        assert isinstance(bound.arguments, input_type)
        assert getattr(bound.arguments, digest_field) == result_digest
        assert bound.consumed_result_refs == (result_ref,)


def test_catalog_exposes_thirteen_exact_runtime_capability_refs() -> None:
    catalog = CapabilityInputBindingCatalog(create_builtin_capability_catalog())

    refs = catalog.capabilities()

    assert {(item.capability_id, item.version) for item in refs} == {
        ("knowledge.local.v1", "1.0.0"),
        ("mcp.text.metrics.v1", "1.0.0"),
        ("workspace.snapshot.check.v1", "1.0.0"),
        ("workspace.python.test.v1", "1.0.0"),
        ("workspace.node.test.v1", "1.0.0"),
        ("workspace.patch.bundle.v1", "1.0.0"),
        ("workspace.project.search.v1", "1.0.0"),
        ("workspace.project.read_many.v1", "1.0.0"),
        ("workspace.git.inspect.v1", "1.0.0"),
        ("workspace.git.commit.v1", "1.0.0"),
        ("workspace.command.run.v1", "1.0.0"),
        ("artifact.html.v1", "1.2.0"),
        ("browser.verify.v1", "1.1.0"),
    }
    assert all(len(item.digest) == 64 for item in refs)
