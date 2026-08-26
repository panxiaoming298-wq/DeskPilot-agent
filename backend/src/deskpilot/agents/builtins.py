"""Fixed read-only Agent registrations for the frozen v1 Registry."""

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.application.agent_registry import (
    AgentModelAdmissionPolicy,
    AgentRegistration,
    AgentRegistry,
    AgentReleaseActivationPolicyPort,
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
from deskpilot.domain.agent_loop import (
    CoordinatorLoopDecision,
    DynamicCoordinatorLoopDecision,
    WorkspaceBoundedCodingCoordinatorDecision,
    WorkspaceLoopDecision,
    WorkspacePatchLoopDecision,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelLocation,
    ModelProviderDescriptor,
    ModelRole,
    PrivacyMode,
)
from deskpilot.domain.research import ResearchAgentDecision, ResearchLoopDecision
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.domain.turn_planning import TurnPlannerDecision, TurnPlannerInput
from deskpilot.domain.workspace_coding_explorations import (
    WorkspaceCodingExplorationDecision,
)


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
    model_admissions: AgentModelAdmissionPolicy | None = None,
    release_activations: AgentReleaseActivationPolicyPort | None = None,
) -> AgentRegistry:
    prompt_root = Path(__file__).parent / "prompts"
    computer_prompt = load_prompt_package(prompt_root, "computer_observer.json")
    knowledge_prompt = load_prompt_package(prompt_root, "knowledge_researcher.json")
    web_prompt = load_prompt_package(prompt_root, "web_researcher.json")
    web_loop_prompt = load_prompt_package(prompt_root, "web_researcher_loop.json")
    workspace_prompt = load_prompt_package(prompt_root, "workspace_reader_loop.json")
    workspace_prompt_v2 = load_prompt_package(prompt_root, "workspace_reader_loop_v2.json")
    workspace_tester_prompt = load_prompt_package(prompt_root, "workspace_tester_loop.json")
    coordinator_prompt = load_prompt_package(prompt_root, "workspace_coordinator_loop.json")
    dynamic_coordinator_prompt = load_prompt_package(
        prompt_root, "workspace_dynamic_coordinator_loop.json"
    )
    workspace_patch_planner_prompt = load_prompt_package(
        prompt_root, "workspace_patch_planner_loop.json"
    )
    workspace_coding_explorer_prompt = load_prompt_package(
        prompt_root, "workspace_coding_explorer.json"
    )
    turn_planner_prompt = load_prompt_package(prompt_root, "turn_planner.json")
    cloud_turn_planner_prompt = load_prompt_package(
        prompt_root, "turn_planner_cloud_v2.json"
    )
    cloud_coordinator_prompt = load_prompt_package(
        prompt_root, "workspace_dynamic_coordinator_cloud_v2.json"
    )
    cloud_patch_planner_prompt = load_prompt_package(
        prompt_root, "workspace_patch_planner_cloud_v2.json"
    )
    release_reader_prompt = load_prompt_package(
        prompt_root, "workspace_reader_release_v2.json"
    )
    release_tester_prompt = load_prompt_package(
        prompt_root, "workspace_tester_release_v2.json"
    )
    synthesizer_prompt = load_prompt_package(prompt_root, "task_synthesizer.json")
    disk = tool_registry.resolve("computer.disk_usage", "1.0.0").contract
    synth_ref = AgentHandoffRef(agent_id="builtin.task_synthesizer", version="1.0.0")
    computer_ref = AgentHandoffRef(agent_id="builtin.computer_observer", version="1.0.0")
    knowledge_ref = AgentHandoffRef(agent_id="builtin.knowledge_researcher", version="1.0.0")
    coordinator_ref = AgentHandoffRef(agent_id="builtin.workspace_coordinator", version="1.0.0")
    dynamic_coordinator_ref = AgentHandoffRef(
        agent_id="builtin.workspace_coordinator", version="1.1.0"
    )
    workspace_reader_ref = AgentHandoffRef(agent_id="builtin.workspace_reader", version="1.1.0")
    dynamic_workspace_reader_ref = AgentHandoffRef(
        agent_id="builtin.workspace_reader", version="1.2.0"
    )
    workspace_tester_ref = AgentHandoffRef(agent_id="builtin.workspace_tester", version="1.0.0")
    workspace_patch_planner_ref = AgentHandoffRef(
        agent_id="builtin.workspace_patch_planner", version="1.0.0"
    )
    cloud_coordinator_ref = AgentHandoffRef(
        agent_id="builtin.workspace_coordinator", version="2.0.0"
    )
    cloud_patch_planner_ref = AgentHandoffRef(
        agent_id="builtin.workspace_patch_planner", version="2.0.0"
    )
    release_workspace_reader_ref = AgentHandoffRef(
        agent_id="builtin.workspace_reader", version="2.0.0"
    )
    release_workspace_tester_ref = AgentHandoffRef(
        agent_id="builtin.workspace_tester", version="2.0.0"
    )

    registry = AgentRegistry(model_admissions, release_activations)
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.turn_planner",
                version="1.0.0",
                kind=AgentKind.WORKER,
                display_name="Turn Planner",
                description=(
                    "只从服务器预编译的 opaque Capability Offer 提出任务步骤；"
                    "不能授予能力、权限或审批。"
                ),
                provides=("turn.plan.propose.v1",),
                prompt=turn_planner_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.PLANNER,
                budget=AgentBudgetPolicy(
                    max_model_calls=1,
                    max_tool_calls=0,
                    max_input_tokens=32_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("server_capability_offer", "turn_planning_adjudication"),
                input_model=TurnPlannerInput,
                output_model=TurnPlannerDecision,
                allowed_sources=("conversation_message", "server_capability_offer"),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=TurnPlannerInput,
            output_model=TurnPlannerDecision,
            prompt_package=turn_planner_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_coding_explorer",
                version="1.0.0",
                kind=AgentKind.WORKER,
                display_name="Workspace Coding Explorer",
                description=(
                    "只从服务器封存的项目快照提议 2～8 个候选文件；输出不授予"
                    "读取、Patch、测试、Git 或审批权限。"
                ),
                provides=("workspace.coding.explore.propose.v1",),
                prompt=workspace_coding_explorer_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.PLANNER,
                budget=AgentBudgetPolicy(
                    max_model_calls=1,
                    max_tool_calls=0,
                    max_input_tokens=24_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=0,
                ),
                required_evidence=("workspace_exploration_snapshot",),
                output_model=WorkspaceCodingExplorationDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceCodingExplorationDecision,
            prompt_package=workspace_coding_explorer_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.turn_planner",
                version="2.0.0",
                kind=AgentKind.WORKER,
                display_name="Cloud Turn Planner Candidate",
                description=(
                    "只从服务器预编译的 opaque Capability Offer 提出任务步骤；"
                    "Release 和 Admission 均闭合前不可派发。"
                ),
                provides=("turn.plan.propose.v1",),
                prompt=cloud_turn_planner_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.PLANNER,
                budget=AgentBudgetPolicy(
                    max_model_calls=1,
                    max_tool_calls=0,
                    max_input_tokens=32_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=200_000,
                ),
                required_evidence=("server_capability_offer", "turn_planning_adjudication"),
                input_model=TurnPlannerInput,
                output_model=TurnPlannerDecision,
                allowed_sources=("conversation_message", "server_capability_offer"),
                allowed_locations=(ModelLocation.CLOUD,),
                allowed_privacy_modes=("balanced", "quality_first"),
            ),
            input_model=TurnPlannerInput,
            output_model=TurnPlannerDecision,
            prompt_package=cloud_turn_planner_prompt,
            source="builtin_cloud_candidate",
            requires_release_activation=True,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_patch_planner",
                version="1.0.0",
                kind=AgentKind.WORKER,
                display_name="Workspace Patch Planner",
                description=(
                    "读取一个服务器绑定文件并提出一次精确替换；不能应用补丁或选择测试命令。"
                ),
                provides=("workspace.patch.propose.v1",),
                prompt=workspace_patch_planner_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(dynamic_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=24_000,
                    max_output_tokens=3_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_read_observation", "user_patch_confirmation"),
                output_model=WorkspacePatchLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspacePatchLoopDecision,
            prompt_package=workspace_patch_planner_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_patch_planner",
                version="2.0.0",
                kind=AgentKind.WORKER,
                display_name="Cloud Workspace Patch Planner Candidate",
                description=(
                    "读取服务器绑定证据并提出精确 Patch；Release、Admission 和用户确认"
                    "均不能被模型输出替代。"
                ),
                provides=("workspace.patch.propose.v1",),
                prompt=cloud_patch_planner_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(cloud_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=24_000,
                    max_output_tokens=3_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=300_000,
                ),
                required_evidence=("workspace_read_observation", "user_patch_confirmation"),
                output_model=WorkspacePatchLoopDecision,
                allowed_sources=("task_contract", "conversation_message", "tool_evidence"),
                allowed_locations=(ModelLocation.CLOUD,),
                allowed_privacy_modes=("balanced", "quality_first"),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspacePatchLoopDecision,
            prompt_package=cloud_patch_planner_prompt,
            source="builtin_cloud_candidate",
            requires_release_activation=True,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_tester",
                version="2.0.0",
                kind=AgentKind.WORKER,
                display_name="Release Workspace Tester Companion",
                description="执行 cloud Coordinator 服务器绑定的本地固定测试。",
                provides=("workspace.python.test.v1", "workspace.node.test.v1"),
                prompt=release_tester_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(cloud_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_test_observation",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=("task_contract", "conversation_message", "tool_evidence"),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=release_tester_prompt,
            source="builtin_release_companion",
            requires_release_activation=True,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_reader",
                version="2.0.0",
                kind=AgentKind.WORKER,
                display_name="Release Workspace Reader Companion",
                description="执行 cloud Coordinator 服务器绑定的本地只读节点。",
                provides=("workspace.file.read.v1", "workspace.directory.read.v1"),
                prompt=release_reader_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(cloud_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_read_observation",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=("task_contract", "conversation_message", "tool_evidence"),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=release_reader_prompt,
            source="builtin_release_companion",
            requires_release_activation=True,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_coordinator",
                version="2.0.0",
                kind=AgentKind.SYNTHESIZER,
                display_name="Cloud Dynamic Workspace Coordinator Candidate",
                description=(
                    "提出服务器可验证的候选任务 DAG，只消费 verified Child join；"
                    "Release 和 Admission 闭合前不可派发。"
                ),
                provides=("workspace.dynamic.coordinate.v1",),
                prompt=cloud_coordinator_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(
                    may_delegate_to=(
                        release_workspace_reader_ref,
                        release_workspace_tester_ref,
                        cloud_patch_planner_ref,
                    ),
                    max_outgoing_handoffs=4,
                    max_depth=1,
                ),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=12_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=300_000,
                    max_handoffs=4,
                ),
                required_evidence=("verified_task_graph",),
                output_model=DynamicCoordinatorLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "verified_child_result",
                ),
                allowed_locations=(ModelLocation.CLOUD,),
                allowed_privacy_modes=("balanced", "quality_first"),
            ),
            input_model=AgentReferenceInput,
            output_model=DynamicCoordinatorLoopDecision,
            prompt_package=cloud_coordinator_prompt,
            source="builtin_cloud_candidate",
            requires_release_activation=True,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_tester",
                version="1.0.0",
                kind=AgentKind.WORKER,
                display_name="Workspace Tester",
                description=(
                    "执行服务器绑定的固定 pytest 或 node:test 文件，不接受 executable 或 argv。"
                ),
                provides=("workspace.python.test.v1", "workspace.node.test.v1"),
                prompt=workspace_tester_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(dynamic_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_test_observation",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=workspace_tester_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_reader",
                version="1.2.0",
                kind=AgentKind.WORKER,
                display_name="Workspace Reader",
                description="执行服务器动态任务图中绑定的工作区文件或目录只读节点。",
                provides=("workspace.file.read.v1", "workspace.directory.read.v1"),
                prompt=workspace_prompt_v2,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(dynamic_coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_read_observation",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=workspace_prompt_v2,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_coordinator",
                version="1.1.0",
                kind=AgentKind.SYNTHESIZER,
                display_name="Dynamic Workspace Coordinator",
                description=("提出一个完整的候选只读子任务 DAG，并只消费服务器验证后的 join。"),
                provides=("workspace.dynamic.coordinate.v1",),
                prompt=dynamic_coordinator_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(
                    may_delegate_to=(
                        dynamic_workspace_reader_ref,
                        workspace_tester_ref,
                        workspace_patch_planner_ref,
                    ),
                    max_outgoing_handoffs=4,
                    max_depth=1,
                ),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=12_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=4,
                ),
                required_evidence=("verified_task_graph",),
                output_model=DynamicCoordinatorLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "verified_child_result",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=DynamicCoordinatorLoopDecision,
            prompt_package=dynamic_coordinator_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_bounded_coordinator",
                version="1.0.0",
                kind=AgentKind.SYNTHESIZER,
                display_name="Bounded Workspace Coding Coordinator",
                description=(
                    "确认服务器预编译的 3～8 文件编码 DAG；不得创建路径、节点或权限。"
                ),
                provides=("workspace.dynamic.coordinate.v1",),
                prompt=dynamic_coordinator_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=12_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=0,
                ),
                required_evidence=("server_sealed_bounded_coding_graph",),
                output_model=WorkspaceBoundedCodingCoordinatorDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceBoundedCodingCoordinatorDecision,
            prompt_package=dynamic_coordinator_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_bounded_coordinator",
                version="1.1.0",
                kind=AgentKind.SYNTHESIZER,
                display_name="Bounded Workspace Coding Coordinator",
                description=(
                    "确认服务器预编译的 3～8 文件编码 DAG；不得创建路径、节点或权限。"
                ),
                provides=("workspace.dynamic.coordinate.v1",),
                prompt=dynamic_coordinator_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=12_000,
                    max_output_tokens=3_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=0,
                ),
                required_evidence=("server_sealed_bounded_coding_graph",),
                output_model=WorkspaceBoundedCodingCoordinatorDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceBoundedCodingCoordinatorDecision,
            prompt_package=dynamic_coordinator_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_reader",
                version="1.1.0",
                kind=AgentKind.WORKER,
                display_name="Workspace Reader",
                description="通过持久化受限 Route Loop 读取工作区文件或直接子目录。",
                provides=("workspace.file.read.v1", "workspace.directory.read.v1"),
                prompt=workspace_prompt_v2,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(may_receive_from=(coordinator_ref,)),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_read_observation",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=workspace_prompt_v2,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_coordinator",
                kind=AgentKind.SYNTHESIZER,
                display_name="Workspace Coordinator",
                description=(
                    "提议一个服务器预编译的只读 Workspace Reader 子任务，只消费已经验证的子结果。"
                ),
                provides=("workspace.directory.coordinate.v1",),
                prompt=coordinator_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(
                    may_delegate_to=(workspace_reader_ref,),
                    max_outgoing_handoffs=1,
                    max_depth=1,
                ),
                role=ModelRole.SUMMARIZER,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=0,
                    max_input_tokens=12_000,
                    max_output_tokens=1_000,
                    max_wall_seconds=60,
                    max_retries=0,
                    max_cost_micros=100_000,
                    max_handoffs=1,
                ),
                required_evidence=("verified_child_result",),
                output_model=CoordinatorLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "verified_child_result",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=CoordinatorLoopDecision,
            prompt_package=coordinator_prompt,
        )
    )
    registry.register(
        AgentRegistration(
            contract=_contract(
                agent_id="builtin.workspace_reader",
                kind=AgentKind.WORKER,
                display_name="Workspace Reader",
                description="通过持久化受限 Route Loop 读取一个工作区文本文件。",
                provides=("workspace.file.read.v1",),
                prompt=workspace_prompt,
                tool_policy=AgentToolPolicy(max_risk_level=ToolRiskLevel.R0),
                handoff=AgentHandoffPolicy(),
                role=ModelRole.TOOL_AGENT,
                budget=AgentBudgetPolicy(
                    max_model_calls=2,
                    max_tool_calls=1,
                    max_input_tokens=20_000,
                    max_output_tokens=2_000,
                    max_wall_seconds=90,
                    max_retries=0,
                    max_cost_micros=100_000,
                ),
                required_evidence=("workspace_file_version",),
                output_model=WorkspaceLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "tool_evidence",
                ),
                allowed_locations=(ModelLocation.LOCAL,),
            ),
            input_model=AgentReferenceInput,
            output_model=WorkspaceLoopDecision,
            prompt_package=workspace_prompt,
        )
    )
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
                version="1.1.0",
                kind=AgentKind.WORKER,
                display_name="Web Researcher",
                description="通过持久化受限 Route Loop 取得公开来源并提出待验证 Claim。",
                provides=("research.read.v1",),
                prompt=web_loop_prompt,
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
                output_model=ResearchLoopDecision,
                allowed_sources=(
                    "task_contract",
                    "conversation_message",
                    "working_memory",
                    "long_term_memory",
                    "compaction_snapshot",
                    "external_untrusted_page_snapshot",
                ),
                memory_read_scopes=("user",),
            ),
            input_model=AgentReferenceInput,
            output_model=ResearchLoopDecision,
            prompt_package=web_loop_prompt,
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
                    "compaction_snapshot",
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
    version: str = "1.0.0",
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
    input_model: type[BaseModel] = AgentReferenceInput,
    allowed_sources: tuple[str, ...] = (
        "task_contract",
        "upstream_artifact",
        "tool_evidence",
    ),
    memory_read_scopes: tuple[str, ...] = (),
    allowed_locations: tuple[ModelLocation, ...] = (
        ModelLocation.LOCAL,
        ModelLocation.CLOUD,
    ),
    allowed_privacy_modes: tuple[PrivacyMode, ...] = (
        "local_only",
        "local_preferred",
        "balanced",
    ),
) -> AgentContract:
    manifest = prompt.manifest
    contract = AgentContract(
        schema_version="deskpilot.agent-contract.v1",
        agent_id=agent_id,
        version=version,
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
        input_schema=input_model.model_json_schema(),
        output_schema=output_model.model_json_schema(),
        tool_policy=tool_policy,
        handoff_policy=handoff,
        model_policy=AgentModelPolicy(
            role=role,
            allowed_locations=allowed_locations,
            allowed_privacy_modes=allowed_privacy_modes,
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
