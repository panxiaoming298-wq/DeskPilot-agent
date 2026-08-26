"""Pure deterministic compiler from an untrusted DraftPlan to a sealed plan."""

from typing import Literal

from deskpilot.application.agent_registry import AgentRegistry, AgentRegistryError
from deskpilot.application.capability_catalog import CapabilityCatalog, CapabilityCatalogError
from deskpilot.application.plan_binder import AgentPlanBinder, AgentPlanBindingError
from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.application.workspace_coding_graph import (
    WORKSPACE_CODING_MAX_FILES,
    WORKSPACE_CODING_MIN_FILES,
    workspace_coding_coordinator_output_tokens,
    workspace_coding_max_output_tokens,
    workspace_coding_path_parameter,
    workspace_coding_planner_key,
    workspace_coding_planner_keys,
    workspace_coding_reader_key,
)
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

    def _validate_contract_capabilities(self, contract: TaskContract) -> dict[str, CapabilityRef]:
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
                ),
                allowed_locations=contract.privacy_policy.allowed_provider_locations,
                allowed_privacy_modes=contract.privacy_policy.allowed_privacy_modes,
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


def research_to_html_contract(
    task_id: str,
    capabilities: CapabilityCatalog,
    *,
    allow_user_path_export: bool = False,
) -> TaskContract:
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
        normalized_objective="研究公开主题并制作带来源的静态 HTML、Markdown 与 PDF 交付物",
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
                description=(
                    "产生受控工作区中的 HTML 主 revision、Markdown 与经过真实渲染验收的 "
                    "PDF 伴生 revision。"
                ),
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
            allowed_extensions=(".html", ".css", ".md", ".pdf"),
            max_total_bytes=1_048_576,
            max_files=10,
            retention_days=30,
            allow_user_path_export=allow_user_path_export,
        ),
        browser_verify=BrowserVerifyContract(profile_id="deskpilot.browser-static-html.v1"),
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
        producer=PlanProducer(kind="trusted_template", producer_ref="research_to_html.v1"),
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
                objective=(
                    "只使用已验证 Claim 在 Task Workspace 生成静态 HTML 主交付、"
                    "Markdown 伴生交付和经过真实渲染验收的 PDF 伴生交付。"
                ),
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


def knowledge_lookup_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="knowledge.local.v1",
        objective="查询本地知识库并返回带来源行号的结果",
        criterion=AcceptanceCriterion(
            criterion_id="ac_knowledge_citations",
            kind=AcceptanceKind.CITATION_REQUIREMENT,
            description="回答只引用来源版本和检索证明均有效的本地知识片段。",
            verification_requirement=VerificationRequirement.CITATION,
            origin="trusted_template",
        ),
        output=OutputContract(
            media_type="text/markdown",
            language="zh-CN",
            require_citations=True,
        ),
    )


def knowledge_lookup_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="knowledge_lookup.v1",
        local_key="knowledge_lookup",
        capability_id="knowledge.local.v1",
        objective="检索并复核本地知识 Citation。",
        acceptance_ref="ac_knowledge_citations",
        verification_profile=VerificationProfile.CITATION,
    )


def mcp_text_metrics_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="mcp.text.metrics.v1",
        objective="使用固定内置 MCP Server 计算文本指标",
        criterion=AcceptanceCriterion(
            criterion_id="ac_text_metrics",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="结果通过固定 MCP bundle、Schema 与审计链复核。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(
            media_type="application/json",
            language="zh-CN",
        ),
    )


def mcp_text_metrics_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="mcp_text_metrics.v1",
        local_key="mcp_text_metrics",
        capability_id="mcp.text.metrics.v1",
        objective="在短生命周期本地 MCP 进程中计算文本指标。",
        acceptance_ref="ac_text_metrics",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_file_read_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.file.read.v1",
        objective="读取已配置工作区内一个受限 UTF-8 文本文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_file_read",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="结果绑定规范化相对路径、稳定文件版本与内容摘要。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="text/markdown", language="zh-CN"),
        model_calls=2,
        input_tokens=20_000,
        output_tokens=2_000,
        cost_micros=100_000,
    )


def workspace_file_read_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_file_read.v1",
        local_key="workspace_file_read",
        capability_id="workspace.file.read.v1",
        objective="读取并复核工作区文件版本证明。",
        acceptance_ref="ac_workspace_file_read",
        verification_profile=VerificationProfile.DETERMINISTIC,
        agent_selector="builtin.workspace_reader",
        model_calls=2,
        input_tokens=20_000,
        output_tokens=2_000,
        cost_micros=100_000,
    )


