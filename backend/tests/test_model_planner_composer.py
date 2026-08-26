from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.application.capability_catalog import (
    CapabilityCatalog,
    create_builtin_capability_catalog,
)
from deskpilot.application.model_planner_composer import (
    MODEL_PLANNER_PRODUCER_REF,
    ModelPlannerComposer,
    ModelPlannerDomainLimitError,
    ModelPlannerOfferRejectedError,
    RevalidatedOfferStep,
)
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.route_recipe_catalog import RouteOfferDraft, RouteRecipeCatalog
from deskpilot.application.turn_planner_runtime import turn_planning_offer_key
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.task_plans import (
    DraftNodeKind,
    ExecutablePlan,
    PlanNodeBudget,
    TaskBudget,
    TaskContractRef,
)
from deskpilot.domain.turn_planning import (
    TurnPlanningOffer,
    TurnPlanningParameterSpec,
    TurnPlanningRecipeRef,
)
from deskpilot.model_providers.fake import FakeModelProvider
from deskpilot.tools import create_builtin_registry

TASK_ID = f"tsk_{'1' * 32}"
MESSAGE_ID = f"msg_{'2' * 32}"
MESSAGE_DIGEST = sha256_digest({"message": "compose these exact offers"})
NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _environment(
    *,
    research_runtime_enabled: bool = False,
) -> tuple[
    ModelPlannerComposer,
    PlanCompiler,
    CapabilityCatalog,
    BoundAgentRef,
    FakeModelProvider,
]:
    tools = create_builtin_registry()
    provider = FakeModelProvider()
    agents = create_builtin_agent_registry(tools, (provider.descriptor,))
    capabilities = create_builtin_capability_catalog(
        research_runtime_enabled=research_runtime_enabled
    )
    compiler = PlanCompiler(agents, tools, capabilities)
    registration = agents.resolve_exact("builtin.turn_planner", "1.0.0")
    planner = BoundAgentRef(
        agent_id=registration.contract.agent_id,
        version=registration.contract.version,
        contract_digest=registration.contract.digest,
        prompt_package_digest=registration.prompt_package.digest,
    )
    return (
        ModelPlannerComposer(compiler, capabilities),
        compiler,
        capabilities,
        planner,
        provider,
    )


def _route_budget(budget: TaskBudget) -> PlanNodeBudget:
    return PlanNodeBudget(
        model_calls=budget.max_model_calls,
        tool_calls=budget.max_tool_calls,
        input_tokens=budget.max_input_tokens,
        output_tokens=budget.max_output_tokens,
        wall_seconds=budget.max_wall_seconds,
        retries=budget.max_retries,
        cost_micros=budget.max_cost_micros,
        handoffs=budget.max_handoffs,
    )


def _execution_agents(plan: ExecutablePlan) -> tuple[BoundAgentRef, ...]:
    agents = {
        (
            agent.agent_id,
            agent.version,
            agent.contract_digest,
            agent.prompt_package_digest,
        ): agent
        for node in plan.nodes
        if (agent := node.bound_agent) is not None
    }
    return tuple(agents[key] for key in sorted(agents))


