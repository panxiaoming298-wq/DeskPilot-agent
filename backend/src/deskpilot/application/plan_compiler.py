"""Pure deterministic compiler from an untrusted DraftPlan to a sealed plan."""

from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.application.capability_catalog import CapabilityCatalog, CapabilityCatalogError
from deskpilot.application.plan_binder import AgentPlanBinder, AgentPlanBindingError
from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import (
    AgentPlanBudget,
    AgentPlanDraftStep,
    AgentToolGrant,
    BoundAgentRef,
)
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.task_plans import (
    AcceptanceCoverage,
    AcceptanceCriterion,
    AcceptanceKind,
    BrowserVerifyContract,
    CapabilityRef,
    DraftNodeKind,
    DraftPlan,
    DraftPlanNode,
    ExecutablePlan,
    ExecutablePlanNode,
    OutputContract,
    PlanNodeBudget,
    PlanProducer,
    PrivacyPolicy,
    ResearchContract,
    TaskBudget,
    TaskContract,
    TaskContractRef,
    TaskWorkspaceContract,
    VerificationProfile,
    VerificationRequirement,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel

_RISK = {"R0": 0, "R1": 1, "R2": 2, "R3": 3, "R4": 4}
_VERIFICATION_DIGESTS = {
    profile: sha256_digest({"profile": profile.value, "version": 1})
    for profile in VerificationProfile
}
_PROFILE_REQUIREMENT = {
    VerificationProfile.DETERMINISTIC: VerificationRequirement.DETERMINISTIC,
    VerificationProfile.CITATION: VerificationRequirement.CITATION,
    VerificationProfile.ARTIFACT: VerificationRequirement.ARTIFACT,
    VerificationProfile.BROWSER: VerificationRequirement.BROWSER,
    VerificationProfile.SEMANTIC: VerificationRequirement.SEMANTIC,
}


class PlanCompilerError(RuntimeError):
    code = "PLAN_COMPILATION_REJECTED"


class PlanBindingUnknownError(PlanCompilerError):
    code = "PLAN_BINDING_UNKNOWN"


class PlanCapabilityMismatchError(PlanCompilerError):
    code = "PLAN_CAPABILITY_MISMATCH"


class PlanPrivacyConflictError(PlanCompilerError):
    code = "PLAN_PRIVACY_CONFLICT"


class PlanBudgetExceededError(PlanCompilerError):
    code = "PLAN_BUDGET_EXCEEDED"


class PlanAcceptanceUncoveredError(PlanCompilerError):
    code = "PLAN_ACCEPTANCE_UNCOVERED"


class PlanManifestDriftError(PlanCompilerError):
    code = "PLAN_MANIFEST_DRIFT"


class PlanCompiler:
    def __init__(
        self,
        agents: AgentRegistry,
        tools: ToolRegistry,
        capabilities: CapabilityCatalog,
    ) -> None:
        self._agents = agents
        self._tools = tools
        self._capabilities = capabilities
        self._agent_binder = AgentPlanBinder(agents)

    def compile(
        self,
        contract: TaskContract,
        draft: DraftPlan,
        *,
        generation: int,
    ) -> ExecutablePlan:
        if draft.task_id != contract.task_id or draft.contract_version != contract.version:
            raise PlanManifestDriftError("Draft Plan does not match the Task Contract")
        if len(draft.nodes) > contract.budget.max_plan_nodes:
            raise PlanBudgetExceededError("Draft Plan exceeds the node budget")
        capability_refs = self._validate_contract_capabilities(contract)
        self._validate_total_budget(contract, draft)
        nodes = self._bind_nodes(contract, draft, capability_refs, generation)
        self._validate_handoffs(nodes)
        coverage = self._coverage(contract, nodes)
        used_capabilities = sorted(
            (node.capability.model_dump(mode="json") for node in nodes if node.capability),
            key=lambda item: (str(item["capability_id"]), str(item["version"])),
        )
        used_agents = sorted(
            (node.bound_agent.model_dump(mode="json") for node in nodes if node.bound_agent),
            key=lambda item: (str(item["agent_id"]), str(item["version"])),
        )
        binding_snapshot_digest = sha256_digest(
            {"agents": used_agents, "capabilities": used_capabilities}
        )
        plan_id = f"epl_{sha256_digest({'task_id': contract.task_id, 'generation': generation})}"
        material = {
            "schema_version": "deskpilot.executable-plan.v1",
            "canonicalization_version": 1,
            "compiler_version": "deskpilot.plan-compiler.v1",
            "plan_id": plan_id,
            "task_id": contract.task_id,
            "plan_generation": generation,
            "task_contract": TaskContractRef(
                contract_id=contract.contract_id,
                version=contract.version,
                digest=contract.digest,
            ),
            "producer": draft.producer,
            "nodes": nodes,
            "acceptance_coverage": coverage,
            "runtime_enabled": all(node.runtime_enabled for node in nodes),
            "binding_snapshot_digest": binding_snapshot_digest,
        }
        return ExecutablePlan.model_validate(
            {**material, "plan_manifest_digest": sha256_digest(material)}
        )

    def validate_manifest(self, plan: ExecutablePlan) -> None:
        try:
            used_agents: list[dict[str, object]] = []
            used_capabilities: list[dict[str, object]] = []
            for node in plan.nodes:
                if node.bound_agent is not None:
                    registration = self._agents.resolve_exact(
                        node.bound_agent.agent_id,
                        node.bound_agent.version,
                        contract_digest=node.bound_agent.contract_digest,
                        prompt_package_digest=node.bound_agent.prompt_package_digest,
                    )
                    if node.bound_tool is not None and (
                        node.bound_tool not in registration.contract.tool_policy.grants
                    ):
                        raise PlanManifestDriftError("Bound Tool no longer matches the Agent")
                    used_agents.append(node.bound_agent.model_dump(mode="json"))
                if node.capability is not None:
                    pack = self._capabilities.resolve(
                        node.capability.capability_id,
                        node.capability.version,
                        node.capability.digest,
                    )
                    if node.runtime_enabled is not pack.runtime_enabled:
                        raise PlanManifestDriftError("Capability runtime status changed")
                    used_capabilities.append(node.capability.model_dump(mode="json"))
                if (
                    node.verification_profile_digest
                    != _VERIFICATION_DIGESTS[node.verification_profile]
                ):
                    raise PlanManifestDriftError("Verification profile digest changed")
            used_agents.sort(key=lambda item: (str(item["agent_id"]), str(item["version"])))
            used_capabilities.sort(
                key=lambda item: (
                    str(item["capability_id"]),
                    str(item["version"]),
                )
            )
            if plan.binding_snapshot_digest != sha256_digest(
                {"agents": used_agents, "capabilities": used_capabilities}
            ):
                raise PlanManifestDriftError("Executable Plan binding snapshot changed")
            if plan.runtime_enabled is not all(node.runtime_enabled for node in plan.nodes):
                raise PlanManifestDriftError("Executable Plan runtime status changed")
        except (AgentRegistryError, CapabilityCatalogError) as error:
            raise PlanManifestDriftError("Executable Plan binding changed") from error

    def _validate_contract_capabilities(
        self, contract: TaskContract
    ) -> dict[str, CapabilityRef]:
        result: dict[str, CapabilityRef] = {}
        for reference in contract.capabilities:
            if reference.capability_id in result:
                raise PlanCapabilityMismatchError("Capability selector is ambiguous")
            try:
                pack = self._capabilities.resolve(*reference.key, reference.digest)
            except CapabilityCatalogError as error:
                raise PlanBindingUnknownError("Task capability is not registered") from error
            if _RISK[pack.max_risk_level.value] > _RISK[contract.max_risk_level.value]:
                raise PlanCapabilityMismatchError("Capability exceeds Task risk posture")
            if pack.external_egress and not contract.privacy_policy.external_egress_allowed:
                raise PlanPrivacyConflictError("Capability requires forbidden external egress")
            result[reference.capability_id] = reference
        return result

    def _bind_nodes(
        self,
        contract: TaskContract,
        draft: DraftPlan,
        capability_refs: dict[str, CapabilityRef],
        generation: int,
    ) -> tuple[ExecutablePlanNode, ...]:
        node_ids = {}
        for node in draft.nodes:
            identity = {
                "task_id": contract.task_id,
                "generation": generation,
                "local_key": node.local_key,
            }
            node_ids[node.local_key] = f"pnd_{sha256_digest(identity)}"
        bound: list[ExecutablePlanNode] = []
        for draft_node in sorted(draft.nodes, key=lambda item: item.local_key):
            bound_agent = None
            bound_tool = None
            capability = None
            runtime_enabled = True
            if draft_node.kind is DraftNodeKind.AGENT:
                bound_agent, bound_tool = self._bind_agent(contract, draft_node)
                selected = [
                    capability_refs[item]
                    for item in draft_node.capability_requirements
                    if item in capability_refs
                ]
                if len(selected) > 1:
                    raise PlanCapabilityMismatchError(
                        "An Agent node can bind only one Task capability"
                    )
                if selected:
                    capability = selected[0]
                    pack = self._capabilities.resolve(*capability.key, capability.digest)
                    runtime_enabled = pack.runtime_enabled
            elif draft_node.kind is DraftNodeKind.CAPABILITY:
                if draft_node.capability_selector is None:
                    raise PlanCapabilityMismatchError("Capability selector is missing")
                capability = capability_refs.get(draft_node.capability_selector)
                if capability is None:
                    raise PlanCapabilityMismatchError("Draft capability is outside Task Contract")
                pack = self._capabilities.resolve(*capability.key, capability.digest)
                runtime_enabled = pack.runtime_enabled
            material = {
                "node_id": node_ids[draft_node.local_key],
                "local_key": draft_node.local_key,
                "kind": draft_node.kind,
                "objective": draft_node.objective,
                "bound_agent": bound_agent,
                "bound_tool": bound_tool,
                "capability": capability,
                "capability_requirements": tuple(sorted(draft_node.capability_requirements)),
                "depends_on": tuple(sorted(node_ids[item] for item in draft_node.depends_on)),
                "handoff_parent_node_id": (
                    node_ids[draft_node.handoff_parent]
                    if draft_node.handoff_parent is not None
                    else None
                ),
                "acceptance_refs": tuple(sorted(draft_node.acceptance_refs)),
                "verification_profile": draft_node.verification_profile,
                "verification_profile_digest": _VERIFICATION_DIGESTS[
                    draft_node.verification_profile
                ],
                "budget": draft_node.budget,
                "runtime_enabled": runtime_enabled,
            }
            bound.append(
                ExecutablePlanNode.model_validate(
                    {**material, "node_spec_digest": sha256_digest(material)}
                )
            )
        return tuple(bound)

    def _bind_agent(
        self, contract: TaskContract, node: DraftPlanNode
    ) -> tuple[BoundAgentRef, AgentToolGrant | None]:
        if node.agent_selector is None:
            raise PlanCapabilityMismatchError("Agent selector is missing")
        if node.budget.model_calls < 1:
            raise PlanBudgetExceededError("Agent node requires a model-call budget")
        try:
            bound = self._agent_binder.bind(
                AgentPlanDraftStep(
                    step_id=node.local_key,
                    agent_selector=node.agent_selector,
                    tool_name=node.tool_name,
                    tool_version=node.tool_version,
                    budget=AgentPlanBudget(
                        model_calls=node.budget.model_calls,
                        tool_calls=node.budget.tool_calls,
                        input_tokens=node.budget.input_tokens,
                        output_tokens=node.budget.output_tokens,
                        wall_seconds=node.budget.wall_seconds,
                        retries=node.budget.retries,
                        cost_micros=node.budget.cost_micros,
                        handoffs=node.budget.handoffs,
                    ),
                )
            )
        except (AgentPlanBindingError, AgentRegistryError) as error:
            raise PlanBindingUnknownError("Agent binding was rejected") from error
        registration = self._agents.resolve_exact(
            bound.agent.agent_id,
            bound.agent.version,
            contract_digest=bound.agent.contract_digest,
            prompt_package_digest=bound.agent.prompt_package_digest,
        )
        if not set(node.capability_requirements).issubset(registration.contract.provides):
            raise PlanCapabilityMismatchError("Agent does not provide required capabilities")
        if bound.tool is not None:
            tool = self._tools.resolve(bound.tool.name, bound.tool.version).contract
            if _RISK[tool.risk_level.value] > _RISK[contract.max_risk_level.value]:
                raise PlanCapabilityMismatchError("Tool exceeds Task risk posture")
        return bound.agent, bound.tool

    def _validate_handoffs(self, nodes: tuple[ExecutablePlanNode, ...]) -> None:
        by_id = {node.node_id: node for node in nodes}
        outgoing_counts: dict[str, int] = {}
        for node in nodes:
            if node.handoff_parent_node_id is None:
                continue
            parent = by_id[node.handoff_parent_node_id]
            outgoing_counts[parent.node_id] = outgoing_counts.get(parent.node_id, 0) + 1
            if outgoing_counts[parent.node_id] > parent.budget.handoffs:
                raise PlanBudgetExceededError("Agent handoffs exceed the source node budget")
            if node.bound_agent is None or parent.bound_agent is None:
                raise PlanCapabilityMismatchError("Handoff endpoints must be Agent nodes")
            source = self._agents.resolve_exact(
                parent.bound_agent.agent_id,
                parent.bound_agent.version,
            )
            target = self._agents.resolve_exact(node.bound_agent.agent_id, node.bound_agent.version)
            target_key = (target.contract.agent_id, target.contract.version)
            source_key = (source.contract.agent_id, source.contract.version)
            outgoing = {item.key for item in source.contract.handoff_policy.may_delegate_to}
            incoming = {item.key for item in target.contract.handoff_policy.may_receive_from}
            if target_key not in outgoing or source_key not in incoming:
                raise PlanCapabilityMismatchError("Agent handoff is not allowed")

    @staticmethod
    def _validate_total_budget(contract: TaskContract, draft: DraftPlan) -> None:
        requested = {
            "max_model_calls": sum(node.budget.model_calls for node in draft.nodes),
            "max_tool_calls": sum(node.budget.tool_calls for node in draft.nodes),
            "max_input_tokens": sum(node.budget.input_tokens for node in draft.nodes),
            "max_output_tokens": sum(node.budget.output_tokens for node in draft.nodes),
            "max_wall_seconds": sum(node.budget.wall_seconds for node in draft.nodes),
            "max_retries": sum(node.budget.retries for node in draft.nodes),
            "max_cost_micros": sum(node.budget.cost_micros for node in draft.nodes),
            "max_handoffs": sum(node.budget.handoffs for node in draft.nodes),
        }
        limits = contract.budget.model_dump()
        if any(requested[key] > limits[key] for key in requested):
            raise PlanBudgetExceededError("Draft nodes exceed the Task budget")

    @staticmethod
    def _coverage(
        contract: TaskContract, nodes: tuple[ExecutablePlanNode, ...]
    ) -> tuple[AcceptanceCoverage, ...]:
        criteria = {item.criterion_id: item for item in contract.acceptance_criteria}
        covered: dict[str, list[str]] = {key: [] for key in criteria}
        for node in nodes:
            for reference in node.acceptance_refs:
                criterion = criteria.get(reference)
                if criterion is None:
                    raise PlanAcceptanceUncoveredError("Plan references an unknown criterion")
                if node.kind is DraftNodeKind.DELIVERY:
                    raise PlanAcceptanceUncoveredError("Delivery cannot verify acceptance")
                if _PROFILE_REQUIREMENT[node.verification_profile] is not (
                    criterion.verification_requirement
                ):
                    raise PlanAcceptanceUncoveredError(
                        "Verification profile cannot cover its criterion"
                    )
                covered[reference].append(node.node_id)
        result: list[AcceptanceCoverage] = []
        for criterion in contract.acceptance_criteria:
            node_ids = tuple(sorted(covered[criterion.criterion_id]))
            if criterion.required and not node_ids:
                raise PlanAcceptanceUncoveredError("Required criterion is not covered")
            if node_ids:
                result.append(
                    AcceptanceCoverage(
                        criterion_id=criterion.criterion_id,
                        node_ids=node_ids,
                        verification_requirement=criterion.verification_requirement,
                    )
                )
        return tuple(result)


def research_to_html_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    suffix = task_id.removeprefix("tsk_")
    capability_ids = {
        "artifact.html.v1",
        "browser.verify.v1",
        "research.read.v1",
    }
    refs = tuple(
        CapabilityRef(
            capability_id=pack.capability_id,
            version=pack.version,
            digest=pack.digest,
        )
        for pack in sorted(
            (capabilities.resolve_preferred(item) for item in capability_ids),
            key=lambda item: item.capability_id,
        )
    )
    return TaskContract(
        contract_id=f"tc_{suffix}",
        task_id=task_id,
        version=1,
        goal_ref=f"artifact://task-input/{task_id}",
        normalized_objective="研究公开主题并制作带来源的静态 HTML 页面",
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="ac_citations",
                kind=AcceptanceKind.CITATION_REQUIREMENT,
                description="主要事实具有 Claim 级引用。",
                verification_requirement=VerificationRequirement.CITATION,
                origin="trusted_template",
            ),
            AcceptanceCriterion(
                criterion_id="ac_html",
                kind=AcceptanceKind.ARTIFACT_REQUIREMENT,
                description="产生受控工作区中的静态 HTML revision。",
                verification_requirement=VerificationRequirement.ARTIFACT,
                origin="trusted_template",
            ),
            AcceptanceCriterion(
                criterion_id="ac_browser",
                kind=AcceptanceKind.OUTPUT_REQUIREMENT,
                description="HTML 通过隔离浏览器渲染检查。",
                verification_requirement=VerificationRequirement.BROWSER,
                origin="trusted_template",
            ),
            AcceptanceCriterion(
                criterion_id="ac_no_external_network",
                kind=AcceptanceKind.SAFETY_INVARIANT,
                description="浏览器验收期间没有外部网络访问。",
                verification_requirement=VerificationRequirement.DETERMINISTIC,
                origin="policy",
            ),
        ),
        constraints=("external_content_is_untrusted", "no_shell", "no_dynamic_code"),
        privacy_policy=PrivacyPolicy(
            classification="public",
            allowed_provider_locations=(ModelLocation.LOCAL, ModelLocation.CLOUD),
            allowed_privacy_modes=("local_preferred", "balanced"),
            external_egress_allowed=True,
        ),
        max_risk_level=ToolRiskLevel.R1,
        budget=TaskBudget(
            max_model_calls=8,
            max_tool_calls=12,
            max_input_tokens=100_000,
            max_output_tokens=20_000,
            max_wall_seconds=900,
            max_retries=3,
            max_cost_micros=2_000_000,
            max_handoffs=2,
            max_plan_nodes=5,
        ),
        output_contract=OutputContract(
            media_type="text/html",
            language="zh-CN",
            require_citations=True,
        ),
        capabilities=refs,
        research=ResearchContract(
            max_search_calls=3,
            max_page_reads=8,
            max_results_per_search=10,
            minimum_distinct_sources=2,
        ),
        workspace=TaskWorkspaceContract(
            workspace_ref=f"workspace://task/{suffix}",
        allowed_extensions=(".html", ".css"),
            max_total_bytes=1_048_576,
            max_files=10,
            retention_days=30,
        ),
        browser_verify=BrowserVerifyContract(
            profile_id="deskpilot.browser-static-html.v1"
        ),
        created_by="trusted_template",
    )