def workspace_directory_list_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    suffix = task_id.removeprefix("tsk_")
    pack = capabilities.resolve_preferred("workspace.directory.read.v1")
    return TaskContract(
        contract_id=f"tc_{suffix}",
        task_id=task_id,
        version=1,
        goal_ref=f"artifact://task-input/{task_id}",
        normalized_objective=(
            "由受约束父 Agent 提出只读子任务 DAG，服务器裁决后列出工作区目录的受限直接子项"
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="ac_workspace_directory_list",
                kind=AcceptanceKind.OUTPUT_REQUIREMENT,
                description=(
                    "父 Agent 只消费服务器绑定动态图中全部已验证子 Agent 的 join；"
                    "目录结果绑定规范相对路径、排序子项、版本摘要与截断状态。"
                ),
                verification_requirement=VerificationRequirement.DETERMINISTIC,
                origin="trusted_template",
            ),
        ),
        constraints=(
            "external_content_is_untrusted",
            "no_shell",
            "no_dynamic_code",
            "server_adjudicated_handoff_only",
            "server_adjudicated_dynamic_graph_v1",
        ),
        privacy_policy=PrivacyPolicy(
            classification="internal",
            allowed_provider_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_only", "local_preferred", "balanced"),
            external_egress_allowed=False,
        ),
        max_risk_level=ToolRiskLevel.R0,
        budget=TaskBudget(
            max_model_calls=10,
            max_tool_calls=4,
            max_input_tokens=92_000,
            max_output_tokens=10_000,
            max_wall_seconds=450,
            max_retries=0,
            max_cost_micros=500_000,
            max_handoffs=4,
            max_plan_nodes=7,
        ),
        output_contract=OutputContract(media_type="application/json", language="zh-CN"),
        capabilities=(
            CapabilityRef(
                capability_id=pack.capability_id,
                version=pack.version,
                digest=pack.digest,
            ),
        ),
        created_by="trusted_template",
    )


def workspace_directory_list_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    zero = PlanNodeBudget(
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=15,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    return DraftPlan(
        task_id=task_id,
        contract_version=contract_version,
        producer=PlanProducer(kind="trusted_template", producer_ref="workspace_directory_list.v3"),
        nodes=(
            DraftPlanNode(
                local_key="workspace_directory_list",
                kind=DraftNodeKind.AGENT,
                objective="提出受控只读子任务 DAG，并只汇总服务器验证后的完整 join。",
                agent_selector="builtin.workspace_coordinator",
                acceptance_refs=("ac_workspace_directory_list",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=PlanNodeBudget(
                    model_calls=2,
                    tool_calls=0,
                    input_tokens=12_000,
                    output_tokens=2_000,
                    wall_seconds=60,
                    retries=0,
                    cost_micros=100_000,
                    handoffs=4,
                ),
            ),
            DraftPlanNode(
                local_key="final_acceptance",
                kind=DraftNodeKind.FINAL_ACCEPTANCE,
                objective="确定性复核父 Agent、动态子图及 verified join 的绑定。",
                depends_on=("workspace_directory_list",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
            DraftPlanNode(
                local_key="delivery",
                kind=DraftNodeKind.DELIVERY,
                objective="只基于已验证父结果形成对话交付。",
                depends_on=("final_acceptance",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
        ),
    )


def workspace_directory_analyze_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    base = workspace_directory_list_contract(task_id, capabilities)
    material = base.model_dump(mode="json")
    directory = capabilities.resolve_preferred("workspace.directory.read.v1")
    file = capabilities.resolve_preferred("workspace.file.read.v1")
    python_test = capabilities.resolve_preferred("workspace.python.test.v1")
    node_test = capabilities.resolve_preferred("workspace.node.test.v1")
    material["normalized_objective"] = (
        "由受约束父 Agent 生成异构只读 DAG，读取显式路径并按服务器固定协议运行测试"
    )
    material["constraints"] = [
        *base.constraints,
        "server_bound_capability_inputs_v1",
        "server_bound_fixed_test_inputs_v1",
        "server_adjudicated_test_conditions_v1",
        "fixed_executable_and_argv_v1",
    ]
    material["capabilities"] = [
        CapabilityRef(
            capability_id=directory.capability_id,
            version=directory.version,
            digest=directory.digest,
        ).model_dump(mode="json"),
        CapabilityRef(
            capability_id=file.capability_id,
            version=file.version,
            digest=file.digest,
        ).model_dump(mode="json"),
        CapabilityRef(
            capability_id=python_test.capability_id,
            version=python_test.version,
            digest=python_test.digest,
        ).model_dump(mode="json"),
        CapabilityRef(
            capability_id=node_test.capability_id,
            version=node_test.version,
            digest=node_test.digest,
        ).model_dump(mode="json"),
    ]
    return TaskContract.model_validate(material)


def workspace_directory_analyze_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    base = workspace_directory_list_draft(task_id, contract_version)
    material = base.model_dump(mode="json")
    material["producer"] = {
        "kind": "trusted_template",
        "producer_ref": "workspace_directory_analyze.v1",
    }
    nodes = material["nodes"]
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, dict)
        if node["local_key"] == "workspace_directory_list":
            node["local_key"] = "workspace_directory_analyze"
            node["objective"] = (
                "提出服务器绑定读取/固定测试输入的异构 DAG，并汇总完整 verified join。"
            )
        if node.get("depends_on") == ["workspace_directory_list"]:
            node["depends_on"] = ["workspace_directory_analyze"]
    return DraftPlan.model_validate(material)


def workspace_snapshot_check_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.snapshot.check.v1",
        objective="在断网隔离进程中检查工作区文本快照",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_snapshot_check",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="结果绑定固定 profile、完整有界快照、解析问题与隔离证明。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_snapshot_check_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_snapshot_check.v1",
        local_key="workspace_snapshot_check",
        capability_id="workspace.snapshot.check.v1",
        objective="对只读文件快照运行固定语法解析检查。",
        acceptance_ref="ac_workspace_snapshot_check",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_python_test_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.python.test.v1",
        objective="在断网隔离进程中运行项目的一个显式 Python 测试文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_python_test",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=("结果绑定完整有界快照、内容寻址运行时、pytest 输出与隔离证明。"),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_python_test_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_python_test.v1",
        local_key="workspace_python_test",
        capability_id="workspace.python.test.v1",
        objective="从只读项目快照运行固定 pytest 文件协议。",
        acceptance_ref="ac_workspace_python_test",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_node_test_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.node.test.v1",
        objective="在断网隔离进程中运行项目的一个显式 Node 测试文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_node_test",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=("结果绑定完整有界快照、内容寻址 Node 运行时、node:test 输出与隔离证明。"),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_node_test_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_node_test.v1",
        local_key="workspace_node_test",
        capability_id="workspace.node.test.v1",
        objective="从有界项目快照运行固定 Node node:test 文件协议。",
        acceptance_ref="ac_workspace_node_test",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_project_search_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.project.search.v1",
        objective="在显式项目根内递归搜索受限 UTF-8 源文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_project_search",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=(
                "结果绑定项目根、查询摘要、排序匹配、扫描边界与链接拒绝策略。"
            ),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_project_search_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_project_search.v1",
        local_key="workspace_project_search",
        capability_id="workspace.project.search.v1",
        objective="递归搜索项目源文件并生成有界内容寻址匹配。",
        acceptance_ref="ac_workspace_project_search",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_project_batch_read_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.project.read_many.v1",
        objective="在显式项目根内批量读取受限 UTF-8 文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_project_batch_read",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="结果绑定排序路径、逐文件版本/内容摘要和总字节边界。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_project_batch_read_draft(
    task_id: str, contract_version: int = 1
) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_project_batch_read.v1",
        local_key="workspace_project_batch_read",
        capability_id="workspace.project.read_many.v1",
        objective="批量读取项目文件并复核每个版本证明。",
        acceptance_ref="ac_workspace_project_batch_read",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_git_inspect_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.git.inspect.v1",
        objective="以服务器固定只读配置检查项目 Git 状态、差异或历史",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_git_inspect",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=(
                "结果绑定仓库/HEAD/工具链摘要、固定操作、输出限制及关闭扩展证明。"
            ),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
    )


def workspace_git_inspect_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_git_inspect.v1",
        local_key="workspace_git_inspect",
        capability_id="workspace.git.inspect.v1",
        objective="运行一个服务器固定的只读 Git 检查。",
        acceptance_ref="ac_workspace_git_inspect",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_command_profile_contract(
    task_id: str, capabilities: CapabilityCatalog
) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.command.run.v1",
        objective="在断网临时项目快照中运行一个服务器注册的固定命令 Profile",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_command_profile",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=(
                "结果绑定 Profile、项目快照、内容寻址工具链、退出码、受限输出、"
                "超时/取消回执及 AppContainer 断网证明。"
            ),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
        max_tool_calls=2,
        max_retries=1,
    )