def _offer(
    route: RouteOfferDraft,
    *,
    compiler: PlanCompiler,
    planner: BoundAgentRef,
    provider: FakeModelProvider,
) -> TurnPlanningOffer:
    expected_plan = compiler.compile(route.contract, route.draft, generation=1)
    execution_agents = _execution_agents(expected_plan)
    parameter_specs = tuple(
        TurnPlanningParameterSpec(
            parameter_name=item.name,
            required=item.required,
            min_length=1,
            max_length=4_000,
        )
        for item in route.parameter_specs
    )
    budget = _route_budget(route.contract.budget)
    policy_snapshot_digest = sha256_digest(
        {
            "schema_version": "deskpilot.turn-planning-policy-snapshot.v1",
            "task_contract_digest": route.contract.digest,
            "planner_agent": planner.model_dump(mode="json"),
            "agent_contract_digest": planner.contract_digest,
            "prompt_package_digest": planner.prompt_package_digest,
            "execution_agents": [item.model_dump(mode="json") for item in execution_agents],
            "execution_agents_digest": sha256_digest(
                {"execution_agents": [item.model_dump(mode="json") for item in execution_agents]}
            ),
            "expected_plan_id": expected_plan.plan_id,
            "expected_plan_manifest_digest": expected_plan.plan_manifest_digest,
            "expected_plan_binding_snapshot_digest": expected_plan.binding_snapshot_digest,
            "provider_snapshot_digest": sha256_digest(provider.descriptor),
            "capabilities": [item.model_dump(mode="json") for item in route.contract.capabilities],
            "trusted_recipe_digest": route.recipe_digest,
            "budget": budget.model_dump(mode="json"),
            "parameter_specs": [item.model_dump(mode="json") for item in parameter_specs],
        }
    )
    offer_key = turn_planning_offer_key(
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        variant_key=route.variant_key,
        task_contract_digest=route.contract.digest,
        execution_agents=execution_agents,
        expected_plan_id=expected_plan.plan_id,
        expected_plan_manifest_digest=expected_plan.plan_manifest_digest,
        expected_plan_binding_snapshot_digest=expected_plan.binding_snapshot_digest,
        provider_snapshot_digest=sha256_digest(provider.descriptor),
        recipe_digest=route.recipe_digest,
        policy_snapshot_digest=policy_snapshot_digest,
    )
    return TurnPlanningOffer.build(
        offer_key=offer_key,
        task_id=TASK_ID,
        user_message_id=MESSAGE_ID,
        user_message_digest=MESSAGE_DIGEST,
        intent_description=next(
            node.objective
            for node in route.draft.nodes
            if node.kind not in {DraftNodeKind.FINAL_ACCEPTANCE, DraftNodeKind.DELIVERY}
        ),
        task_contract=TaskContractRef(
            contract_id=route.contract.contract_id,
            version=route.contract.version,
            digest=route.contract.digest,
        ),
        expected_plan=expected_plan,
        capabilities=route.contract.capabilities,
        provider=provider.descriptor,
        trusted_recipe=TurnPlanningRecipeRef(
            route_id=route.route_id,
            route_version=route.route_version,
            route_manifest_digest=route.recipe_digest,
        ),
        budget=budget,
        parameter_specs=parameter_specs,
        policy_snapshot_digest=policy_snapshot_digest,
        created_at=NOW,
    )


def _parameters(route: RouteOfferDraft) -> dict[str, str]:
    values: dict[str, str] = dict(route.fixed_parameters)
    fixtures = {
        "goal": "goal",
        "query": "cats",
        "text": "hello",
        "path": "README.md",
        "old_text": "old",
        "new_text": "new",
        "changes_json": "{}",
        "project_path": ".",
        "test_path": "tests",
        "objective": "repair",
        "patch_path": "README.md",
        "target_path": "result.md",
        "content": "content",
        "source_path": "source.md",
        "file_path": "README.md",
        "profile": "python-syntax",
    }
    for spec in RouteRecipeCatalog.parameter_specs(route.route_id):
        if spec.name not in values and spec.required:
            values[spec.name] = fixtures[spec.name]
    if route.route_id == "workspace_directory_analyze":
        values["file_path"] = "README.md"
    if route.route_id == "workspace_dynamic_patch_test":
        values["patch_paths_json"] = canonical_json_bytes({"paths": [values["patch_path"]]}).decode(
            "utf-8"
        )
    return values


def _step(
    route: RouteOfferDraft,
    *,
    compiler: PlanCompiler,
    planner: BoundAgentRef,
    provider: FakeModelProvider,
    parameters: dict[str, str] | None = None,
) -> RevalidatedOfferStep:
    bound = _parameters(route) if parameters is None else parameters
    return RevalidatedOfferStep(
        offer=_offer(route, compiler=compiler, planner=planner, provider=provider),
        route=route,
        parameters=bound,
        parameter_binding_digest=sha256_digest({"route": route.variant_key, "parameters": bound}),
        planner_agent=planner,
    )


def _routes(
    capabilities: CapabilityCatalog,
    *variant_keys: str,
) -> tuple[RouteOfferDraft, ...]:
    by_key = {
        item.variant_key: item
        for item in RouteRecipeCatalog.offers_for(
            task_id=TASK_ID,
            capabilities=capabilities,
        )
    }
    return tuple(by_key[key] for key in variant_keys)