def research_to_html_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    zero = PlanNodeBudget(
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=30,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    return DraftPlan(
        task_id=task_id,
        contract_version=contract_version,
        producer=PlanProducer(
            kind="trusted_template", producer_ref="research_to_html.v1"
        ),
        nodes=(
            DraftPlanNode(
                local_key="research",
                kind=DraftNodeKind.AGENT,
                objective="搜索并读取公开来源，形成待验证 Claim/Citation。",
                agent_selector="builtin.web_researcher",
                capability_requirements=("research.read.v1",),
                acceptance_refs=("ac_citations",),
                verification_profile=VerificationProfile.CITATION,
                budget=PlanNodeBudget(
                    model_calls=4,
                    tool_calls=10,
                    input_tokens=60_000,
                    output_tokens=8_000,
                    wall_seconds=300,
                    retries=2,
                    cost_micros=1_000_000,
                    handoffs=0,
                ),
            ),
            DraftPlanNode(
                local_key="build_html",
                kind=DraftNodeKind.CAPABILITY,
                objective="只使用已验证 Claim 在 Task Workspace 生成静态 HTML。",
                capability_selector="artifact.html.v1",
                depends_on=("research",),
                acceptance_refs=("ac_html",),
                verification_profile=VerificationProfile.ARTIFACT,
                budget=PlanNodeBudget(
                    model_calls=4,
                    tool_calls=2,
                    input_tokens=40_000,
                    output_tokens=12_000,
                    wall_seconds=300,
                    retries=1,
                    cost_micros=1_000_000,
                    handoffs=1,
                ),
            ),
            DraftPlanNode(
                local_key="browser_verify",
                kind=DraftNodeKind.CAPABILITY,
                objective="在无登录、断网浏览器中验收当前 HTML revision。",
                capability_selector="browser.verify.v1",
                depends_on=("build_html",),
                acceptance_refs=("ac_browser",),
                verification_profile=VerificationProfile.BROWSER,
                budget=zero,
            ),
            DraftPlanNode(
                local_key="final_acceptance",
                kind=DraftNodeKind.FINAL_ACCEPTANCE,
                objective="确定性检查外部网络不变量和全部 coverage。",
                depends_on=("research", "build_html", "browser_verify"),
                acceptance_refs=("ac_no_external_network",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
            DraftPlanNode(
                local_key="delivery",
                kind=DraftNodeKind.DELIVERY,
                objective="只基于已验证视图形成交付清单。",
                depends_on=("final_acceptance",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
        ),
    )