def workspace_command_profile_draft(
    task_id: str, contract_version: int = 1
) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_command_profile.v1",
        local_key="workspace_command_profile",
        capability_id="workspace.command.run.v1",
        objective="运行一个服务器固定、断网、临时快照命令 Profile。",
        acceptance_ref="ac_workspace_command_profile",
        verification_profile=VerificationProfile.DETERMINISTIC,
        max_tool_calls=2,
        max_retries=1,
    )


def workspace_file_replace_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.file.replace.v1",
        objective="经用户确认后精确替换工作区文件中的单个文本片段",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_file_replace",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="提交绑定预览摘要、原版本、结果版本、安全备份与不可变回执。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
        max_risk_level=ToolRiskLevel.R1,
    )


def workspace_file_replace_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_file_replace.v1",
        local_key="workspace_file_replace",
        capability_id="workspace.file.replace.v1",
        objective="提交已确认的单次文本替换并核验安全备份。",
        acceptance_ref="ac_workspace_file_replace",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_file_create_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.file.create.v1",
        objective="经用户确认后创建一个不存在的工作区文本文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_file_create",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="提交绑定目标目录版本、拟写内容、持久化恢复清单与不可变回执。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
        max_risk_level=ToolRiskLevel.R1,
    )


def workspace_file_create_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_file_create.v1",
        local_key="workspace_file_create",
        capability_id="workspace.file.create.v1",
        objective="提交已确认的新建文件并核验恢复证明。",
        acceptance_ref="ac_workspace_file_create",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_file_rename_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.file.rename.v1",
        objective="经用户确认后原子重命名一个工作区文本文件",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_file_rename",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="提交绑定源文件版本、目标目录版本、内容恒等证明与不可变回执。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
        max_risk_level=ToolRiskLevel.R1,
    )