def test_composes_two_revalidated_offers_into_one_sealed_linear_plan() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    knowledge, metrics = _routes(capabilities, "knowledge_lookup", "mcp_text_metrics")
    steps = (
        _step(knowledge, compiler=compiler, planner=planner, provider=provider),
        _step(metrics, compiler=compiler, planner=planner, provider=provider),
    )

    composed = composer.compose(TASK_ID, steps)
    repeated = composer.compose(TASK_ID, steps)

    assert composed == repeated
    assert composed.draft.producer.kind == "model_planner"
    assert composed.draft.producer.producer_ref == MODEL_PLANNER_PRODUCER_REF
    assert composed.expected_plan.producer == composed.draft.producer
    assert composed.expected_plan.runtime_enabled is True
    assert composed.contract.budget.max_plan_nodes == 4
    assert composed.contract.budget.max_tool_calls == 2
    assert composed.contract.budget.max_wall_seconds == 180
    assert composed.contract.output_contract.media_type == "application/json"
    assert composed.contract.output_contract.require_citations is True
    assert tuple(item.criterion_id for item in composed.contract.acceptance_criteria) == (
        "ac_s01_knowledge_citations",
        "ac_s02_text_metrics",
    )
    by_key = {node.local_key: node for node in composed.draft.nodes}
    assert tuple(by_key) == (
        "s01_knowledge_lookup",
        "s02_mcp_text_metrics",
        "final_acceptance",
        "delivery",
    )
    assert by_key["s01_knowledge_lookup"].depends_on == ()
    assert by_key["s02_mcp_text_metrics"].depends_on == ("s01_knowledge_lookup",)
    assert by_key["final_acceptance"].depends_on == ("s02_mcp_text_metrics",)
    assert by_key["delivery"].depends_on == ("final_acceptance",)
    assert composed.step_bindings[0].source_to_composite_keys == (
        ("knowledge_lookup", "s01_knowledge_lookup"),
    )
    assert composed.step_bindings[0].parameter_summary[0].parameter_name == "query"
    assert composed.step_bindings[0].parameter_summary[0].value_digest == sha256_digest(
        {"value": "cats"}
    )
    assert not hasattr(composed.step_bindings[0].parameter_summary[0], "value")
    compiler.validate_manifest(composed.expected_plan)


def test_rejects_duplicate_or_drifted_offer_before_composition() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    knowledge, metrics = _routes(capabilities, "knowledge_lookup", "mcp_text_metrics")
    step = _step(knowledge, compiler=compiler, planner=planner, provider=provider)
    metrics_step = _step(metrics, compiler=compiler, planner=planner, provider=provider)

    with pytest.raises(ModelPlannerOfferRejectedError, match="duplicate Offers"):
        composer.compose(TASK_ID, (step, step))

    drifted = step.offer.model_copy(update={"policy_snapshot_digest": "0" * 64})
    with pytest.raises(ModelPlannerOfferRejectedError, match="policy drifted"):
        composer.compose(
            TASK_ID,
            (replace(step, offer=drifted), metrics_step),
        )


def test_rejects_fixed_parameter_drift_and_invalid_binding_digest() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    python_route, metrics = _routes(
        capabilities,
        "workspace_agent_patch_test:python",
        "mcp_text_metrics",
    )
    changed = _parameters(python_route)
    changed["test_kind"] = "node"
    bad_fixed = _step(
        python_route,
        compiler=compiler,
        planner=planner,
        provider=provider,
        parameters=changed,
    )
    metrics_step = _step(metrics, compiler=compiler, planner=planner, provider=provider)

    with pytest.raises(ModelPlannerOfferRejectedError, match="fixed parameter changed"):
        composer.compose(TASK_ID, (bad_fixed, metrics_step))

    bad_digest = RevalidatedOfferStep(
        offer=metrics_step.offer,
        route=metrics_step.route,
        parameters=metrics_step.parameters,
        parameter_binding_digest="not-a-digest",
        planner_agent=planner,
    )
    with pytest.raises(ModelPlannerOfferRejectedError, match="digest is invalid"):
        composer.compose(TASK_ID, (bad_digest, bad_fixed))


def test_rejects_budget_domain_overflow_instead_of_clamping() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    routes = _routes(
        capabilities,
        "workspace_dynamic_patch_test:python",
        "workspace_dynamic_patch_test:node",
    )
    steps = tuple(
        _step(route, compiler=compiler, planner=planner, provider=provider) for route in routes
    )

    assert sum(step.route.contract.budget.max_handoffs for step in steps) == 24
    with pytest.raises(ModelPlannerDomainLimitError, match="budget exceeds"):
        composer.compose(TASK_ID, steps)


def test_rejects_merged_structural_node_budget_over_twenty_four() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    routes = _routes(
        capabilities,
        "workspace_dynamic_patch_test:python",
        "workspace_dynamic_patch_test:node",
        "workspace_directory_list",
        "workspace_directory_analyze",
        "workspace_agent_patch_test:python",
        "workspace_agent_patch_test:node",
        "workspace_file_read",
    )
    steps = tuple(
        _step(route, compiler=compiler, planner=planner, provider=provider) for route in routes
    )
    merged_node_budget = 2 + sum(step.route.contract.budget.max_plan_nodes - 2 for step in steps)

    assert merged_node_budget == 25
    with pytest.raises(ModelPlannerDomainLimitError, match="twenty-four nodes"):
        composer.compose(TASK_ID, steps)


