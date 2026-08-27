"""Fail-closed node authority binding for a sealed model-planner Draft."""

from __future__ import annotations

import json
from typing import Literal

from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.application.capability_executor_registry import (
    CapabilityExecutorRegistry,
    CapabilityExecutorRegistryError,
)
from deskpilot.application.capability_input_binding_catalog import (
    CapabilityInputBindingError,
    canonicalize_capability_parameter,
)
from deskpilot.application.model_planner_composer import RevalidatedOfferStep
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog
from deskpilot.application.task_loop_agent_adapter_registry import (
    TaskLoopAgentAdapterError,
    TaskLoopAgentAdapterRegistry,
)
from deskpilot.application.turn_planner_runtime import RevalidatedDeferredPlan
from deskpilot.application.workspace_coding_change_runtime import (
    WorkspaceCodingWriteActivationBundle,
)
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingReaderActivationBundle,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import AgentToolGrant, BoundAgentRef
from deskpilot.domain.task_loop import (
    ModelPlannerDraft,
    ModelPlannerNodeMapping,
    ModelPlannerStepBinding,
)
from deskpilot.domain.task_loop_execution import (
    EffectiveNodeAuthority,
    ModelPlannerNodeBinding,
    RuntimeEligibilityProof,
)
from deskpilot.domain.task_plans import (
    CapabilityRef,
    DraftNodeKind,
    ExecutablePlanNode,
    PlanNodeBudget,
    PrivacyPolicy,
    TaskContract,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import TurnPlanningRecipeRef
from deskpilot.domain.workspace_coding_changes import WorkspaceCodingWriteNodeProof
from deskpilot.domain.workspace_coding_explorations import WorkspaceCodingReaderNodeProof

_CLASSIFICATION_ORDER = {"public": 0, "internal": 1, "sensitive": 2}
_RISK_ORDER = {
    ToolRiskLevel.R0: 0,
    ToolRiskLevel.R1: 1,
    ToolRiskLevel.R2: 2,
    ToolRiskLevel.R3: 3,
    ToolRiskLevel.R4: 4,
}


class ModelPlannerNodeBindingError(RuntimeError):
    code = "MODEL_PLANNER_NODE_BINDING_REJECTED"


class ModelPlannerNodeProofRejectedError(ModelPlannerNodeBindingError):
    code = "MODEL_PLANNER_NODE_PROOF_REJECTED"


class ModelPlannerNodeAuthorityRejectedError(ModelPlannerNodeBindingError):
    code = "MODEL_PLANNER_NODE_AUTHORITY_REJECTED"


class ModelPlannerNodeRuntimeIneligibleError(ModelPlannerNodeBindingError):
    code = "MODEL_PLANNER_NODE_RUNTIME_INELIGIBLE"


class ModelPlannerNodeBinder:
    """Bind every runnable composite node to one revalidated source Offer step."""

    def __init__(
        self,
        agents: AgentRegistry,
        capability_executors: CapabilityExecutorRegistry,
        agent_adapters: TaskLoopAgentAdapterRegistry,
    ) -> None:
        self._agents = agents
        self._capability_executors = capability_executors
        self._agent_adapters = agent_adapters

    def bind(
        self,
        draft: ModelPlannerDraft,
        steps: tuple[ModelPlannerStepBinding, ...],
        revalidated: RevalidatedDeferredPlan,
    ) -> tuple[ModelPlannerNodeBinding, ...]:
        """Recalculate source lineage, least authority and runtime eligibility.

        The returned objects contain no new authority.  They prove that each
        runnable composite node is no broader than both its composite Contract
        and its exact source-step Contract.  Server control nodes are not
        dispatchable and therefore deliberately receive no node binding.
        """

        if draft.source.task_id != revalidated.planning.task_id:
            raise ModelPlannerNodeProofRejectedError("Revalidated proposal crosses the sealed Task")
        # Task-loop planning admits either the historical 2-8 Offer composition
        # or one server-owned planner-only Offer whose recipe expands to a DAG.
        # ``revalidate_task_loop_plan`` is the authority boundary that prevents
        # legacy direct-execution single-step Offers from reaching this binder.
        if len(steps) != len(revalidated.steps) or not 1 <= len(steps) <= 8:
            raise ModelPlannerNodeProofRejectedError(
                "Sealed and revalidated step sets do not match"
            )
        if draft.step_count != len(steps) or tuple(item.ref for item in steps) != draft.steps:
            raise ModelPlannerNodeProofRejectedError("Model Planner Draft step references changed")

        composite_nodes = {item.node_id: item for item in draft.expected_plan.nodes}
        bindings: list[ModelPlannerNodeBinding] = []
        seen_composite_nodes: set[str] = set()
        for persisted, current in zip(steps, revalidated.steps, strict=True):
            self._validate_step(draft, persisted, current)
            bound_input_manifest = self._canonical_parameters(persisted, current)
            source_contract = current.route.contract
            source_nodes = {item.node_id: item for item in current.offer.expected_plan.nodes}
            for mapping in persisted.node_mappings:
                source_node = source_nodes.get(mapping.source_node_id)
                composite_node = composite_nodes.get(mapping.composite_node_id)
                if source_node is None or composite_node is None:
                    raise ModelPlannerNodeProofRejectedError(
                        "Node mapping no longer resolves in both sealed Plans"
                    )
                if (
                    mapping.source_local_key != source_node.local_key
                    or mapping.source_node_spec_digest != source_node.node_spec_digest
                    or mapping.composite_local_key != composite_node.local_key
                    or mapping.composite_node_spec_digest != composite_node.node_spec_digest
                ):
                    raise ModelPlannerNodeProofRejectedError(
                        "Node mapping columns changed from the sealed Plans"
                    )
                if composite_node.node_id in seen_composite_nodes:
                    raise ModelPlannerNodeProofRejectedError(
                        "Composite node is bound by more than one source step"
                    )
                seen_composite_nodes.add(composite_node.node_id)
                authority = self._effective_authority(
                    composite_contract=draft.task_contract,
                    source_contract=source_contract,
                    composite_node=composite_node,
                    source_node=source_node,
                )
                eligibility = self._runtime_eligibility(
                    composite_node=composite_node,
                    source_node=source_node,
                    route_id=current.route.route_id,
                )
                bindings.append(
                    ModelPlannerNodeBinding.build(
                        task_id=draft.source.task_id,
                        user_message_id=draft.source.user_message_id,
                        draft_id=draft.draft_id,
                        step_binding_id=persisted.step_binding_id,
                        step_binding_digest=persisted.step_binding_digest,
                        step_ordinal=persisted.ordinal,
                        offer_id=persisted.offer.offer_id,
                        offer_key=persisted.offer.offer_key,
                        offer_digest=persisted.offer.offer_digest,
                        recipe=persisted.recipe,
                        policy_snapshot_digest=persisted.policy_snapshot_digest,
                        source_contract_digest=source_contract.digest,
                        source_plan_id=persisted.source_plan_id,
                        source_plan_manifest_digest=(persisted.source_plan_manifest_digest),
                        source_node_id=source_node.node_id,
                        source_node_spec_digest=source_node.node_spec_digest,
                        composite_contract_digest=draft.task_contract_digest,
                        composite_plan_id=draft.expected_plan.plan_id,
                        composite_plan_manifest_digest=(draft.expected_plan_manifest_digest),
                        composite_node_id=composite_node.node_id,
                        composite_node_spec_digest=composite_node.node_spec_digest,
                        mapping=mapping,
                        parameter_bindings=persisted.parameter_bindings,
                        parameter_bindings_digest=(persisted.parameter_bindings_digest),
                        bound_input_manifest=bound_input_manifest,
                        bound_input_digest=sha256_digest({"parameters": bound_input_manifest}),
                        effective_authority=authority,
                        runtime_eligibility=eligibility,
                    )
                )

        runnable = {
            item.node_id
            for item in draft.expected_plan.nodes
            if item.kind in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY}
        }
        if runnable != seen_composite_nodes:
            raise ModelPlannerNodeProofRejectedError(
                "Not every runnable composite node has one exact source-step binding"
            )
        return tuple(sorted(bindings, key=lambda item: item.composite_node_id))

    def bind_confirmed_readers(
        self,
        bundle: WorkspaceCodingReaderActivationBundle,
    ) -> tuple[ModelPlannerNodeBinding, ...]:
        """Bind exact confirmed Reader nodes without inventing a model Offer."""

        snapshot = bundle.snapshot
        proposal = bundle.proposal
        confirmed = bundle.binding
        if (
            confirmed.proposal_id != proposal.proposal_id
            or confirmed.proposal_digest != proposal.proposal_digest
            or proposal.snapshot_id != snapshot.snapshot_id
            or proposal.snapshot_digest != snapshot.snapshot_digest
        ):
            raise ModelPlannerNodeProofRejectedError(
                "Confirmed Reader activation crossed its exploration lineage"
            )
        files = {item.relative_path: item for item in snapshot.files}
        nodes = {item.node_id: item for item in confirmed.expected_plan.nodes}
        bindings: list[ModelPlannerNodeBinding] = []
        for mapping in confirmed.mappings:
            node = nodes.get(mapping.plan_node_id)
            file_proof = files.get(mapping.relative_path)
            if (
                node is None
                or file_proof is None
                or file_proof.proof_digest != mapping.source_file_proof_digest
                or node.local_key != mapping.plan_local_key
                or node.node_spec_digest != mapping.plan_node_spec_digest
                or node.kind is not DraftNodeKind.AGENT
                or node.bound_agent is None
                or node.bound_agent.agent_id != "builtin.workspace_reader"
                or node.capability is None
                or node.capability.capability_id != "workspace.file.read.v1"
            ):
                raise ModelPlannerNodeProofRejectedError(
                    "Confirmed Reader mapping no longer resolves exactly"
                )
            try:
                self._agents.resolve_exact(
                    node.bound_agent.agent_id,
                    node.bound_agent.version,
                    contract_digest=node.bound_agent.contract_digest,
                    prompt_package_digest=node.bound_agent.prompt_package_digest,
                )
                adapter = self._agent_adapters.resolve(
                    route_id="workspace_confirmed_file_set",
                    source_local_key=node.local_key,
                    bound_agent=node.bound_agent,
                    capability=node.capability,
                )
            except (AgentRegistryError, TaskLoopAgentAdapterError) as error:
                raise ModelPlannerNodeRuntimeIneligibleError(
                    "Confirmed Reader Agent runtime is no longer eligible"
                ) from error
            reader_proof = WorkspaceCodingReaderNodeProof.build(
                file_set_binding_id=confirmed.binding_id,
                file_set_binding_digest=confirmed.binding_digest,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
                snapshot_id=snapshot.snapshot_id,
                snapshot_digest=snapshot.snapshot_digest,
                catalog_digest=snapshot.catalog_digest,
                project_path=snapshot.project_path,
                ecosystem=snapshot.ecosystem,
                successor_task_id=confirmed.successor_task_id,
                confirmation_message_id=confirmed.confirmation_message_id,
                confirmation_message_digest=confirmed.confirmation_message_digest,
                ordinal=mapping.ordinal,
                relative_path=mapping.relative_path,
                workspace_relative_path=(
                    mapping.relative_path
                    if snapshot.project_path == "."
                    else f"{snapshot.project_path.rstrip('/')}/{mapping.relative_path}"
                ),
                source_file_proof_digest=mapping.source_file_proof_digest,
                plan_id=confirmed.expected_plan.plan_id,
                plan_manifest_digest=confirmed.expected_plan_manifest_digest,
                plan_node_id=node.node_id,
                plan_local_key=node.local_key,
                plan_node_spec_digest=node.node_spec_digest,
                reader_agent=node.bound_agent,
                capability=node.capability,
            )
            node_mapping = ModelPlannerNodeMapping.build(
                source_node_id=node.node_id,
                source_local_key=node.local_key,
                source_node_spec_digest=node.node_spec_digest,
                composite_node_id=node.node_id,
                composite_local_key=node.local_key,
                composite_node_spec_digest=node.node_spec_digest,
            )
            authority = EffectiveNodeAuthority.build(
                authority_rule="confirmed_file_set_exact_reader",
                composite_contract_digest=confirmed.task_contract_digest,
                source_contract_digest=confirmed.task_contract_digest,
                node_kind=node.kind,
                bound_agent=node.bound_agent,
                bound_tool=node.bound_tool,
                capability=node.capability,
                resource_scopes=confirmed.task_contract.resource_scopes,
                privacy_classification=(confirmed.task_contract.privacy_policy.classification),
                allowed_provider_locations=(
                    confirmed.task_contract.privacy_policy.allowed_provider_locations
                ),
                allowed_privacy_modes=(
                    confirmed.task_contract.privacy_policy.allowed_privacy_modes
                ),
                external_egress_allowed=(
                    confirmed.task_contract.privacy_policy.external_egress_allowed
                ),
                max_risk_level=confirmed.task_contract.max_risk_level,
                budget=node.budget,
            )
            eligibility = RuntimeEligibilityProof.build(
                runtime_kind="agent",
                bound_agent=node.bound_agent,
                capability=None,
                executor_id=None,
                executor_manifest_digest=None,
                agent_adapter_id=adapter.adapter_id,
                agent_adapter_manifest_digest=adapter.manifest_digest,
                registry_snapshot_digest=sha256_digest(
                    {
                        "agent_registry_snapshot_digest": (self._agents.snapshot().snapshot_digest),
                        "agent_adapter_registry_snapshot_digest": (
                            self._agent_adapters.snapshot_digest
                        ),
                    }
                ),
            )
            bound_input = {
                "path": reader_proof.workspace_relative_path,
                "project_path": snapshot.project_path,
                "source_file_proof_digest": mapping.source_file_proof_digest,
                "workspace_reader_node_proof_digest": reader_proof.proof_digest,
            }
            bindings.append(
                ModelPlannerNodeBinding.build(
                    source_kind="confirmed_file_set",
                    task_id=confirmed.successor_task_id,
                    user_message_id=confirmed.confirmation_message_id,
                    draft_id=None,
                    step_binding_id=None,
                    step_binding_digest=None,
                    step_ordinal=None,
                    offer_id=None,
                    offer_key=None,
                    offer_digest=None,
                    recipe=None,
                    policy_snapshot_digest=None,
                    source_contract_digest=confirmed.task_contract_digest,
                    source_plan_id=confirmed.expected_plan.plan_id,
                    source_plan_manifest_digest=(confirmed.expected_plan_manifest_digest),
                    source_node_id=node.node_id,
                    source_node_spec_digest=node.node_spec_digest,
                    composite_contract_digest=confirmed.task_contract_digest,
                    composite_plan_id=confirmed.expected_plan.plan_id,
                    composite_plan_manifest_digest=(confirmed.expected_plan_manifest_digest),
                    composite_node_id=node.node_id,
                    composite_node_spec_digest=node.node_spec_digest,
                    mapping=node_mapping,
                    parameter_bindings=(),
                    parameter_bindings_digest=sha256_digest({"parameter_bindings": []}),
                    bound_input_manifest=bound_input,
                    bound_input_digest=sha256_digest(
                        {"parameters": dict(sorted(bound_input.items()))}
                    ),
                    effective_authority=authority,
                    runtime_eligibility=eligibility,
                    workspace_reader_node_proof=reader_proof,
                )
            )
        if len(bindings) != len(confirmed.mappings):
            raise ModelPlannerNodeProofRejectedError("Confirmed Reader binding set is incomplete")
        return tuple(sorted(bindings, key=lambda item: item.composite_node_id))

    def bind_confirmed_write_plan(
        self,
        bundle: WorkspaceCodingWriteActivationBundle,
    ) -> tuple[ModelPlannerNodeBinding, ...]:
        """Bind a fresh-confirmed proposal to its existing coding-loop DAG."""

        reader = bundle.reader
        proposal = bundle.proposal
        run_binding = bundle.run_binding
        confirmed = bundle.binding
        if (
            confirmed.proposal_id != proposal.proposal_id
            or confirmed.proposal_digest != proposal.proposal_digest
            or proposal.run_binding_id != run_binding.binding_id
            or proposal.run_binding_digest != run_binding.binding_digest
            or run_binding.file_set_binding_id != reader.binding.binding_id
            or run_binding.file_set_binding_digest != reader.binding.binding_digest
            or confirmed.successor_task_id != confirmed.expected_plan.task_id
            or confirmed.task_contract.digest
            != confirmed.expected_plan.task_contract.digest
        ):
            raise ModelPlannerNodeProofRejectedError(
                "Confirmed write activation crossed its Reader or Proposal lineage"
            )
        expected_changes_json = json.dumps(
            [
                {
                    "path": (
                        change.relative_path
                        if reader.snapshot.project_path == "."
                        else f"{reader.snapshot.project_path.rstrip('/')}/{change.relative_path}"
                    ),
                    "old_text": change.old_text,
                    "new_text": change.new_text,
                }
                for change in proposal.decision.changes
            ],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if (
            confirmed.parameters.get("changes_json") != expected_changes_json
            or confirmed.parameters.get("project_path") != reader.snapshot.project_path
            or confirmed.parameters.get("test_kind") != reader.snapshot.ecosystem
        ):
            raise ModelPlannerNodeProofRejectedError(
                "Confirmed write parameters changed from the exact Proposal"
            )
        recipe = TurnPlanningRecipeRef(
            route_id=confirmed.route_id,
            route_version=confirmed.route_version,
            route_manifest_digest=confirmed.recipe_digest,
        )
        bindings: list[ModelPlannerNodeBinding] = []
        for node in confirmed.expected_plan.nodes:
            if node.kind not in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY}:
                continue
            if node.capability is None:
                raise ModelPlannerNodeProofRejectedError(
                    "Confirmed write runnable node lost its exact Capability"
                )
            mapping = ModelPlannerNodeMapping.build(
                source_node_id=node.node_id,
                source_local_key=node.local_key,
                source_node_spec_digest=node.node_spec_digest,
                composite_node_id=node.node_id,
                composite_local_key=node.local_key,
                composite_node_spec_digest=node.node_spec_digest,
            )
            authority = self._effective_authority(
                composite_contract=confirmed.task_contract,
                source_contract=confirmed.task_contract,
                composite_node=node,
                source_node=node,
                authority_rule="confirmed_change_proposal_exact_plan",
            )
            eligibility = self._runtime_eligibility(
                composite_node=node,
                source_node=node,
                route_id="workspace_coding_loop",
            )
            write_proof = WorkspaceCodingWriteNodeProof.build(
                write_plan_binding_id=confirmed.binding_id,
                write_plan_binding_digest=confirmed.binding_digest,
                proposal_id=proposal.proposal_id,
                proposal_digest=proposal.proposal_digest,
                proposal_decision_digest=proposal.decision_digest,
                file_set_binding_id=reader.binding.binding_id,
                file_set_binding_digest=reader.binding.binding_digest,
                snapshot_id=reader.snapshot.snapshot_id,
                snapshot_digest=reader.snapshot.snapshot_digest,
                catalog_digest=reader.snapshot.catalog_digest,
                project_path=reader.snapshot.project_path,
                ecosystem=reader.snapshot.ecosystem,
                successor_task_id=confirmed.successor_task_id,
                confirmation_message_id=confirmed.confirmation_message_id,
                confirmation_message_digest=confirmed.confirmation_message_digest,
                recipe_digest=confirmed.recipe_digest,
                parameter_binding_digest=confirmed.parameter_binding_digest,
                parameters=confirmed.parameters,
                plan_id=confirmed.expected_plan.plan_id,
                plan_manifest_digest=confirmed.expected_plan_manifest_digest,
                plan_node_id=node.node_id,
                plan_local_key=node.local_key,
                plan_node_spec_digest=node.node_spec_digest,
                node_kind=node.kind,
                bound_agent=node.bound_agent,
                bound_tool=node.bound_tool,
                capability=node.capability,
            )
            bindings.append(
                ModelPlannerNodeBinding.build(
                    source_kind="confirmed_change_proposal",
                    task_id=confirmed.successor_task_id,
                    user_message_id=confirmed.confirmation_message_id,
                    draft_id=None,
                    step_binding_id=None,
                    step_binding_digest=None,
                    step_ordinal=None,
                    offer_id=None,
                    offer_key=None,
                    offer_digest=None,
                    recipe=recipe,
                    policy_snapshot_digest=None,
                    source_contract_digest=confirmed.task_contract_digest,
                    source_plan_id=confirmed.expected_plan.plan_id,
                    source_plan_manifest_digest=confirmed.expected_plan_manifest_digest,
                    source_node_id=node.node_id,
                    source_node_spec_digest=node.node_spec_digest,
                    composite_contract_digest=confirmed.task_contract_digest,
                    composite_plan_id=confirmed.expected_plan.plan_id,
                    composite_plan_manifest_digest=confirmed.expected_plan_manifest_digest,
                    composite_node_id=node.node_id,
                    composite_node_spec_digest=node.node_spec_digest,
                    mapping=mapping,
                    parameter_bindings=(),
                    parameter_bindings_digest=sha256_digest({"parameter_bindings": []}),
                    bound_input_manifest=dict(sorted(confirmed.parameters.items())),
                    bound_input_digest=sha256_digest(
                        {"parameters": dict(sorted(confirmed.parameters.items()))}
                    ),
                    effective_authority=authority,
                    runtime_eligibility=eligibility,
                    workspace_coding_write_node_proof=write_proof,
                )
            )
        runnable_count = sum(
            item.kind in {DraftNodeKind.AGENT, DraftNodeKind.CAPABILITY}
            for item in confirmed.expected_plan.nodes
        )
        if not bindings or len(bindings) != runnable_count:
            raise ModelPlannerNodeProofRejectedError(
                "Confirmed write binding set is incomplete"
            )
        return tuple(sorted(bindings, key=lambda item: item.composite_node_id))

    @staticmethod
    def _validate_step(
        draft: ModelPlannerDraft,
        persisted: ModelPlannerStepBinding,
        current: RevalidatedOfferStep,
    ) -> None:
        # Avoid accepting a duck-typed or model-authored Offer at this authority
        # boundary; the revalidation runtime returns the exact domain type.
        from deskpilot.domain.turn_planning import TurnPlanningOffer

        offer = current.offer
        if not isinstance(offer, TurnPlanningOffer):
            raise ModelPlannerNodeProofRejectedError(
                "Revalidated step did not contain a trusted Offer"
            )
        if (
            persisted.source != draft.source
            or persisted.offer != offer.ref
            or persisted.recipe != offer.trusted_recipe
            or persisted.policy_snapshot_digest != offer.policy_snapshot_digest
            or persisted.source_plan_id != offer.expected_plan.plan_id
            or persisted.source_plan_manifest_digest != offer.expected_plan.plan_manifest_digest
            or persisted.source_plan_binding_snapshot_digest
            != offer.expected_plan.binding_snapshot_digest
            or persisted.budget != offer.budget
        ):
            raise ModelPlannerNodeProofRejectedError(
                "Sealed step Offer, recipe, policy, input, budget, or Plan changed"
            )

    @staticmethod
    def _canonical_parameters(
        persisted: ModelPlannerStepBinding,
        current: RevalidatedOfferStep,
    ) -> dict[str, str]:
        enum_names = {
            item.name
            for item in RouteRecipeCatalog.parameter_specs(current.route.route_id)
            if item.allowed_values
        }
        raw_bindings: dict[str, str] = {}
        for item in persisted.parameter_bindings:
            if item.parameter_name in raw_bindings:
                raise ModelPlannerNodeProofRejectedError(
                    "Sealed step repeats a parameter source proof"
                )
            raw_bindings[item.parameter_name] = item.value
        derived_names = (
            {"patch_paths_json"}
            if current.route.route_id == "workspace_dynamic_patch_test"
            else set()
        )
        if set(current.parameters) - (
            set(raw_bindings) | set(current.route.fixed_parameters) | derived_names
        ):
            raise ModelPlannerNodeProofRejectedError(
                "Revalidated step contains an unproven parameter"
            )
        try:
            canonical = {
                name: canonicalize_capability_parameter(
                    value,
                    enum_value=name in enum_names,
                )
                for name, value in current.parameters.items()
            }
            for name, raw_value in raw_bindings.items():
                current_value = current.parameters.get(name)
                if (
                    current_value is None
                    or canonicalize_capability_parameter(
                        raw_value,
                        enum_value=name in enum_names,
                    )
                    != canonical[name]
                ):
                    raise ModelPlannerNodeProofRejectedError(
                        "Revalidated parameter differs from its persisted message span"
                    )
        except CapabilityInputBindingError as error:
            raise ModelPlannerNodeProofRejectedError(
                "Revalidated parameter cannot be canonicalized"
            ) from error
        return dict(sorted(canonical.items()))

    def _runtime_eligibility(
        self,
        *,
        composite_node: ExecutablePlanNode,
        source_node: ExecutablePlanNode,
        route_id: str,
    ) -> RuntimeEligibilityProof:
        if not composite_node.runtime_enabled or not source_node.runtime_enabled:
            raise ModelPlannerNodeRuntimeIneligibleError(
                "Source or composite node is no longer runtime enabled"
            )
        try:
            if composite_node.kind is DraftNodeKind.AGENT:
                bound = composite_node.bound_agent
                if bound is None or source_node.bound_agent != bound:
                    raise ModelPlannerNodeRuntimeIneligibleError(
                        "Composite Agent binding differs from its source step"
                    )
                self._agents.resolve_exact(
                    bound.agent_id,
                    bound.version,
                    contract_digest=bound.contract_digest,
                    prompt_package_digest=bound.prompt_package_digest,
                )
                capability = composite_node.capability
                if capability is None or source_node.capability != capability:
                    raise ModelPlannerNodeRuntimeIneligibleError(
                        "Composite Agent capability differs from its source step"
                    )
                adapter = self._agent_adapters.resolve(
                    route_id=route_id,
                    source_local_key=source_node.local_key,
                    bound_agent=bound,
                    capability=capability,
                )
                return RuntimeEligibilityProof.build(
                    runtime_kind="agent",
                    bound_agent=bound,
                    capability=None,
                    executor_id=None,
                    executor_manifest_digest=None,
                    agent_adapter_id=adapter.adapter_id,
                    agent_adapter_manifest_digest=adapter.manifest_digest,
                    registry_snapshot_digest=sha256_digest(
                        {
                            "agent_registry_snapshot_digest": (
                                self._agents.snapshot().snapshot_digest
                            ),
                            "agent_adapter_registry_snapshot_digest": (
                                self._agent_adapters.snapshot_digest
                            ),
                        }
                    ),
                )
            if composite_node.kind is DraftNodeKind.CAPABILITY:
                capability = composite_node.capability
                if capability is None or source_node.capability != capability:
                    raise ModelPlannerNodeRuntimeIneligibleError(
                        "Composite capability binding differs from its source step"
                    )
                registration = self._capability_executors.resolve(capability)
                if (
                    not registration.manifest.runtime_enabled
                    or composite_node.kind not in registration.manifest.node_kinds
                ):
                    raise ModelPlannerNodeRuntimeIneligibleError(
                        "Exact capability executor is not eligible for this node"
                    )
                registry_digest = sha256_digest(
                    {
                        "manifests": [
                            item.model_dump(mode="json")
                            for item in self._capability_executors.manifests()
                        ]
                    }
                )
                return RuntimeEligibilityProof.build(
                    runtime_kind="capability_executor",
                    bound_agent=None,
                    capability=capability,
                    executor_id=registration.manifest.executor_id,
                    executor_manifest_digest=registration.manifest.manifest_digest,
                    registry_snapshot_digest=registry_digest,
                )
        except (
            AgentRegistryError,
            CapabilityExecutorRegistryError,
            TaskLoopAgentAdapterError,
        ) as error:
            raise ModelPlannerNodeRuntimeIneligibleError(
                "Exact Agent or capability executor is not currently eligible"
            ) from error
        raise ModelPlannerNodeRuntimeIneligibleError(
            "Control node cannot receive runtime eligibility"
        )

    @classmethod
    def _effective_authority(
        cls,
        *,
        composite_contract: TaskContract,
        source_contract: TaskContract,
        composite_node: ExecutablePlanNode,
        source_node: ExecutablePlanNode,
        authority_rule: Literal[
            "composite_intersection_source_step",
            "confirmed_change_proposal_exact_plan",
        ] = "composite_intersection_source_step",
    ) -> EffectiveNodeAuthority:
        if composite_node.kind != source_node.kind or composite_node.kind not in {
            DraftNodeKind.AGENT,
            DraftNodeKind.CAPABILITY,
        }:
            raise ModelPlannerNodeAuthorityRejectedError(
                "Composite node kind exceeds its source-step node"
            )
        cls._require_exact_or_none(
            composite_node.bound_agent,
            source_node.bound_agent,
            "Agent",
        )
        cls._require_exact_or_none(
            composite_node.bound_tool,
            source_node.bound_tool,
            "Tool",
        )
        cls._require_exact_or_none(
            composite_node.capability,
            source_node.capability,
            "capability",
        )
        privacy = cls._intersect_privacy(
            composite_contract.privacy_policy,
            source_contract.privacy_policy,
        )
        resource_scopes = tuple(
            item
            for item in composite_contract.resource_scopes
            if item in set(source_contract.resource_scopes)
        )
        max_risk = min(
            (composite_contract.max_risk_level, source_contract.max_risk_level),
            key=_RISK_ORDER.__getitem__,
        )
        return EffectiveNodeAuthority.build(
            authority_rule=authority_rule,
            composite_contract_digest=composite_contract.digest,
            source_contract_digest=source_contract.digest,
            node_kind=composite_node.kind,
            bound_agent=composite_node.bound_agent,
            bound_tool=composite_node.bound_tool,
            capability=composite_node.capability,
            resource_scopes=resource_scopes,
            privacy_classification=privacy.classification,
            allowed_provider_locations=privacy.allowed_provider_locations,
            allowed_privacy_modes=privacy.allowed_privacy_modes,
            external_egress_allowed=privacy.external_egress_allowed,
            max_risk_level=max_risk,
            budget=cls._intersect_budget(composite_node.budget, source_node.budget),
        )

    @staticmethod
    def _require_exact_or_none(
        composite: BoundAgentRef | AgentToolGrant | CapabilityRef | None,
        source: BoundAgentRef | AgentToolGrant | CapabilityRef | None,
        label: str,
    ) -> None:
        if composite != source:
            raise ModelPlannerNodeAuthorityRejectedError(
                f"Composite {label} authority differs from its source step"
            )

    @staticmethod
    def _intersect_budget(
        composite: PlanNodeBudget,
        source: PlanNodeBudget,
    ) -> PlanNodeBudget:
        return PlanNodeBudget(
            **{
                field: min(getattr(composite, field), getattr(source, field))
                for field in (
                    "model_calls",
                    "tool_calls",
                    "input_tokens",
                    "output_tokens",
                    "wall_seconds",
                    "retries",
                    "cost_micros",
                    "handoffs",
                )
            }
        )

    @staticmethod
    def _intersect_privacy(
        composite: PrivacyPolicy,
        source: PrivacyPolicy,
    ) -> PrivacyPolicy:
        locations = tuple(
            item
            for item in composite.allowed_provider_locations
            if item in set(source.allowed_provider_locations)
        )
        modes = tuple(
            item
            for item in composite.allowed_privacy_modes
            if item in set(source.allowed_privacy_modes)
        )
        if not locations or not modes:
            raise ModelPlannerNodeAuthorityRejectedError(
                "Composite and source-step privacy authority do not intersect"
            )
        classification = max(
            (composite.classification, source.classification),
            key=_CLASSIFICATION_ORDER.__getitem__,
        )
        return PrivacyPolicy(
            classification=classification,
            allowed_provider_locations=locations,
            allowed_privacy_modes=modes,
            external_egress_allowed=(
                composite.external_egress_allowed and source.external_egress_allowed
            ),
        )