def workspace_file_rename_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_file_rename.v1",
        local_key="workspace_file_rename",
        capability_id="workspace.file.rename.v1",
        objective="提交已确认的文件重命名并核验内容身份。",
        acceptance_ref="ac_workspace_file_rename",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_patch_bundle_contract(task_id: str, capabilities: CapabilityCatalog) -> TaskContract:
    return _direct_capability_contract(
        task_id,
        capabilities,
        capability_id="workspace.patch.bundle.v1",
        objective="经用户一次确认后提交隔离预演的多文件精确替换补丁",
        criterion=AcceptanceCriterion(
            criterion_id="ac_workspace_patch_bundle",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description="提交绑定全部原版本、隔离副本、逐项备份及完整或部分完成回执。",
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ),
        output=OutputContract(media_type="application/json", language="zh-CN"),
        max_risk_level=ToolRiskLevel.R1,
    )


def workspace_patch_bundle_draft(task_id: str, contract_version: int = 1) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_patch_bundle.v1",
        local_key="workspace_patch_bundle",
        capability_id="workspace.patch.bundle.v1",
        objective="核验隔离补丁清单并按序提交全部精确替换。",
        acceptance_ref="ac_workspace_patch_bundle",
        verification_profile=VerificationProfile.DETERMINISTIC,
    )


def workspace_agent_patch_test_contract(
    task_id: str,
    capabilities: CapabilityCatalog,
    *,
    test_kind: Literal["python", "node"],
) -> TaskContract:
    suffix = task_id.removeprefix("tsk_")
    capability_ids = (
        "workspace.file.read.v1",
        "workspace.patch.propose.v1",
        "workspace.patch.bundle.v1",
        (
            "workspace.python.test.v1"
            if test_kind == "python"
            else "workspace.node.test.v1"
        ),
    )
    refs = tuple(
        CapabilityRef(
            capability_id=pack.capability_id,
            version=pack.version,
            digest=pack.digest,
        )
        for pack in (capabilities.resolve_preferred(item) for item in capability_ids)
    )
    return TaskContract(
        contract_id=f"tc_{suffix}",
        task_id=task_id,
        version=1,
        goal_ref=f"artifact://task-input/{task_id}",
        normalized_objective=(
            "读取一个显式文件并生成无授权精确补丁提案；仅在用户确认后应用，"
            "再运行服务器固定测试形成可验证结果"
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="ac_workspace_agent_patch_test",
                kind=AcceptanceKind.OUTPUT_REQUIREMENT,
                description=(
                    "结果绑定文件观察、模型提案、用户确认摘要、补丁回执、"
                    "固定测试快照/运行时与隔离证明。"
                ),
                verification_requirement=VerificationRequirement.DETERMINISTIC,
                origin="trusted_template",
            ),
        ),
        constraints=(
            "external_content_is_untrusted",
            "no_shell",
            "no_dynamic_code",
            "model_patch_proposal_grants_no_authority_v1",
            "explicit_user_patch_confirmation_v1",
            "server_bound_fixed_test_v1",
            "no_automatic_replan_after_workspace_write_v1",
        ),
        privacy_policy=PrivacyPolicy(
            classification="internal",
            allowed_provider_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_only", "local_preferred", "balanced"),
            external_egress_allowed=False,
        ),
        max_risk_level=ToolRiskLevel.R1,
        budget=TaskBudget(
            max_model_calls=2,
            max_tool_calls=2,
            max_input_tokens=24_000,
            max_output_tokens=3_000,
            max_wall_seconds=180,
            max_retries=0,
            max_cost_micros=100_000,
            max_handoffs=0,
            max_plan_nodes=3,
        ),
        output_contract=OutputContract(media_type="application/json", language="zh-CN"),
        capabilities=refs,
        created_by="trusted_template",
    )


def workspace_agent_patch_test_draft(
    task_id: str, contract_version: int = 1
) -> DraftPlan:
    return _direct_capability_draft(
        task_id,
        contract_version,
        producer_ref="workspace_agent_patch_test.v1",
        local_key="workspace_agent_patch_test",
        capability_id="workspace.patch.propose.v1",
        objective=(
            "读取服务器绑定文件，提出一次无授权精确替换，并等待用户确认后固定测试。"
        ),
        acceptance_ref="ac_workspace_agent_patch_test",
        verification_profile=VerificationProfile.DETERMINISTIC,
        agent_selector="builtin.workspace_patch_planner",
        model_calls=2,
        input_tokens=24_000,
        output_tokens=3_000,
        cost_micros=100_000,
    )