def test_research_and_local_read_merge_without_expanding_provider_location() -> None:
    composer, compiler, capabilities, planner, provider = _environment(
        research_runtime_enabled=True
    )
    research, knowledge = _routes(
        capabilities,
        "research_to_html",
        "knowledge_lookup",
    )
    research_step = _step(
        research,
        compiler=compiler,
        planner=planner,
        provider=provider,
    )
    knowledge_step = _step(
        knowledge,
        compiler=compiler,
        planner=planner,
        provider=provider,
    )

    composed = composer.compose(TASK_ID, (research_step, knowledge_step))

    assert composed.contract.privacy_policy.classification == "internal"
    assert composed.contract.privacy_policy.allowed_provider_locations == (ModelLocation.LOCAL,)
    assert composed.contract.privacy_policy.allowed_privacy_modes == (
        "local_preferred",
        "balanced",
    )
    assert composed.contract.privacy_policy.external_egress_allowed is True
    assert composed.contract.research == research.contract.research
    assert composed.contract.workspace == research.contract.workspace
    assert composed.contract.browser_verify == research.contract.browser_verify
    assert composed.contract.workspace is not None
    assert (
        composed.contract.workspace.max_total_bytes == research.contract.workspace.max_total_bytes  # type: ignore[union-attr]
    )
    assert "server_bound_step_inputs_v1" in composed.contract.constraints
    assert "exact_capability_per_composite_node_v1" in composed.contract.constraints
    by_key = {node.local_key: node for node in composed.draft.nodes}
    assert by_key["s02_knowledge_lookup"].depends_on == ("s01_browser_verify",)
    assert by_key["final_acceptance"].acceptance_refs == ("ac_s01_no_external_network",)
    coverage = {item.criterion_id: item for item in composed.expected_plan.acceptance_coverage}
    assert set(coverage) == {
        "ac_s01_citations",
        "ac_s01_html",
        "ac_s01_browser",
        "ac_s01_no_external_network",
        "ac_s02_knowledge_citations",
    }
    final_node = next(
        node for node in composed.expected_plan.nodes if node.local_key == "final_acceptance"
    )
    assert coverage["ac_s01_no_external_network"].node_ids == (final_node.node_id,)
    compiler.validate_manifest(composed.expected_plan)


def test_rejects_empty_or_legacy_single_offer() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    (knowledge,) = _routes(capabilities, "knowledge_lookup")
    step = _step(knowledge, compiler=compiler, planner=planner, provider=provider)

    with pytest.raises(ModelPlannerOfferRejectedError, match="one and eight"):
        composer.compose(TASK_ID, ())
    with pytest.raises(ModelPlannerOfferRejectedError, match="retain direct execution"):
        composer.compose(TASK_ID, (step,))


def test_composes_one_planner_only_offer_for_the_generic_task_loop() -> None:
    composer, compiler, capabilities, planner, provider = _environment()
    (search,) = _routes(capabilities, "workspace_project_search")
    step = _step(search, compiler=compiler, planner=planner, provider=provider)

    composed = composer.compose(TASK_ID, (step,))

    assert composed.draft.producer.kind == "model_planner"
    assert tuple(node.local_key for node in composed.draft.nodes) == (
        "s01_workspace_project_search",
        "final_acceptance",
        "delivery",
    )
    assert composed.step_bindings[0].route_id == "workspace_project_search"
    compiler.validate_manifest(composed.expected_plan)


def test_input_dataclass_copies_parameter_mapping() -> None:
    _, compiler, capabilities, planner, provider = _environment()
    (metrics,) = _routes(capabilities, "mcp_text_metrics")
    parameters = _parameters(metrics)
    step = _step(
        metrics,
        compiler=compiler,
        planner=planner,
        provider=provider,
        parameters=parameters,
    )

    parameters["text"] = "changed after construction"

    assert dict(step.parameters) == {"text": "hello"}
    with pytest.raises(TypeError):
        step.parameters["text"] = "mutation"  # type: ignore[index]


def test_offer_builder_fixture_has_no_unaccounted_fields() -> None:
    """Keep test Offer construction aligned with the immutable v1 manifest."""

    _, compiler, capabilities, planner, provider = _environment()
    (knowledge,) = _routes(capabilities, "knowledge_lookup")
    offer = _offer(knowledge, compiler=compiler, planner=planner, provider=provider)
    assert set(offer.model_dump(mode="json")) == {
        "schema_version",
        "offer_id",
        "offer_key",
        "task_id",
        "user_message_id",
        "user_message_digest",
        "intent_description",
        "task_contract",
        "execution_agents",
        "expected_plan",
        "capabilities",
        "provider",
        "provider_snapshot_digest",
        "trusted_recipe",
        "budget",
        "parameter_specs",
        "policy_snapshot_digest",
        "created_at",
        "offer_digest",
    }
    assert isinstance(offer.model_dump(mode="json"), dict)
