"""Pure server-owned composition of revalidated Turn Planner offers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, cast

from pydantic import ValidationError

from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.plan_compiler import PlanCompiler, PlanCompilerError
from deskpilot.application.route_recipe_catalog import RouteOfferDraft, RouteRecipeCatalog
from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.agent_contracts import BoundAgentRef
from deskpilot.domain.task_plans import (
    AcceptanceCriterion,
    BrowserVerifyContract,
    CapabilityRef,
    DraftNodeKind,
    DraftPlan,
    DraftPlanNode,
    ExecutablePlan,
    OutputContract,
    PlanNodeBudget,
    PlanProducer,
    PrivacyPolicy,
    ResearchContract,
    TaskBudget,
    TaskContract,
    TaskWorkspaceContract,
    VerificationProfile,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import TurnPlanningOffer, TurnPlanningParameterSpec

MODEL_PLANNER_PRODUCER_REF = "deskpilot.offer-composer.v1"
_MAX_COMPOSITE_STEPS = 8
_MAX_PLAN_NODES = 20
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_RISK_ORDER = {
    ToolRiskLevel.R0: 0,
    ToolRiskLevel.R1: 1,
    ToolRiskLevel.R2: 2,
    ToolRiskLevel.R3: 3,
    ToolRiskLevel.R4: 4,
}
_CLASSIFICATION_ORDER = {"public": 0, "internal": 1, "sensitive": 2}
_COMPOSITE_CONSTRAINTS = (
    "model_planner_offer_composition_v1",
    "server_bound_step_inputs_v1",
    "exact_capability_per_composite_node_v1",
    "verified_result_ref_dependencies_v1",
    "composite_workspace_quota_is_task_total_v1",
)


class ModelPlannerCompositionError(RuntimeError):
    """The server could not prove a least-authority composite plan."""

    code = "MODEL_PLANNER_COMPOSITION_REJECTED"


class ModelPlannerOfferRejectedError(ModelPlannerCompositionError):
    code = "MODEL_PLANNER_OFFER_REJECTED"


class ModelPlannerDomainLimitError(ModelPlannerCompositionError):
    code = "MODEL_PLANNER_DOMAIN_LIMIT_EXCEEDED"


class ModelPlannerPrivacyConflictError(ModelPlannerCompositionError):
    code = "MODEL_PLANNER_PRIVACY_CONFLICT"


@dataclass(frozen=True, slots=True)
class RevalidatedOfferStep:
    """One selected Offer plus server-revalidated recipe and parameter proof."""

    offer: TurnPlanningOffer
    route: RouteOfferDraft
    parameters: Mapping[str, str]
    parameter_binding_digest: str
    planner_agent: BoundAgentRef

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", MappingProxyType(dict(self.parameters)))


@dataclass(frozen=True, slots=True)
class ModelPlannerParameterSummary:
    parameter_name: str
    value_digest: str
    value_length: int


@dataclass(frozen=True, slots=True)
class ModelPlannerStepBinding:
    step_index: int
    offer_key: str
    route_id: str
    source_to_composite_keys: tuple[tuple[str, str], ...]
    parameter_binding_digest: str
    parameter_summary: tuple[ModelPlannerParameterSummary, ...]


@dataclass(frozen=True, slots=True)
class ModelPlannerComposition:
    contract: TaskContract
    draft: DraftPlan
    expected_plan: ExecutablePlan
    step_bindings: tuple[ModelPlannerStepBinding, ...]


class ModelPlannerComposer:
    """Compose only server-authored subgraphs; no model-authored graph enters here."""

    def __init__(self, plan_compiler: PlanCompiler, capabilities: CapabilityCatalog) -> None:
        self._compiler = plan_compiler
        self._capabilities = capabilities

    def compose(
        self,
        task_id: str,
        steps: tuple[RevalidatedOfferStep, ...],
    ) -> ModelPlannerComposition:
        if not 1 <= len(steps) <= _MAX_COMPOSITE_STEPS:
            raise ModelPlannerOfferRejectedError(
                "A generic Task Loop Plan requires between one and eight Offers"
            )
        if len(steps) == 1 and not RouteRecipeCatalog.is_planner_only_route(
            steps[0].route.route_id
        ):
            raise ModelPlannerOfferRejectedError(
                "A legacy single-step Offer must retain direct execution"
            )
        self._validate_scope_and_uniqueness(task_id, steps)
        for step in steps:
            self._validate_offer(step)
            self._validate_parameters(step)

        criteria, acceptance_maps = self._acceptance_criteria(steps)
        business_nodes, node_maps, final_acceptance_refs = self._business_nodes(
            steps,
            acceptance_maps,
        )
        actual_node_count = len(business_nodes) + 2
        structural_node_budget = 2 + sum(
            step.route.contract.budget.max_plan_nodes - 2 for step in steps
        )
        if actual_node_count > _MAX_PLAN_NODES or structural_node_budget > _MAX_PLAN_NODES:
            raise ModelPlannerDomainLimitError("Composite Plan exceeds twenty nodes")

        final_budget = self._control_budget()
        last_leaves = self._target_leaves(steps[-1].route.draft, node_maps[-1])
        if not last_leaves:
            raise ModelPlannerOfferRejectedError("Final Offer has no executable leaf")
        nodes = (
            *business_nodes,
            DraftPlanNode(
                local_key="final_acceptance",
                kind=DraftNodeKind.FINAL_ACCEPTANCE,
                objective="Deterministically verify every namespaced step result and proof.",
                depends_on=last_leaves,
                acceptance_refs=final_acceptance_refs,
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=final_budget,
            ),
            DraftPlanNode(
                local_key="delivery",
                kind=DraftNodeKind.DELIVERY,
                objective="Deliver only the verified composite result.",
                depends_on=("final_acceptance",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=final_budget,
            ),
        )
        draft = DraftPlan(
            task_id=task_id,
            contract_version=1,
            producer=PlanProducer(
                kind="model_planner",
                producer_ref=MODEL_PLANNER_PRODUCER_REF,
            ),
            nodes=nodes,
        )
        contract = self._contract(
            task_id,
            steps,
            criteria,
            structural_node_budget,
        )
        try:
            expected_plan = self._compiler.compile(contract, draft, generation=1)
        except PlanCompilerError as error:
            raise ModelPlannerCompositionError(
                "Composite Plan failed deterministic compilation"
            ) from error
        bindings = tuple(
            ModelPlannerStepBinding(
                step_index=index,
                offer_key=step.offer.offer_key,
                route_id=step.route.route_id,
                source_to_composite_keys=tuple(node_maps[index - 1].items()),
                parameter_binding_digest=step.parameter_binding_digest,
                parameter_summary=tuple(
                    ModelPlannerParameterSummary(
                        parameter_name=name,
                        value_digest=sha256_digest({"value": value}),
                        value_length=len(value),
                    )
                    for name, value in sorted(step.parameters.items())
                ),
            )
            for index, step in enumerate(steps, start=1)
        )
        return ModelPlannerComposition(
            contract=contract,
            draft=draft,
            expected_plan=expected_plan,
            step_bindings=bindings,
        )

    @staticmethod
    def _validate_scope_and_uniqueness(
        task_id: str,
        steps: tuple[RevalidatedOfferStep, ...],
    ) -> None:
        first = steps[0].offer
        scope = (task_id, first.user_message_id, first.user_message_digest)
        offer_ids = tuple(step.offer.offer_id for step in steps)
        offer_keys = tuple(step.offer.offer_key for step in steps)
        if len(offer_ids) != len(set(offer_ids)) or len(offer_keys) != len(set(offer_keys)):
            raise ModelPlannerOfferRejectedError("Composite Plan contains duplicate Offers")
        if any(
            (
                step.offer.task_id,
                step.offer.user_message_id,
                step.offer.user_message_digest,
            )
            != scope
            for step in steps
        ):
            raise ModelPlannerOfferRejectedError(
                "Composite Offers cross Task or persisted-message scope"
            )
        planner_bindings = {step.planner_agent for step in steps}
        if len(planner_bindings) != 1:
            raise ModelPlannerOfferRejectedError(
                "Composite Offers do not share one exact Planner binding"
            )

    def _validate_offer(self, step: RevalidatedOfferStep) -> None:
        offer = step.offer
        route = step.route
        matches = RouteRecipeCatalog.offers_for(
            task_id=offer.task_id,
            capabilities=self._capabilities,
            eligible_variant_keys=frozenset({route.variant_key}),
        )
        if matches != (route,):
            raise ModelPlannerOfferRejectedError(
                "Selected Offer no longer resolves to one exact server recipe"
            )
        try:
            expected_plan = self._compiler.compile(route.contract, route.draft, generation=1)
        except PlanCompilerError as error:
            raise ModelPlannerOfferRejectedError(
                "Selected Offer recipe no longer compiles"
            ) from error
        expected_specs = tuple(
            TurnPlanningParameterSpec(
                parameter_name=item.name,
                required=item.required,
                min_length=1,
                max_length=4_000,
            )
            for item in route.parameter_specs
        )
        expected_budget = self._route_budget(route.contract.budget)
        expected_agents = self._execution_agents(expected_plan)
        expected_policy_digest = sha256_digest(
            {
                "schema_version": "deskpilot.turn-planning-policy-snapshot.v1",
                "task_contract_digest": route.contract.digest,
                "planner_agent": step.planner_agent.model_dump(mode="json"),
                "agent_contract_digest": step.planner_agent.contract_digest,
                "prompt_package_digest": step.planner_agent.prompt_package_digest,
                "execution_agents": [item.model_dump(mode="json") for item in expected_agents],
                "execution_agents_digest": sha256_digest(
                    {"execution_agents": [item.model_dump(mode="json") for item in expected_agents]}
                ),
                "expected_plan_id": expected_plan.plan_id,
                "expected_plan_manifest_digest": expected_plan.plan_manifest_digest,
                "expected_plan_binding_snapshot_digest": (expected_plan.binding_snapshot_digest),
                "provider_snapshot_digest": sha256_digest(offer.provider),
                "capabilities": [
                    item.model_dump(mode="json") for item in route.contract.capabilities
                ],
                "trusted_recipe_digest": route.recipe_digest,
                "budget": expected_budget.model_dump(mode="json"),
                "parameter_specs": [item.model_dump(mode="json") for item in expected_specs],
            }
        )
        expected_offer_key = self._offer_key(
            offer=offer,
            route=route,
            execution_agents=expected_agents,
            expected_plan=expected_plan,
            policy_snapshot_digest=expected_policy_digest,
        )
        if (
            offer.offer_key != expected_offer_key
            or offer.task_id != route.contract.task_id
            or offer.task_contract.contract_id != route.contract.contract_id
            or offer.task_contract.version != route.contract.version
            or offer.task_contract.digest != route.contract.digest
            or offer.expected_plan != expected_plan
            or offer.capabilities != route.contract.capabilities
            or offer.execution_agents != expected_agents
            or offer.trusted_recipe.route_id != route.route_id
            or offer.trusted_recipe.route_version != route.route_version
            or offer.trusted_recipe.route_manifest_digest != route.recipe_digest
            or offer.budget != expected_budget
            or offer.parameter_specs != expected_specs
            or offer.provider_snapshot_digest != sha256_digest(offer.provider)
            or offer.policy_snapshot_digest != expected_policy_digest
        ):
            raise ModelPlannerOfferRejectedError(
                "Selected Offer Contract, Plan, capability, budget, recipe, or policy drifted"
            )

    @staticmethod
    def _offer_key(
        *,
        offer: TurnPlanningOffer,
        route: RouteOfferDraft,
        execution_agents: tuple[BoundAgentRef, ...],
        expected_plan: ExecutablePlan,
        policy_snapshot_digest: str,
    ) -> str:
        material = {
            "schema_version": "deskpilot.turn-planning-offer-key.v1",
            "task_id": offer.task_id,
            "user_message_id": offer.user_message_id,
            "user_message_digest": offer.user_message_digest,
            "variant_key": route.variant_key,
            "task_contract_digest": route.contract.digest,
            "execution_agents": [item.model_dump(mode="json") for item in execution_agents],
            "expected_plan_id": expected_plan.plan_id,
            "expected_plan_manifest_digest": expected_plan.plan_manifest_digest,
            "expected_plan_binding_snapshot_digest": (expected_plan.binding_snapshot_digest),
            "provider_snapshot_digest": sha256_digest(offer.provider),
            "recipe_digest": route.recipe_digest,
            "policy_snapshot_digest": policy_snapshot_digest,
        }
        return f"ofk_{sha256_digest(material)}"

    @staticmethod
    def _validate_parameters(step: RevalidatedOfferStep) -> None:
        if _DIGEST.fullmatch(step.parameter_binding_digest) is None:
            raise ModelPlannerOfferRejectedError("Parameter binding digest is invalid")
        parameters = dict(step.parameters)
        specs = {
            item.name: item for item in RouteRecipeCatalog.parameter_specs(step.route.route_id)
        }
        allowed = set(specs) | set(step.route.fixed_parameters)
        if step.route.route_id == "workspace_dynamic_patch_test":
            allowed.add("patch_paths_json")
        if set(parameters) - allowed:
            raise ModelPlannerOfferRejectedError("Composite step contains an unknown parameter")
        required = {item.name for item in specs.values() if item.required} | set(
            step.route.fixed_parameters
        )
        if not required.issubset(parameters):
            raise ModelPlannerOfferRejectedError("Composite step omitted a required parameter")
        for name, fixed in step.route.fixed_parameters.items():
            if parameters.get(name) != fixed:
                raise ModelPlannerOfferRejectedError("Composite fixed parameter changed")
        for name, value in parameters.items():
            if not isinstance(value, str) or not value or len(value) > 4_000:
                raise ModelPlannerOfferRejectedError("Composite parameter shape is invalid")
            spec = specs.get(name)
            if spec is not None and spec.allowed_values and value not in spec.allowed_values:
                raise ModelPlannerOfferRejectedError("Composite parameter enum is invalid")
        if step.route.route_id == "workspace_dynamic_patch_test":
            expected_paths = canonical_json_bytes({"paths": [parameters["patch_path"]]}).decode(
                "utf-8"
            )
            if parameters.get("patch_paths_json") != expected_paths:
                raise ModelPlannerOfferRejectedError("Composite derived Patch path proof changed")

    @classmethod
    def _acceptance_criteria(
        cls,
        steps: tuple[RevalidatedOfferStep, ...],
    ) -> tuple[
        tuple[AcceptanceCriterion, ...],
        tuple[dict[str, str], ...],
    ]:
        criteria: list[AcceptanceCriterion] = []
        maps: list[dict[str, str]] = []
        for index, step in enumerate(steps, start=1):
            mapping: dict[str, str] = {}
            for criterion in step.route.contract.acceptance_criteria:
                target = cls._acceptance_key(index, criterion.criterion_id)
                mapping[criterion.criterion_id] = target
                criteria.append(
                    AcceptanceCriterion.model_validate(
                        {
                            **criterion.model_dump(mode="json"),
                            "criterion_id": target,
                        }
                    )
                )
            maps.append(mapping)
        criterion_ids = tuple(item.criterion_id for item in criteria)
        if len(criteria) > 50 or len(criterion_ids) != len(set(criterion_ids)):
            raise ModelPlannerDomainLimitError(
                "Composite acceptance criteria exceed the domain limit or collide"
            )
        return tuple(criteria), tuple(maps)

    @classmethod
    def _business_nodes(
        cls,
        steps: tuple[RevalidatedOfferStep, ...],
        acceptance_maps: tuple[dict[str, str], ...],
    ) -> tuple[
        tuple[DraftPlanNode, ...],
        tuple[dict[str, str], ...],
        tuple[str, ...],
    ]:
        target_nodes: list[DraftPlanNode] = []
        node_maps: list[dict[str, str]] = []
        final_acceptance_refs: list[str] = []
        previous_leaves: tuple[str, ...] = ()
        for index, (step, acceptance_map) in enumerate(
            zip(steps, acceptance_maps, strict=True),
            start=1,
        ):
            source_business = tuple(
                node
                for node in step.route.draft.nodes
                if node.kind not in {DraftNodeKind.FINAL_ACCEPTANCE, DraftNodeKind.DELIVERY}
            )
            final_nodes = tuple(
                node
                for node in step.route.draft.nodes
                if node.kind is DraftNodeKind.FINAL_ACCEPTANCE
            )
            delivery_nodes = tuple(
                node for node in step.route.draft.nodes if node.kind is DraftNodeKind.DELIVERY
            )
            if (
                not source_business
                or len(final_nodes) != 1
                or len(delivery_nodes) != 1
                or delivery_nodes[0].acceptance_refs
                or (
                    final_nodes[0].acceptance_refs
                    and final_nodes[0].verification_profile is not VerificationProfile.DETERMINISTIC
                )
            ):
                raise ModelPlannerOfferRejectedError(
                    "Trusted recipe control nodes cannot be safely consolidated"
                )
            try:
                final_acceptance_refs.extend(
                    acceptance_map[item] for item in final_nodes[0].acceptance_refs
                )
            except KeyError as error:
                raise ModelPlannerOfferRejectedError(
                    "Trusted final acceptance references an unknown criterion"
                ) from error
            source_keys = {node.local_key for node in source_business}
            mapping = {
                node.local_key: cls._node_key(index, node.local_key) for node in source_business
            }
            if len(mapping) != len(set(mapping.values())):
                raise ModelPlannerDomainLimitError("Composite node keys collide")
            node_maps.append(mapping)
            for node in source_business:
                if any(dependency not in source_keys for dependency in node.depends_on):
                    raise ModelPlannerOfferRejectedError(
                        "Trusted business node depends on a removed control node"
                    )
                if node.handoff_parent is not None and node.handoff_parent not in source_keys:
                    raise ModelPlannerOfferRejectedError(
                        "Trusted business handoff references a removed control node"
                    )
                depends_on = tuple(mapping[item] for item in node.depends_on)
                if not depends_on:
                    depends_on = previous_leaves
                handoff_parent = (
                    mapping[node.handoff_parent] if node.handoff_parent is not None else None
                )
                try:
                    acceptance_refs = tuple(acceptance_map[item] for item in node.acceptance_refs)
                except KeyError as error:
                    raise ModelPlannerOfferRejectedError(
                        "Trusted business node references an unknown acceptance criterion"
                    ) from error
                try:
                    target_nodes.append(
                        DraftPlanNode.model_validate(
                            {
                                **node.model_dump(mode="json"),
                                "local_key": mapping[node.local_key],
                                "depends_on": depends_on,
                                "handoff_parent": handoff_parent,
                                "acceptance_refs": acceptance_refs,
                            }
                        )
                    )
                except ValidationError as error:
                    raise ModelPlannerDomainLimitError(
                        "Composite node exceeds a domain field limit"
                    ) from error
            previous_leaves = cls._target_leaves(step.route.draft, mapping)
            if not previous_leaves:
                raise ModelPlannerOfferRejectedError("Trusted Offer has no business leaf")
        return tuple(target_nodes), tuple(node_maps), tuple(final_acceptance_refs)

    def _contract(
        self,
        task_id: str,
        steps: tuple[RevalidatedOfferStep, ...],
        criteria: tuple[AcceptanceCriterion, ...],
        node_count: int,
    ) -> TaskContract:
        contracts = tuple(step.route.contract for step in steps)
        first = contracts[0]
        if any(
            contract.task_id != task_id
            or contract.contract_id != first.contract_id
            or contract.version != 1
            or contract.previous_contract_digest is not None
            or contract.goal_ref != first.goal_ref
            or contract.created_by != "trusted_template"
            for contract in contracts
        ):
            raise ModelPlannerOfferRejectedError(
                "Composite source Contracts do not share one initial Task scope"
            )
        privacy = self._merge_privacy(tuple(item.privacy_policy for item in contracts))
        objective = "Execute these server-bound steps in order: " + " | ".join(
            item.normalized_objective for item in contracts
        )
        if len(objective) > 1_000:
            raise ModelPlannerDomainLimitError(
                "Composite normalized objective exceeds the domain limit"
            )
        try:
            return TaskContract(
                contract_id=first.contract_id,
                task_id=task_id,
                version=1,
                goal_ref=first.goal_ref,
                normalized_objective=objective,
                acceptance_criteria=criteria,
                constraints=self._stable_union(
                    (
                        *(item.constraints for item in contracts),
                        _COMPOSITE_CONSTRAINTS,
                    )
                ),
                resource_scopes=self._stable_union(item.resource_scopes for item in contracts),
                privacy_policy=privacy,
                max_risk_level=max(
                    (item.max_risk_level for item in contracts),
                    key=_RISK_ORDER.__getitem__,
                ),
                budget=self._merge_budget(contracts, node_count),
                output_contract=self._merge_output(
                    tuple(item.output_contract for item in contracts)
                ),
                capabilities=self._merge_capabilities(contracts),
                research=self._merge_research(
                    tuple(item.research for item in contracts if item.research is not None)
                ),
                workspace=self._merge_workspace(
                    tuple(item.workspace for item in contracts if item.workspace is not None)
                ),
                browser_verify=self._merge_browser(
                    tuple(
                        item.browser_verify for item in contracts if item.browser_verify is not None
                    )
                ),
                created_by="trusted_template",
            )
        except ValidationError as error:
            raise ModelPlannerDomainLimitError(
                "Composite Contract exceeds a domain field limit"
            ) from error

    @staticmethod
    def _merge_privacy(policies: tuple[PrivacyPolicy, ...]) -> PrivacyPolicy:
        first = policies[0]
        allowed_locations = tuple(
            location
            for location in first.allowed_provider_locations
            if all(location in item.allowed_provider_locations for item in policies[1:])
        )
        allowed_modes = tuple(
            mode
            for mode in first.allowed_privacy_modes
            if all(mode in item.allowed_privacy_modes for item in policies[1:])
        )
        if not allowed_locations or not allowed_modes:
            raise ModelPlannerPrivacyConflictError(
                "Composite Offers have incompatible privacy policies"
            )
        classification = max(
            (item.classification for item in policies),
            key=_CLASSIFICATION_ORDER.__getitem__,
        )
        return PrivacyPolicy(
            classification=classification,
            allowed_provider_locations=allowed_locations,
            allowed_privacy_modes=allowed_modes,
            # A true source Contract necessarily carries a currently bound
            # external-egress capability; exact per-node capability binding
            # still prevents an unrelated local step from gaining that tool.
            external_egress_allowed=any(item.external_egress_allowed for item in policies),
        )

    @staticmethod
    def _merge_budget(
        contracts: tuple[TaskContract, ...],
        node_count: int,
    ) -> TaskBudget:
        fields = (
            "max_model_calls",
            "max_tool_calls",
            "max_input_tokens",
            "max_output_tokens",
            "max_wall_seconds",
            "max_retries",
            "max_cost_micros",
            "max_handoffs",
        )
        values = {
            field: sum(cast(int, getattr(contract.budget, field)) for contract in contracts)
            for field in fields
        }
        try:
            return TaskBudget(**values, max_plan_nodes=node_count)
        except ValidationError as error:
            raise ModelPlannerDomainLimitError("Composite budget exceeds a domain limit") from error

    @staticmethod
    def _merge_output(outputs: tuple[OutputContract, ...]) -> OutputContract:
        languages = {item.language for item in outputs}
        if len(languages) != 1:
            raise ModelPlannerCompositionError("Composite output languages are incompatible")
        media_types = {item.media_type for item in outputs}
        media_type: Literal["application/json", "text/html", "text/markdown"] = (
            next(iter(media_types)) if len(media_types) == 1 else "application/json"
        )
        return OutputContract(
            media_type=media_type,
            language=outputs[0].language,
            require_citations=any(item.require_citations for item in outputs),
            disclose_partial=all(item.disclose_partial for item in outputs),
        )

    @staticmethod
    def _merge_capabilities(
        contracts: tuple[TaskContract, ...],
    ) -> tuple[CapabilityRef, ...]:
        by_key: dict[tuple[str, str], CapabilityRef] = {}
        for contract in contracts:
            for capability in contract.capabilities:
                current = by_key.get(capability.key)
                if current is not None and current.digest != capability.digest:
                    raise ModelPlannerOfferRejectedError(
                        "Composite capability version has conflicting digests"
                    )
                by_key[capability.key] = capability
        return tuple(by_key[key] for key in sorted(by_key))

    @staticmethod
    def _merge_research(items: tuple[ResearchContract, ...]) -> ResearchContract | None:
        if not items:
            return None
        freshness = tuple(
            item.freshness_seconds for item in items if item.freshness_seconds is not None
        )
        try:
            return ResearchContract(
                max_search_calls=sum(item.max_search_calls for item in items),
                max_page_reads=sum(item.max_page_reads for item in items),
                max_results_per_search=max(item.max_results_per_search for item in items),
                minimum_distinct_sources=max(item.minimum_distinct_sources for item in items),
                allowed_domains=tuple(
                    sorted({domain for item in items for domain in item.allowed_domains})
                ),
                freshness_seconds=min(freshness) if freshness else None,
            )
        except ValidationError as error:
            raise ModelPlannerDomainLimitError(
                "Composite research policy exceeds a domain limit"
            ) from error

    @staticmethod
    def _merge_workspace(
        items: tuple[TaskWorkspaceContract, ...],
    ) -> TaskWorkspaceContract | None:
        if not items:
            return None
        first = items[0]
        if any(item.workspace_ref != first.workspace_ref for item in items[1:]):
            raise ModelPlannerCompositionError(
                "Composite workspace policies target different workspaces"
            )
        extensions = cast(
            tuple[Literal[".html", ".css", ".md", ".pdf"], ...],
            ModelPlannerComposer._stable_union(item.allowed_extensions for item in items),
        )
        try:
            return TaskWorkspaceContract(
                workspace_ref=first.workspace_ref,
                allowed_extensions=extensions,
                max_total_bytes=sum(item.max_total_bytes for item in items),
                max_files=sum(item.max_files for item in items),
                retention_days=max(item.retention_days for item in items),
                allow_user_path_export=any(item.allow_user_path_export for item in items),
            )
        except ValidationError as error:
            raise ModelPlannerDomainLimitError(
                "Composite workspace policy exceeds a domain limit"
            ) from error

    @staticmethod
    def _merge_browser(
        items: tuple[BrowserVerifyContract, ...],
    ) -> BrowserVerifyContract | None:
        if not items:
            return None
        first = items[0]
        if any(item != first for item in items[1:]):
            raise ModelPlannerCompositionError(
                "Composite browser verification policies are incompatible"
            )
        return first

    @staticmethod
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

    @staticmethod
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

    @staticmethod
    def _stable_union(groups: Iterable[Sequence[str]]) -> tuple[str, ...]:
        result: list[str] = []
        seen: set[str] = set()
        for group in groups:
            for item in group:
                if item not in seen:
                    seen.add(item)
                    result.append(item)
        return tuple(result)

    @staticmethod
    def _control_budget() -> PlanNodeBudget:
        return PlanNodeBudget(
            model_calls=0,
            tool_calls=0,
            input_tokens=0,
            output_tokens=0,
            wall_seconds=15,
            retries=0,
            cost_micros=0,
            handoffs=0,
        )

    @staticmethod
    def _acceptance_key(step_index: int, source_key: str) -> str:
        stem = source_key.removeprefix("ac_")
        candidate = f"ac_s{step_index:02d}_{stem}"
        if len(candidate) <= 63:
            return candidate
        return f"ac_s{step_index:02d}_{sha256_digest({'source': source_key})[:16]}"

    @staticmethod
    def _node_key(step_index: int, source_key: str) -> str:
        candidate = f"s{step_index:02d}_{source_key}"
        if len(candidate) <= 64:
            return candidate
        return f"s{step_index:02d}_n{sha256_digest({'source': source_key})[:16]}"

    @staticmethod
    def _target_leaves(
        source: DraftPlan,
        mapping: Mapping[str, str],
    ) -> tuple[str, ...]:
        business = tuple(
            node
            for node in source.nodes
            if node.kind not in {DraftNodeKind.FINAL_ACCEPTANCE, DraftNodeKind.DELIVERY}
        )
        depended_on = {
            dependency
            for node in business
            for dependency in node.depends_on
            if dependency in mapping
        }
        return tuple(
            mapping[node.local_key] for node in business if node.local_key not in depended_on
        )