def workspace_dynamic_patch_test_contract(
    task_id: str,
    capabilities: CapabilityCatalog,
    *,
    test_kind: Literal["python", "node"],
) -> TaskContract:
    """Authorize a model-shaped DAG with composable server-bound Patch approvals."""

    base = workspace_directory_analyze_contract(task_id, capabilities)
    material = base.model_dump(mode="json")
    capability_ids = (
        "workspace.directory.read.v1",
        "workspace.file.read.v1",
        "workspace.patch.propose.v1",
        "workspace.patch.bundle.v1",
        (
            "workspace.python.test.v1"
            if test_kind == "python"
            else "workspace.node.test.v1"
        ),
    )
    material["normalized_objective"] = (
        "由受约束父 Agent 生成动态修复 DAG；每个 Patch 节点只能消费"
        "一个服务器签发的目标绑定，只提交无授权建议，并分别等待用户"
        "确认当前内容寻址 manifest 后写入并运行服务器固定测试"
    )
    material["acceptance_criteria"] = [
        AcceptanceCriterion(
            criterion_id="ac_workspace_dynamic_patch_test",
            kind=AcceptanceKind.OUTPUT_REQUIREMENT,
            description=(
                "最终目录输出依赖节点级审批证明、补丁回执、固定测试结果及"
                "全部上游类型化 ResultRef；未确认建议不能形成写入。"
            ),
            verification_requirement=VerificationRequirement.DETERMINISTIC,
            origin="trusted_template",
        ).model_dump(mode="json")
    ]
    material["constraints"] = [
        *base.constraints,
        "model_patch_proposal_grants_no_authority_v1",
        "dynamic_patch_approval_node_v1",
        "fresh_confirmation_per_patch_node_v1",
        "composable_patch_approval_nodes_v1",
        "distinct_server_bound_patch_input_per_node_v1",
        "no_automatic_replan_after_workspace_write_v1",
        "maximum_three_patch_plan_generations_v1",
        "cross_generation_task_budget_v1",
        "fresh_confirmation_after_replan_v1",
    ]
    base_budget = base.budget
    material["budget"] = TaskBudget(
        max_model_calls=base_budget.max_model_calls * 3,
        max_tool_calls=base_budget.max_tool_calls * 3,
        max_input_tokens=base_budget.max_input_tokens * 3,
        max_output_tokens=base_budget.max_output_tokens * 3,
        max_wall_seconds=base_budget.max_wall_seconds * 3,
        max_retries=base_budget.max_retries * 3,
        max_cost_micros=base_budget.max_cost_micros * 3,
        max_handoffs=base_budget.max_handoffs * 3,
        # This field remains the per-generation structural graph ceiling.
        max_plan_nodes=base_budget.max_plan_nodes,
    ).model_dump(mode="json")
    material["max_risk_level"] = ToolRiskLevel.R1.value
    material["capabilities"] = [
        CapabilityRef(
            capability_id=pack.capability_id,
            version=pack.version,
            digest=pack.digest,
        ).model_dump(mode="json")
        for pack in (capabilities.resolve_preferred(item) for item in capability_ids)
    ]
    return TaskContract.model_validate(material)


def workspace_dynamic_patch_test_draft(
    task_id: str, contract_version: int = 1
) -> DraftPlan:
    base = workspace_directory_analyze_draft(task_id, contract_version)
    material = base.model_dump(mode="json")
    material["producer"] = {
        "kind": "trusted_template",
        "producer_ref": "workspace_dynamic_patch_test.v1",
    }
    nodes = material["nodes"]
    assert isinstance(nodes, list)
    for node in nodes:
        assert isinstance(node, dict)
        if node["local_key"] == "workspace_directory_analyze":
            node["local_key"] = "workspace_dynamic_patch_test"
            node["objective"] = (
                "提出含节点级 Patch/Approval 的服务器裁决 DAG，并只汇总 verified join。"
            )
            node["acceptance_refs"] = ["ac_workspace_dynamic_patch_test"]
        if node.get("depends_on") == ["workspace_directory_analyze"]:
            node["depends_on"] = ["workspace_dynamic_patch_test"]
    return DraftPlan.model_validate(material)


