"""Fixed read-only Agent registrations for the frozen v1 Registry."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.application.agent_registry import (
    AgentRegistration,
    AgentRegistry,
    PromptPackage,
    load_agent_contract,
    load_prompt_package,
)
from deskpilot.application.tool_registry import ToolRegistry
from deskpilot.domain.agent_contracts import (
    AgentBudgetPolicy,
    AgentContextPolicy,
    AgentContract,
    AgentHandoffPolicy,
    AgentHandoffRef,
    AgentKind,
    AgentModelPolicy,
    AgentResultPolicy,
    AgentToolGrant,
    AgentToolPolicy,
    PromptPackageRef,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelLocation,
    ModelProviderDescriptor,
    ModelRole,
)
from deskpilot.domain.research import ResearchAgentDecision
from deskpilot.domain.tool_contracts import ToolRiskLevel


class AgentReferenceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    objective_ref: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    context_refs: tuple[str, ...] = Field(default=(), max_length=32)


class EvidenceAgentResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    outcome: str = Field(pattern=r"^(?:succeeded|partial|needs_user)$")
    claim_count: int = Field(ge=0, le=100)
    evidence_refs: tuple[str, ...] = Field(default=(), max_length=100)
    limitation_codes: tuple[str, ...] = Field(default=(), max_length=20)


def create_builtin_agent_registry(
    tool_registry: ToolRegistry,
    model_descriptors: tuple[ModelProviderDescriptor, ...],
) -> AgentRegistry:
    prompt_root = Path(__file__).parent / "prompts"
    computer_prompt = load_prompt_package(prompt_root, "computer_observer.json")
    knowledge_prompt = load_prompt_package(prompt_root, "knowledge_researcher.json")
    web_prompt = load_prompt_package(prompt_root, "web_researcher.json")
    synthesizer_prompt = load_prompt_package(prompt_root, "task_synthesizer.json")
    disk = tool_registry.resolve("computer.disk_usage", "1.0.0").contract
    synth_ref = AgentHandoffRef(agent_id="builtin.task_synthesizer", version="1.0.0")
    computer_ref = AgentHandoffRef(agent_id="builtin.computer_observer", version="1.0.0")
    knowledge_ref = AgentHandoffRef(agent_id="builtin.knowledge_researcher", version="1.0.0")

    registry = AgentRegistry()
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.computer_observer",
                kind=AgentKind.WORKER,
                display_name="Computer Observer",
                description="读取本机只读容量证据，不执行写入。",
                provides=("computer.metadata.read", "computer.disk.inspect"),
                prompt=computer_prompt,
                tool_policy=AgentToolPolicy(
                    max_risk_level=ToolRiskLevel.R0,
                    grants=(
                        AgentToolGrant(
                            name=disk.name,
                            version=disk.version,
                            contract_digest=disk.digest,
                            max_calls=2,
                        ),
                    ),
                ),
                handoff=AgentHandoffPolicy(may_receive_from=(synth_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=2,
                    max_input_tokens=20_000,
                    max_output_tokens=4_000,
                    max_wall_seconds=60,
                    max_retries=1,
                    max_cost_micros=100_000,
                ),
                required_evidence=("tool_receipt",),
            ),
            input_model=AgentReferenceInput,
            output_model=EvidenceAgentResult,
            prompt_package=computer_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.web_researcher",
                kind=AgentKind.WORKER,
                display_name="Web Researcher",
                description="仅基于受控页面快照提出待验证 Claim 与 Citation。",
                provides=("research.read.v1",),
                prompt=web_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=4,
                    max_tool_calls=10,
                    max_input_tokens=60_000,
                    max_output_tokens=8_000,
                    max_wall_seconds=300,
                    max_retries=2,
                    max_cost_micros=1_000_000,
                ),
                required_evidence=("citation",),
                require_citations=True,
                output_model=ResearchAgentDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "working_memory",
                    "long_term_memory",
                    "external_untrusted_page_snapshot",
                ),
                memory_read_scopes=("user",),
            ),
            input_model=AgentReferenceInput,
            output_model=ResearchAgentDecision,
            prompt_package=web_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.knowledge_researcher",
                kind=AgentKind.WORKER,
                display_name="Knowledge Researcher",
                description="检索受控本地知识来源并保留 Citation。",
                provides=("knowledge.local.search", "knowledge.citation.read"),
                prompt=knowledge_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(synth_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=30_000,
                    max_output_tokens=5_000,
                    max_wall_seconds=60,
                    max_retries=1,
                    max_cost_micros=100_000,
                ),
                required_evidence=("citation",),
                require_citations=True,
                rag_collections=("local_knowledge",),
            ),
            input_model=AgentReferenceInput,
            output_model=EvidenceAgentResult,
            prompt_package=knowledge_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.task_synthesizer",
                kind=AgentKind.SYNTHESIZER,
                display_name="Task Synthesizer",
                description="只汇总已经验证的上游 Artifact 与 Evidence。",
                provides=("task.result.synthesize",),
                prompt=synthesizer_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(
                    may_delegate_to=(computer_ref, knowledge_ref),
                    max_outgoing_handoffs=2,
                    max_depth=1,
                ),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=1,
                    max_tool_calls=0,
                    max_input_tokens=40_000,
                    max_output_tokens=6_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=2,
                ),
                required_evidence=("verified_upstream_result",),
            ),
            input_model=AgentReferenceInput,
            output_model=EvidenceAgentResult,
            prompt_package=synthesizer_prompt,
        )
    )
    registry.freeze(tool_registry, model_descriptors)
    return registry


def _contract(
    *,
    agent_id: str,
    kind: AgentKind,
    display_name: str,
    description: str,
    provides: tuple[str, ...],
    prompt: PromptPackage,
    tool_policy: AgentToolPolicy,
    handoff: AgentHandoffPolicy,
    role: ModelRole,
    budget: AgentBudgetPolicy,
    required_evidence: tuple[str, ...],
    require_citations: bool = False,
    rag_collections: tuple[str, ...] = (),
    output_model: type[BaseModel] = EvidenceAgentResult,
    allowed_sources: tuple[str, ...] = (
        "task_contract",
        "upstream_artifact",
        "tool_evidence",
    ),
    memory_read_scopes: tuple[str, ...] = (),
) -> AgentContract:
    manifest = prompt.manifest
    contract = AgentContract(
        schema_version="deskpilot.agent-contract.v1",
        agent_id=agent_id,
        version="1.0.0",
        kind=kind,
        display_name=display_name,
        description=description,
        provides=provides,
        prompt_package=PromptPackageRef(
            package_id=manifest.package_id,
            version=manifest.version,
            renderer_version=manifest.renderer_version,
            digest=prompt.digest,
        ),
        input_schema=AgentReferenceInput.model_json_schema(),
        output_schema=output_model.model_json_schema(),
        tool_policy=tool_policy,
        handoff_policy=handoff,
        model_policy=AgentModelPolicy(
            role=role,
            allowed_locations=(ModelLocation.LOCAL, ModelLocation.CLOUD),
            allowed_privacy_modes=(
                "local_only",
                "local_preferred",
                "balanced",
            ),
            requirements=ModelCapabilityRequirements(
                structured_output=True,
                strict_json_schema=True,
                min_context_tokens=8_192,
            ),
        ),
        context_policy=AgentContextPolicy(
            allowed_sources=allowed_sources,
            memory_read_scopes=memory_read_scopes,
            rag_collections=rag_collections,
        ),
        budget_policy=budget,
        result_policy=AgentResultPolicy(
            required_evidence=required_evidence,
            require_citations=require_citations,
            allow_unreferenced_claims=False,
        ),
    )
    return load_agent_contract(contract.model_dump_json().encode("utf-8"))