def workspace_coding_loop_contract(
    task_id: str,
    capabilities: CapabilityCatalog,
    *,
    test_kind: Literal["python", "node"],
    file_count: int = WORKSPACE_CODING_MIN_FILES,
) -> TaskContract:
    """Authorize one persistent LOCAL-only inspect/patch/test coding loop.

    Every file input and the complete Patch bundle are message-bound before
    execution. The model cannot turn a Reader result into new authority; each
    Reader feeds one verified, unprivileged Patch Planner proposal and the
    complete proposal set forms the fixed verified join before Patch execution.
    """

    if not WORKSPACE_CODING_MIN_FILES <= file_count <= WORKSPACE_CODING_MAX_FILES:
        raise ValueError("Workspace coding Contract file count is outside 2..8")

    suffix = task_id.removeprefix("tsk_")
    capability_ids = (
        "workspace.dynamic.coordinate.v1",
        "workspace.file.read.v1",
        "workspace.patch.propose.v1",
        "workspace.patch.bundle.v1",
        "workspace.git.commit.v1",
        (
            "workspace.python.test.v1"
            if test_kind == "python"
            else "workspace.node.test.v1"
        ),
    )
    refs = tuple(
        CapabilityRef(
            capability_id=pack.capability_id,
            version=pack.version,
            digest=pack.digest,
        )
        for pack in (capabilities.resolve_preferred(item) for item in capability_ids)
    )
    normalized_objective = (
        "由受约束 Coordinator 确认服务器封存的固定编码图；两个独立本地 Reader"
        "并行读取服务器绑定文件；两个无执行权 Patch Planner"
        "只能提议服务器封存的精确替换；全部结果分别验证后执行经用户确认的多文件"
        "Patch，再运行服务器固定测试；测试通过后经第二次精确确认创建服务器命名的"
        "Git 分支与提交，最后独立验收交付"
        if file_count == WORKSPACE_CODING_MIN_FILES
        else (
            f"由受约束 Coordinator 确认服务器封存的 {file_count} 文件固定编码图；"
            f"{file_count} 个独立本地 Reader 分批并行读取服务器绑定文件；"
            f"{file_count} 个无执行权 Patch Planner 只能提议服务器封存的精确替换；"
            "全部结果分别验证后执行经用户确认的多文件 Patch，再运行服务器固定测试；"
            "测试通过后经第二次精确确认创建服务器命名的 Git 分支与提交，最后独立验收交付"
        )
    )
    acceptance_description = (
        "结果必须绑定一个 Coordinator 图确认、两个 Reader ResultRef、"
        "两个 Patch Proposal ResultRef、内容寻址 Patch 回执、固定测试结果、"
        "受控 Git 提交回执、失败/Repair 历史和最终确定性验收。"
        if file_count == WORKSPACE_CODING_MIN_FILES
        else (
            f"结果必须绑定一个 Coordinator 图确认、{file_count} 个 Reader ResultRef、"
            f"{file_count} 个 Patch Proposal ResultRef、内容寻址 Patch 回执、固定测试"
            "结果、受控 Git 提交回执、失败/Repair 历史和最终确定性验收。"
        )
    )
    cardinality_constraints = (
        ("two_independent_read_only_children_v1",)
        if file_count == WORKSPACE_CODING_MIN_FILES
        else (
            "two_to_eight_independent_read_only_children_v1",
            f"server_fixed_file_count_{file_count}_v1",
        )
    )
    return TaskContract(
        contract_id=f"tc_{suffix}",
        task_id=task_id,
        version=1,
        goal_ref=f"artifact://task-input/{task_id}",
        normalized_objective=normalized_objective,
        acceptance_criteria=(
            AcceptanceCriterion(
                criterion_id="ac_workspace_coding_loop",
                kind=AcceptanceKind.OUTPUT_REQUIREMENT,
                description=acceptance_description,
                verification_requirement=VerificationRequirement.DETERMINISTIC,
                origin="trusted_template",
            ),
        ),
        constraints=(
            "local_only_coding_loop_v1",
            "server_bounded_coordinator_graph_v1",
            *cardinality_constraints,
            "verified_result_join_before_patch_planner_v1",
            "unprivileged_patch_planner_model_turn_v1",
            "exact_multi_file_patch_confirmation_v1",
            "server_bound_fixed_test_v1",
            "exact_git_branch_commit_confirmation_v1",
            "git_hooks_signing_push_disabled_v1",
            "single_bounded_test_repair_v1",
            "no_shell",
            "no_dynamic_code",
            "no_dependency_install",
            "no_automatic_push",
            "no_automatic_replay_after_workspace_write_v1",
        ),
        privacy_policy=PrivacyPolicy(
            classification="internal",
            allowed_provider_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_only", "local_preferred", "balanced"),
            external_egress_allowed=False,
        ),
        max_risk_level=ToolRiskLevel.R1,
        budget=TaskBudget(
            max_model_calls=(5 if file_count == 2 else 1 + (2 * file_count)),
            max_tool_calls=(5 if file_count == 2 else file_count + 3),
            max_input_tokens=(
                56_000 if file_count == 2 else 12_000 + (12_001 * file_count)
            ),
            max_output_tokens=workspace_coding_max_output_tokens(file_count),
            max_wall_seconds=(780 if file_count == 2 else 510 + (120 * file_count)),
            max_retries=1,
            max_cost_micros=(
                300_000 if file_count == 2 else 100_000 + (50_000 * file_count)
            ),
            max_handoffs=0,
            max_plan_nodes=2 * file_count + 6,
        ),
        output_contract=OutputContract(media_type="application/json", language="zh-CN"),
        capabilities=refs,
        created_by="trusted_template",
    )


def workspace_coding_loop_draft(
    task_id: str,
    *,
    test_kind: Literal["python", "node"],
    file_count: int = WORKSPACE_CODING_MIN_FILES,
    contract_version: int = 1,
) -> DraftPlan:
    if not WORKSPACE_CODING_MIN_FILES <= file_count <= WORKSPACE_CODING_MAX_FILES:
        raise ValueError("Workspace coding Draft file count is outside 2..8")
    coordinator_budget = PlanNodeBudget(
        model_calls=1,
        tool_calls=0,
        input_tokens=12_000,
        output_tokens=workspace_coding_coordinator_output_tokens(file_count),
        wall_seconds=60,
        retries=0,
        cost_micros=100_000,
        handoffs=0,
    )
    reader_budget = PlanNodeBudget(
        model_calls=1,
        tool_calls=1,
        input_tokens=1,
        output_tokens=1,
        wall_seconds=60,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    planner_budget = PlanNodeBudget(
        model_calls=1,
        tool_calls=0,
        input_tokens=12_000,
        output_tokens=1_500,
        wall_seconds=60,
        retries=0,
        cost_micros=50_000,
        handoffs=0,
    )
    patch_budget = PlanNodeBudget(
        model_calls=0,
        tool_calls=1,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=120,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    test_budget = PlanNodeBudget(
        model_calls=0,
        tool_calls=1,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=180,
        retries=1,
        cost_micros=0,
        handoffs=0,
    )
    zero = PlanNodeBudget(
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=15,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    test_capability = (
        "workspace.python.test.v1"
        if test_kind == "python"
        else "workspace.node.test.v1"
    )
    reader_nodes = tuple(
        DraftPlanNode(
            local_key=workspace_coding_reader_key(index),
            kind=DraftNodeKind.AGENT,
            objective=(
                (
                    "独立读取主要目标文件并形成确定性版本证明。"
                    if index == 1
                    else "独立读取上下文文件并形成确定性版本证明。"
                )
                if file_count == WORKSPACE_CODING_MIN_FILES
                else (
                    f"独立读取第 {index} 个服务器绑定目标文件并形成确定性版本证明；"
                    f"输入只来自 {workspace_coding_path_parameter(index)}。"
                )
            ),
            agent_selector="builtin.workspace_reader",
            capability_requirements=("workspace.file.read.v1",),
            depends_on=(
                "coordinate_coding"
                if file_count == WORKSPACE_CODING_MIN_FILES
                else "coordinate_bounded_coding",
            ),
            verification_profile=VerificationProfile.DETERMINISTIC,
            budget=reader_budget,
        )
        for index in range(1, file_count + 1)
    )
    planner_nodes = tuple(
        DraftPlanNode(
            local_key=workspace_coding_planner_key(index),
            kind=DraftNodeKind.AGENT,
            objective=(
                (
                    "基于已验证的主要文件证据提议服务器 Offer 中唯一允许的精确替换；"
                    "提议本身不授予写权限。"
                    if index == 1
                    else (
                        "基于已验证的次要文件证据提议服务器 Offer 中唯一允许的精确替换；"
                        "提议本身不授予写权限。"
                    )
                )
                if file_count == WORKSPACE_CODING_MIN_FILES
                else (
                    f"基于第 {index} 个已验证文件证据提议服务器 Offer 中唯一允许的"
                    "精确替换；提议本身不授予写权限。"
                )
            ),
            agent_selector="builtin.workspace_patch_planner",
            capability_requirements=("workspace.patch.propose.v1",),
            depends_on=(workspace_coding_reader_key(index),),
            verification_profile=VerificationProfile.DETERMINISTIC,
            budget=planner_budget,
        )
        for index in range(1, file_count + 1)
    )
    coordinator_key = (
        "coordinate_coding"
        if file_count == WORKSPACE_CODING_MIN_FILES
        else "coordinate_bounded_coding"
    )
    return DraftPlan(
        task_id=task_id,
        contract_version=contract_version,
        producer=PlanProducer(
            kind="trusted_template",
            producer_ref=(
                "workspace_coding_loop.v1"
                if file_count == WORKSPACE_CODING_MIN_FILES
                else "workspace_coding_loop.bounded.v1"
            ),
        ),
        nodes=(
            DraftPlanNode(
                local_key=coordinator_key,
                kind=DraftNodeKind.AGENT,
                objective=(
                    (
                        "只确认服务器封存的七节点编码图、精确依赖和预算；不得创建"
                        "路径、工具、节点、预算或权限。"
                    )
                    if file_count == WORKSPACE_CODING_MIN_FILES
                    else (
                        f"只确认服务器封存的 {2 * file_count + 3} 节点编码图、精确依赖"
                        "和预算；不得创建路径、工具、节点、预算或权限。"
                    )
                ),
                agent_selector=(
                    "builtin.workspace_coordinator"
                    if file_count == WORKSPACE_CODING_MIN_FILES
                    else "builtin.workspace_bounded_coordinator"
                ),
                capability_requirements=("workspace.dynamic.coordinate.v1",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=coordinator_budget,
            ),
            *reader_nodes,
            *planner_nodes,
            DraftPlanNode(
                local_key="apply_patch",
                kind=DraftNodeKind.CAPABILITY,
                objective=(
                    (
                        "只在两个 Reader 及两个无执行权 Patch Planner 提议均已验证后，"
                        "准备并提交精确多文件 Patch。"
                    )
                    if file_count == WORKSPACE_CODING_MIN_FILES
                    else (
                        f"只在 {file_count} 个 Reader 及 {file_count} 个无执行权 Patch "
                        "Planner 提议均已验证后，准备并提交精确多文件 Patch。"
                    )
                ),
                capability_selector="workspace.patch.bundle.v1",
                depends_on=workspace_coding_planner_keys(file_count),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=patch_budget,
            ),
            DraftPlanNode(
                local_key="run_fixed_test",
                kind=DraftNodeKind.CAPABILITY,
                objective="在内容寻址 Patch 回执之后运行服务器固定测试。",
                capability_selector=test_capability,
                depends_on=("apply_patch",),
                acceptance_refs=("ac_workspace_coding_loop",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=test_budget,
            ),
            DraftPlanNode(
                local_key="commit_git",
                kind=DraftNodeKind.CAPABILITY,
                objective=(
                    "只在固定测试通过后，准备经用户精确确认的服务器命名 Git 分支与"
                    "提交；关闭 hooks、签名和 push，并生成可对账回执。"
                ),
                capability_selector="workspace.git.commit.v1",
                depends_on=("run_fixed_test",),
                acceptance_refs=("ac_workspace_coding_loop",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=patch_budget,
            ),
            DraftPlanNode(
                local_key="final_acceptance",
                kind=DraftNodeKind.FINAL_ACCEPTANCE,
                objective=(
                    "独立复核双 Reader、Patch、Test、Git commit 与 Repair 证明链。"
                    if file_count == WORKSPACE_CODING_MIN_FILES
                    else (
                        f"独立复核 {file_count} 个 Reader/Planner、Patch、Test、Git commit "
                        "与 Repair 证明链。"
                    )
                ),
                depends_on=("commit_git",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
            DraftPlanNode(
                local_key="delivery",
                kind=DraftNodeKind.DELIVERY,
                objective="只基于完整 verified ResultRef 链交付编码结果。",
                depends_on=("final_acceptance",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
        ),
    )


def _direct_capability_contract(
    task_id: str,
    capabilities: CapabilityCatalog,
    *,
    capability_id: str,
    objective: str,
    criterion: AcceptanceCriterion,
    output: OutputContract,
    max_risk_level: ToolRiskLevel = ToolRiskLevel.R0,
    model_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    max_tool_calls: int = 1,
    max_retries: int = 0,
) -> TaskContract:
    suffix = task_id.removeprefix("tsk_")
    pack = capabilities.resolve_preferred(capability_id)
    return TaskContract(
        contract_id=f"tc_{suffix}",
        task_id=task_id,
        version=1,
        goal_ref=f"artifact://task-input/{task_id}",
        normalized_objective=objective,
        acceptance_criteria=(criterion,),
        constraints=("external_content_is_untrusted", "no_shell", "no_dynamic_code"),
        privacy_policy=PrivacyPolicy(
            classification="internal",
            allowed_provider_locations=(ModelLocation.LOCAL,),
            allowed_privacy_modes=("local_only", "local_preferred", "balanced"),
            external_egress_allowed=False,
        ),
        max_risk_level=max_risk_level,
        budget=TaskBudget(
            max_model_calls=model_calls,
            max_tool_calls=max_tool_calls,
            max_input_tokens=input_tokens,
            max_output_tokens=output_tokens,
            max_wall_seconds=90,
            max_retries=max_retries,
            max_cost_micros=cost_micros,
            max_handoffs=0,
            max_plan_nodes=3,
        ),
        output_contract=output,
        capabilities=(
            CapabilityRef(
                capability_id=pack.capability_id,
                version=pack.version,
                digest=pack.digest,
            ),
        ),
        created_by="trusted_template",
    )


def _direct_capability_draft(
    task_id: str,
    contract_version: int,
    *,
    producer_ref: str,
    local_key: str,
    capability_id: str,
    objective: str,
    acceptance_ref: str,
    verification_profile: VerificationProfile,
    agent_selector: str | None = None,
    model_calls: int = 0,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cost_micros: int = 0,
    max_tool_calls: int = 1,
    max_retries: int = 0,
) -> DraftPlan:
    zero = PlanNodeBudget(
        model_calls=0,
        tool_calls=0,
        input_tokens=0,
        output_tokens=0,
        wall_seconds=15,
        retries=0,
        cost_micros=0,
        handoffs=0,
    )
    return DraftPlan(
        task_id=task_id,
        contract_version=contract_version,
        producer=PlanProducer(kind="trusted_template", producer_ref=producer_ref),
        nodes=(
            DraftPlanNode(
                local_key=local_key,
                kind=(
                    DraftNodeKind.AGENT if agent_selector is not None else DraftNodeKind.CAPABILITY
                ),
                objective=objective,
                agent_selector=agent_selector,
                capability_selector=(capability_id if agent_selector is None else None),
                capability_requirements=((capability_id,) if agent_selector is not None else ()),
                acceptance_refs=(acceptance_ref,),
                verification_profile=verification_profile,
                budget=PlanNodeBudget(
                    model_calls=model_calls,
                    tool_calls=max_tool_calls,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    wall_seconds=60,
                    retries=max_retries,
                    cost_micros=cost_micros,
                    handoffs=0,
                ),
            ),
            DraftPlanNode(
                local_key="final_acceptance",
                kind=DraftNodeKind.FINAL_ACCEPTANCE,
                objective="确定性复核绑定结果与证据摘要。",
                depends_on=(local_key,),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
            DraftPlanNode(
                local_key="delivery",
                kind=DraftNodeKind.DELIVERY,
                objective="只基于已验证结果形成对话交付。",
                depends_on=("final_acceptance",),
                verification_profile=VerificationProfile.DETERMINISTIC,
                budget=zero,
            ),
        ),
    )
