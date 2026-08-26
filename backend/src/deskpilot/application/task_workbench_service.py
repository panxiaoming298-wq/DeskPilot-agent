"""Server-owned phase-76 Conversation/Research/Artifact projection."""

import asyncio
from typing import Literal, Protocol, cast

from sqlalchemy import select

from deskpilot.application.agent_execution_runtime import (
    AgentExecutionRuntime,
    AgentRuntimeError,
)
from deskpilot.application.agent_supervisor_runtime import AgentSupervisorError
from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactDeliveryRuntime,
)
from deskpilot.application.artifact_export_runtime import (
    ArtifactExportError,
    ArtifactExportRuntime,
)
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.context_memory_runtime import ContextMemoryRuntime
from deskpilot.application.long_term_memory_runtime import (
    LongTermMemoryError,
    LongTermMemoryRuntime,
)
from deskpilot.application.multi_step_plan_runtime import (
    MultiStepPlanRuntime,
    MultiStepPlanRuntimeError,
)
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
    PlanningNotFoundError,
)
from deskpilot.application.plan_compiler import (
    knowledge_lookup_contract,
    knowledge_lookup_draft,
    mcp_text_metrics_contract,
    mcp_text_metrics_draft,
    research_to_html_contract,
    research_to_html_draft,
    workspace_agent_patch_test_contract,
    workspace_agent_patch_test_draft,
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
    workspace_node_test_contract,
    workspace_node_test_draft,
    workspace_patch_bundle_contract,
    workspace_patch_bundle_draft,
    workspace_python_test_contract,
    workspace_python_test_draft,
    workspace_snapshot_check_contract,
    workspace_snapshot_check_draft,
)
from deskpilot.application.research_runtime import ResearchRuntime, ResearchRuntimeError
from deskpilot.application.route_recipe_catalog import RouteRecipeCatalog
from deskpilot.application.task_loop_activation_runtime import (
    TaskLoopActivationError,
    TaskLoopActivationRuntime,
)
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
    TaskLoopExecutionCoordinatorError,
)
from deskpilot.application.task_service import TaskNotFoundError, TaskService
from deskpilot.application.turn_planner_runtime import (
    BoundTurnRoute,
    TurnPlannerRuntime,
    TurnPlannerRuntimeError,
)
from deskpilot.application.turn_router import (
    FollowupResolution,
    RouteCandidate,
    TurnRouter,
    TurnRouterError,
)
from deskpilot.application.workspace_agent_runtime import (
    WorkspaceAgentOutcome,
    WorkspaceAgentRuntime,
    WorkspaceAgentRuntimeError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_replanning import (
    BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT,
    AgentRepairLoopStatus,
    AgentReplanBudgetTotals,
    AgentReplanContinuationIntent,
    classify_agent_replan_continuation,
    condition_replan_generation_limit,
)
from deskpilot.domain.agent_runtime import (
    ExecutionNodeStatus,
    ExecutionRunPage,
    ExecutionRunStatus,
)
from deskpilot.domain.artifact_runtime import DeliveryManifestRead
from deskpilot.domain.coding_tools import GitCommitPreview, GitCommitReceipt
from deskpilot.domain.command_profiles import CommandProfileId
from deskpilot.domain.context_memory import (
    ConversationMessageRead,
    CreateConversationMessageRequest,
    CreateConversationRequest,
    DataClassification,
)
from deskpilot.domain.research import (
    CitationEvidence,
    PageSnapshot,
    ResearchClaim,
    ResearchSessionRead,
    SearchCallRead,
    SearchHit,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.domain.task_loop import TaskLoop, TaskLoopWorkbenchRead
from deskpilot.domain.task_loop_execution import (
    TaskLoopExecutionRead,
    TaskLoopExecutionWorkbenchRead,
)
from deskpilot.domain.task_plans import (
    ExecutablePlanPage,
    PlanningStateRead,
    TaskContractVersionRead,
)
from deskpilot.domain.task_workbench import (
    ArtifactExportRead,
    ContinueConversationTurn,
    CreateConversationTurn,
    CreateResearchWorkbenchTask,
    TaskWorkbenchRead,
    TurnRouteDecision,
    TurnRouteRead,
    TurnRouteStatus,
    WorkbenchAction,
    WorkbenchActionRead,
    WorkbenchStage,
)
from deskpilot.domain.turn_planning import TurnPlanningRead, TurnPlanningWorkbenchRead
from deskpilot.domain.workspace_files import (
    WorkspaceDirectoryRead,
    WorkspaceEditReceipt,
    WorkspaceFileRead,
    WorkspacePatchPreview,
    WorkspacePatchReceipt,
    WorkspacePathOperationReceipt,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ResearchCitationRecord,
    ResearchClaimRecord,
    ResearchPageSnapshotRecord,
    ResearchSearchCallRecord,
    ResearchSessionRecord,
    TaskArtifactWorkspaceRecord,
)


class TaskWorkbenchError(RuntimeError):
    code = "TASK_WORKBENCH_ERROR"


class TaskWorkbenchNotFoundError(TaskWorkbenchError):
    code = "TASK_WORKBENCH_NOT_FOUND"


class TaskWorkbenchConflictError(TaskWorkbenchError):
    code = "TASK_WORKBENCH_CONFLICT"


class WorkbenchAutoAdvancePort(Protocol):
    async def schedule(self, task_id: str, projection_digest: str) -> None: ...

    async def cancel(self, task_id: str) -> None: ...


class TaskWorkbenchService:
    _PROJECTION_READ_ATTEMPTS = 4

    def __init__(
        self,
        database: Database,
        tasks: TaskService,
        context: ContextMemoryRuntime,
        planning: PlanCompilationService,
        capabilities: CapabilityCatalog,
        execution: AgentExecutionRuntime,
        research: ResearchRuntime | None,
        workspace_agents: WorkspaceAgentRuntime | None,
        memory: LongTermMemoryRuntime,
        artifacts: ArtifactDeliveryRuntime,
        exports: ArtifactExportRuntime,
        router: TurnRouter,
        turn_planner: TurnPlannerRuntime | None = None,
        task_loop: MultiStepPlanRuntime | None = None,
        task_loop_activation: TaskLoopActivationRuntime | None = None,
        task_loop_execution: TaskLoopExecutionCoordinator | None = None,
        command_profile_ids: frozenset[CommandProfileId] = frozenset(),
    ) -> None:
        self._database = database
        self._tasks = tasks
        self._context = context
        self._planning = planning
        self._capabilities = capabilities
        self._execution = execution
        self._research = research
        self._workspace_agents = workspace_agents
        self._memory = memory
        self._artifacts = artifacts
        self._exports = exports
        self._router = router
        self._turn_planner = turn_planner
        self._task_loop = task_loop
        self._task_loop_activation = task_loop_activation
        self._task_loop_execution = task_loop_execution
        self._command_profile_ids = command_profile_ids
        self._auto_advance: WorkbenchAutoAdvancePort | None = None

    def bind_auto_advance(self, auto_advance: WorkbenchAutoAdvancePort) -> None:
        if self._auto_advance is not None:
            raise RuntimeError("Task Workbench auto-advance runtime is already bound")
        self._auto_advance = auto_advance

    async def turn_planner_recoverable_task_ids(
        self,
        *,
        limit: int = 1_000,
    ) -> tuple[str, ...]:
        if self._turn_planner is None:
            return ()
        return await self._turn_planner.recoverable_task_ids(limit=limit)

    async def task_loop_recoverable_task_ids(
        self,
        *,
        limit: int = 1_000,
    ) -> tuple[str, ...]:
        planned = (
            await self._task_loop_activation.recoverable_task_ids(limit=limit)
            if self._task_loop_activation is not None
            else ()
        )
        deferred = (
            await self._task_loop.recoverable_task_ids(limit=limit)
            if self._task_loop is not None
            else ()
        )
        return tuple(dict.fromkeys((*planned, *deferred)))[:limit]

    @staticmethod
    def automatic_action(workbench: TaskWorkbenchRead) -> WorkbenchAction | None:
        """Return only an action the server may run without fresh user authority."""

        allowed = {
            WorkbenchAction.INTERPRET_TURN,
            WorkbenchAction.PLAN_TASK_LOOP,
            WorkbenchAction.ADVANCE_TASK_LOOP,
            WorkbenchAction.START_EXECUTION,
            WorkbenchAction.RUN_RESEARCH,
            WorkbenchAction.VERIFY_CLAIMS,
            WorkbenchAction.BUILD_ARTIFACT,
            WorkbenchAction.VERIFY_BROWSER,
            WorkbenchAction.FINALIZE_DELIVERY,
            WorkbenchAction.EXECUTE_ROUTE,
            WorkbenchAction.REPLAN_FAILED_EXECUTION,
        }
        for item in workbench.actions:
            if not item.enabled or item.action not in allowed:
                continue
            if (
                item.action is WorkbenchAction.REPLAN_FAILED_EXECUTION
                and workbench.route is not None
                and workbench.route.route_id == "workspace_dynamic_patch_test"
            ):
                continue
            return item.action
        return None

    async def _schedule_automatic(self, workbench: TaskWorkbenchRead) -> TaskWorkbenchRead:
        if self._auto_advance is not None and self.automatic_action(workbench) is not None:
            await self._auto_advance.schedule(
                workbench.task.task_id,
                workbench.projection_digest,
            )
        return workbench

    async def create(self, command: CreateResearchWorkbenchTask) -> TaskWorkbenchRead:
        return await self._schedule_automatic(
            await self._create_task(
                goal=command.goal,
                privacy_mode=command.privacy_mode,
                constraints=command.constraints,
            )
        )

    async def create_turn(self, command: CreateConversationTurn) -> TaskWorkbenchRead:
        return await self._schedule_automatic(
            await self._create_turn_task(
                goal=command.message,
                privacy_mode=command.privacy_mode,
                constraints=command.constraints,
            )
        )

    async def continue_turn(
        self, task_id: str, command: ContinueConversationTurn
    ) -> TaskWorkbenchRead:
        previous = await self.get(task_id)
        if previous.turn_planning is not None and previous.turn_planning.run.status in {
            "prepared",
            "dispatching",
        }:
            if self._auto_advance is not None:
                await self._auto_advance.cancel(previous.task.task_id)
            if self._turn_planner is not None:
                await self._turn_planner.cancel(previous.task.task_id)
            await self._add_assistant_message(
                previous.task.task_id,
                "收到新的任务要求。我已封存旧解释租约；迟到的模型结果不能再绑定路线。",
            )
        conversation_id = previous.task.conversation_id
        if conversation_id is None:
            raise TaskWorkbenchConflictError("Task has no conversation to continue")
        if previous.task.privacy_mode not in {"local_preferred", "balanced"}:
            raise TaskWorkbenchConflictError(
                "Task privacy mode is incompatible with the research conversation"
            )
        privacy_mode = cast(Literal["local_preferred", "balanced"], previous.task.privacy_mode)
        continuation_code = classify_agent_replan_continuation(command.message)
        if (
            continuation_code is not None
            and previous.route is not None
            and previous.route.route_id == "workspace_dynamic_patch_test"
        ):
            if not self._replan_action_enabled(previous):
                raise TaskWorkbenchConflictError(
                    "Patch repair generation or cross-generation budget limit was reached"
                )
            continuation = await self._record_replan_continuation(
                task_id,
                command.message,
                requested_via="conversation_turn",
            )
            return await self._schedule_automatic(
                await self._replan_failed_execution(previous, continuation)
            )
        candidate = self._router.classify(command.message)
        resolution: FollowupResolution | None = None
        if previous.route is not None:
            resolution = self._router.resolve_followup(
                previous.route,
                previous.task.goal,
                command.message,
            )
            if resolution is not None:
                candidate = resolution.candidate
        run = previous.executions.runs[-1] if previous.executions.runs else None
        resolves_agent_input = bool(
            resolution is not None and resolution.rule == "agent_workspace_file_path"
        )
        task_loop_amended = bool(
            isinstance(previous.task_loop, TaskLoopExecutionWorkbenchRead)
            and previous.task_loop.execution_status
            not in {None, "failed", "succeeded", "cancelled"}
            and previous.task_loop.recoverable
        )
        if task_loop_amended:
            if self._task_loop_execution is None:
                raise TaskWorkbenchConflictError(
                    "Task Loop amendment runtime is unavailable"
                )
            if self._auto_advance is not None:
                await self._auto_advance.cancel(previous.task.task_id)
            sealed = await self._task_loop_execution.cancel_for_amendment(task_id)
            if (
                sealed is None
                or sealed.execution is None
                or sealed.execution.status != "cancelled"
            ):
                raise TaskWorkbenchConflictError(
                    "Old Task Loop generation was not sealed"
                )
            await self._add_assistant_message(
                previous.task.task_id,
                "收到新的任务要求。旧 TaskLoop generation 与全部未完成租约已封存；"
                "迟到结果不能进入新任务，接下来按新指令重新规划。",
            )
        if (
            run is not None
            and run.status.value in {"active", "awaiting_verification", "paused"}
            and not resolves_agent_input
            and not task_loop_amended
        ):
            if self._auto_advance is not None:
                await self._auto_advance.cancel(previous.task.task_id)
            await self._execution.cancel(run.run_id)
            await self._add_assistant_message(
                previous.task.task_id,
                "收到新的任务要求。我已停止旧运行并使未完成凭证失效，接下来会按新指令重新规划。",
            )
        replacement = await self._create_turn_task(
            goal=command.message,
            privacy_mode=privacy_mode,
            constraints=tuple(previous.task.constraints),
            conversation_id=conversation_id,
            candidate=candidate,
            resolution=resolution,
        )
        if task_loop_amended:
            assert self._task_loop_execution is not None
            await self._task_loop_execution.bind_conversation_amendment(
                task_id,
                replacement.task.task_id,
            )
        if resolves_agent_input:
            if run is None or self._workspace_agents is None:
                raise TaskWorkbenchConflictError("Workspace Agent input runtime is unavailable")
            await self._workspace_agents.resolve_input(task_id, replacement.task.task_id)
            if self._auto_advance is not None:
                await self._auto_advance.cancel(previous.task.task_id)
            await self._execution.cancel(run.run_id)
            await self._add_assistant_message(
                task_id,
                "已将你的补充绑定到新的不可变任务，并使旧运行的执行租约失效。",
            )
        return await self._schedule_automatic(replacement)

    async def _create_turn_task(
        self,
        *,
        goal: str,
        privacy_mode: Literal["local_preferred", "balanced"],
        constraints: tuple[str, ...],
        conversation_id: str | None = None,
        candidate: RouteCandidate | None = None,
        resolution: FollowupResolution | None = None,
    ) -> TaskWorkbenchRead:
        candidate = candidate or self._router.classify(goal)
        rules_routed = candidate.decision is TurnRouteDecision.ROUTED
        if candidate.route_id == "research_to_html" and not self._research_enabled():
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="RESEARCH_RUNTIME_DISABLED",
            )
        if candidate.route_id in {
            "workspace_file_read",
            "workspace_file_replace",
            "workspace_file_create",
            "workspace_file_rename",
            "workspace_directory_list",
            "workspace_directory_analyze",
            "workspace_snapshot_check",
            "workspace_python_test",
            "workspace_node_test",
            "workspace_agent_patch_test",
            "workspace_dynamic_patch_test",
        } and not (self._router.workspace_enabled):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_RUNTIME_DISABLED",
            )
        if candidate.route_id in {
            "workspace_patch_bundle",
            "workspace_agent_patch_test",
            "workspace_dynamic_patch_test",
        } and not (self._router.workspace_patch_enabled):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_RUNTIME_DISABLED",
            )
        if (
            candidate.route_id
            in {
                "workspace_agent_patch_test",
                "workspace_dynamic_patch_test",
            }
            and self._workspace_agents is None
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_AGENT_RUNTIME_DISABLED",
            )
        if candidate.route_id in {"workspace_file_create", "workspace_file_rename"} and not (
            self._router.workspace_path_operation_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_PATH_OPERATION_RUNTIME_DISABLED",
            )
        if candidate.route_id == "workspace_snapshot_check" and not (
            self._router.workspace_check_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_CHECK_RUNTIME_DISABLED",
            )
        if candidate.route_id == "workspace_python_test" and not (
            self._router.workspace_python_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_PYTHON_TEST_RUNTIME_DISABLED",
            )
        if (
            candidate.route_id in {"workspace_agent_patch_test", "workspace_dynamic_patch_test"}
            and candidate.parameters.get("test_kind") == "python"
            and not self._router.workspace_python_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_PYTHON_TEST_RUNTIME_DISABLED",
            )
        if (
            candidate.route_id == "workspace_directory_analyze"
            and "python_test_path" in candidate.parameters
            and not self._router.workspace_python_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_PYTHON_TEST_RUNTIME_DISABLED",
            )
        if candidate.route_id == "workspace_node_test" and not (
            self._router.workspace_node_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_NODE_TEST_RUNTIME_DISABLED",
            )
        if (
            candidate.route_id in {"workspace_agent_patch_test", "workspace_dynamic_patch_test"}
            and candidate.parameters.get("test_kind") == "node"
            and not self._router.workspace_node_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_NODE_TEST_RUNTIME_DISABLED",
            )
        if (
            candidate.route_id == "workspace_directory_analyze"
            and "node_test_path" in candidate.parameters
            and not self._router.workspace_node_test_enabled
        ):
            candidate = RouteCandidate(
                decision=TurnRouteDecision.UNSUPPORTED,
                route_id=None,
                parameters={},
                reason_code="WORKSPACE_NODE_TEST_RUNTIME_DISABLED",
            )
        if resolution is not None and resolution.candidate != candidate:
            resolution = None
        if conversation_id is None:
            conversation = await self._context.create_conversation(
                CreateConversationRequest(title=goal.strip()[:200])
            )
            conversation_id = conversation.conversation_id
        task = await self._tasks.create_task(
            TaskCreate(
                conversation_id=conversation_id,
                goal=goal,
                privacy_mode=privacy_mode,
                constraints=list(constraints),
            )
        )
        user_message = await self._context.add_message(
            conversation_id,
            CreateConversationMessageRequest(
                role="user",
                content=goal,
                task_id=task.task_id,
                classification=DataClassification.INTERNAL,
            ),
        )
        if candidate.decision is not TurnRouteDecision.ROUTED:
            status = TurnRouteStatus.NOT_APPLICABLE
        elif candidate.route_id in {
            "workspace_file_replace",
            "workspace_patch_bundle",
            "workspace_file_create",
            "workspace_file_rename",
        }:
            status = TurnRouteStatus.NEEDS_USER_ACTION
        elif candidate.route_id == "mcp_text_metrics" and not await self._router.mcp_enabled():
            status = TurnRouteStatus.NEEDS_USER_ACTION
        else:
            status = TurnRouteStatus.READY
        fallback_route = await self._router.create(
            task_id=task.task_id,
            conversation_id=conversation_id,
            user_message_id=user_message.message_id,
            message_digest=user_message.message_digest,
            candidate=candidate,
            status=status,
            resolution=resolution,
        )
        interpreting = False
        if not rules_routed and self._turn_planner is not None and self._turn_planner.enabled:
            try:
                await self._turn_planner.prepare(
                    task.task_id,
                    user_message.message_id,
                    fallback_route,
                    eligible_variant_keys=self._turn_planner_variant_keys(),
                )
                interpreting = True
            except TurnPlannerRuntimeError:
                # Preparation happens before dispatch. If no safe local Offer can
                # be reserved, preserve the exact deterministic fallback.
                interpreting = False
        preparation_error: str | None = None
        if candidate.route_id == "workspace_file_replace":
            try:
                await self._router.prepare_workspace_edit(task.task_id)
            except TurnRouterError as error:
                preparation_error = str(error)
        elif candidate.route_id == "workspace_patch_bundle":
            try:
                await self._router.prepare_workspace_patch(task.task_id)
            except TurnRouterError as error:
                preparation_error = str(error)
        elif candidate.route_id in {"workspace_file_create", "workspace_file_rename"}:
            try:
                await self._router.prepare_workspace_path_operation(task.task_id)
            except TurnRouterError as error:
                preparation_error = str(error)
        if (
            not interpreting
            and candidate.decision is TurnRouteDecision.ROUTED
            and preparation_error is None
        ):
            await self._activate_route(task.task_id, candidate.route_id, candidate.parameters)
            await self._execution.start(task.task_id)
        await self._add_assistant_message(
            task.task_id,
            (
                f"工作区写入预览被拒绝：{preparation_error}。文件没有发生变化。"
                if preparation_error is not None
                else (
                    "确定性规则未命中。我已持久化本地 Capability Offer，正在进行一次"
                    "不可重放的任务解释；模型只能引用 opaque offer_key，不能授予权限。"
                    if interpreting
                    else self._route_acceptance_message(candidate, status)
                )
            ),
        )
        return await self.get(task.task_id)

    async def _create_task(
        self,
        *,
        goal: str,
        privacy_mode: Literal["local_preferred", "balanced"],
        constraints: tuple[str, ...],
        conversation_id: str | None = None,
    ) -> TaskWorkbenchRead:
        self._ensure_research_enabled()
        if conversation_id is None:
            conversation = await self._context.create_conversation(
                CreateConversationRequest(title=goal.strip()[:200])
            )
            conversation_id = conversation.conversation_id
        task = await self._tasks.create_task(
            TaskCreate(
                conversation_id=conversation_id,
                goal=goal,
                privacy_mode=privacy_mode,
                constraints=list(constraints),
            )
        )
        await self._context.add_message(
            conversation_id,
            CreateConversationMessageRequest(
                role="user",
                content=goal,
                task_id=task.task_id,
                classification=DataClassification.INTERNAL,
            ),
        )
        await self.activate(task.task_id)
        await self._execution.start(task.task_id)
        await self._add_assistant_message(
            task.task_id,
            "我已建立可验证的执行计划。取证、独立核验、Artifact 构建和"
            "隔离浏览器验收会自动推进；你可以随时停止。",
        )
        return await self.get(task.task_id)

    async def interpret_turn(self, task_id: str) -> TaskWorkbenchRead:
        """Run or recover the one-shot local interpretation for an unrouted Turn."""

        if self._turn_planner is None:
            raise TaskWorkbenchConflictError("Turn Planner runtime is unavailable")
        before = await self.get(task_id)
        action_enabled = any(
            item.action is WorkbenchAction.INTERPRET_TURN and item.enabled
            for item in before.actions
        )
        if not action_enabled:
            recoverable = task_id in await self._turn_planner.recoverable_task_ids(limit=1_000)
            crash_window = before.turn_planning is None or (
                before.turn_planning.run.status == "dispatching"
            )
            if not recoverable or not crash_window:
                return before
        try:
            planning = await self._turn_planner.interpret_turn(task_id)
        except TurnPlannerRuntimeError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        bound = self._turn_planner.bound_route(planning)
        if bound is not None:
            preparation_error = await self._prepare_planner_route(task_id, bound)
            if preparation_error is None and not RouteRecipeCatalog.is_planner_only_route(
                bound.route_id
            ):
                try:
                    runs = await self._execution.list_for_task(task_id)
                    if not runs.runs:
                        await self._execution.start(task_id)
                except (AgentRuntimeError, AgentSupervisorError) as error:
                    # A concurrent caller may have started the exact bound Plan.
                    runs = await self._execution.list_for_task(task_id)
                    if not runs.runs:
                        raise TaskWorkbenchConflictError(str(error)) from error
            candidate = RouteCandidate(
                decision=TurnRouteDecision.ROUTED,
                route_id=bound.route_id,
                parameters=bound.parameters,
                reason_code=bound.reason_code,
            )
            await self._add_assistant_message(
                task_id,
                (
                    f"本地提案选择的工作区预览被拒绝：{preparation_error}。文件没有发生变化。"
                    if preparation_error is not None
                    else self._route_acceptance_message(candidate, bound.status)
                ),
            )
        else:
            await self._add_assistant_message(
                task_id,
                self._turn_planning_outcome_message(planning),
            )
        return await self.get(task_id)

    async def plan_task_loop(self, task_id: str) -> TaskWorkbenchRead:
        """Compose or recover one deferred multi-step Plan without model replay."""

        if self._task_loop is None:
            raise TaskWorkbenchConflictError("Task Loop runtime is unavailable")
        before = await self.get(task_id)
        enabled = any(
            item.action is WorkbenchAction.PLAN_TASK_LOOP and item.enabled
            for item in before.actions
        )
        if not enabled:
            if before.task_loop is None or not before.task_loop.recoverable:
                return before
            recoverable = task_id in await self._task_loop.recoverable_task_ids(limit=1_000)
            if not recoverable:
                return before
        try:
            await self._task_loop.plan(task_id)
        except MultiStepPlanRuntimeError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        # The append-only Task Loop is the status proof.  A separate
        # conversation write would not be atomic with it and could therefore
        # be duplicated or lost across concurrent workers and crash recovery.
        return await self.get(task_id)

    async def _prepare_planner_route(
        self,
        task_id: str,
        bound: BoundTurnRoute,
    ) -> str | None:
        try:
            if bound.route_id == "workspace_file_replace":
                await self._router.prepare_workspace_edit(task_id)
            elif bound.route_id == "workspace_patch_bundle":
                await self._router.prepare_workspace_patch(task_id)
            elif bound.route_id in {"workspace_file_create", "workspace_file_rename"}:
                await self._router.prepare_workspace_path_operation(task_id)
        except TurnRouterError as error:
            return str(error)
        return None

    @staticmethod
    def _turn_planning_outcome_message(planning: TurnPlanningRead) -> str:
        adjudication = planning.adjudication
        if adjudication is None:
            return "本地任务解释仍在进行，尚未形成可执行绑定。"
        if adjudication.outcome == "multi_step_deferred":
            return (
                "本地 Planner 提出了多步骤任务。阶段 111 已将它保存为 "
                "MULTI_STEP_PLAN_DEFERRED，不会拆成多个隐式执行；通用循环将在阶段 112 接管。"
            )
        if adjudication.outcome == "needs_user_input":
            return "本地 Planner 判断当前消息缺少完成安全绑定所需的参数，请补充对象和期望结果。"
        if adjudication.outcome == "unsupported":
            return "本地 Planner 未找到可安全绑定的服务器 Capability Offer，因此没有建立执行运行。"
        failure_code = planning.run.failure.error_code if planning.run.failure is not None else None
        if failure_code is not None:
            return (
                f"本地 Planner 以 {failure_code} 终止；失败证明已保存，"
                "不会自动重放模型调用，确定性结果保持不变。"
            )
        return "本地 Planner 没有形成可执行的单步骤绑定，确定性结果保持不变。"

    async def advance(self, task_id: str) -> TaskWorkbenchRead:
        """Advance exactly one server-authorized safe step."""

        workbench = await self.get(task_id)
        if any(
            item.action is WorkbenchAction.INTERPRET_TURN and item.enabled
            for item in workbench.actions
        ):
            return await self.interpret_turn(task_id)
        if any(
            item.action is WorkbenchAction.PLAN_TASK_LOOP and item.enabled
            for item in workbench.actions
        ):
            return await self.plan_task_loop(task_id)
        if any(
            item.action is WorkbenchAction.ADVANCE_TASK_LOOP and item.enabled
            for item in workbench.actions
        ):
            if self._task_loop_execution is None:
                raise TaskWorkbenchConflictError("Task Loop execution coordinator is unavailable")
            try:
                await self._task_loop_execution.advance(
                    task_id,
                    f"workbench:{task_id}",
                )
            except TaskLoopExecutionCoordinatorError as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            return await self.get(task_id)
        if any(
            item.action is WorkbenchAction.START_EXECUTION and item.enabled
            for item in workbench.actions
        ):
            if (
                self._turn_planner is not None
                and workbench.turn_planning is not None
                and workbench.route is not None
                and workbench.route.result_digest is None
            ):
                try:
                    internal_planning = await self._turn_planner.get(task_id)
                except TurnPlannerRuntimeError as error:
                    raise TaskWorkbenchConflictError(str(error)) from error
                if internal_planning is None:
                    raise TaskWorkbenchConflictError(
                        "Turn planning proof disappeared before execution"
                    )
                bound = self._turn_planner.bound_route(internal_planning)
                if bound is not None:
                    preparation_error = await self._prepare_planner_route(task_id, bound)
                    if preparation_error is not None:
                        await self._add_assistant_message(
                            task_id,
                            f"恢复任务时工作区预览被拒绝：{preparation_error}。文件没有发生变化。",
                        )
                        return await self.get(task_id)
            try:
                await self._execution.start(task_id)
            except (AgentRuntimeError, AgentSupervisorError) as error:
                runs = await self._execution.list_for_task(task_id)
                if not runs.runs:
                    raise TaskWorkbenchConflictError(str(error)) from error
            return await self.get(task_id)
        if any(
            item.action is WorkbenchAction.REPLAN_FAILED_EXECUTION and item.enabled
            for item in workbench.actions
        ):
            return await self._replan_failed_execution(workbench)
        if workbench.route is not None and workbench.route.route_id in {
            "knowledge_lookup",
            "mcp_text_metrics",
            "workspace_file_read",
            "workspace_directory_list",
            "workspace_directory_analyze",
            "workspace_snapshot_check",
            "workspace_python_test",
            "workspace_node_test",
            "workspace_agent_patch_test",
            "workspace_dynamic_patch_test",
        }:
            if not any(
                item.action is WorkbenchAction.EXECUTE_ROUTE and item.enabled
                for item in workbench.actions
            ):
                return workbench
            route_node = next(
                (
                    node
                    for run in workbench.executions.runs[-1:]
                    for node in run.nodes
                    if node.local_key == workbench.route.route_id
                ),
                None,
            )
            if (
                workbench.route.route_id
                in {
                    "workspace_file_read",
                    "workspace_directory_list",
                    "workspace_directory_analyze",
                    "workspace_agent_patch_test",
                    "workspace_dynamic_patch_test",
                }
                and route_node is not None
                and route_node.bound_agent is not None
            ):
                if self._workspace_agents is None:
                    raise TaskWorkbenchConflictError("Workspace Agent runtime is unavailable")
                run = workbench.executions.runs[-1]
                try:
                    outcome = None
                    for wave in range(12):
                        claimed_wave = await self._execution.claim_ready_batch(
                            run.run_id,
                            f"workbench-auto-v2-wave-{wave}",
                            lease_seconds=600,
                        )
                        if not claimed_wave:
                            settled = await self.get(task_id)
                            settled_run = settled.executions.runs[-1]
                            if (
                                settled.route is not None
                                and settled.route.status is TurnRouteStatus.FAILED
                                and settled.route.error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                                and settled_run.status is ExecutionRunStatus.FAILED
                            ):
                                return settled
                            raise TaskWorkbenchConflictError(
                                "Workspace Agent graph has no ready node"
                            )
                        wave_results = await asyncio.gather(
                            *(self._workspace_agents.run(item) for item in claimed_wave),
                            return_exceptions=True,
                        )
                        errors = tuple(
                            item for item in wave_results if isinstance(item, BaseException)
                        )
                        if errors:
                            settled = await self.get(task_id)
                            settled_run = settled.executions.runs[-1]
                            if (
                                settled.route is not None
                                and settled.route.status is TurnRouteStatus.FAILED
                                and settled.route.error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                                and settled_run.status is ExecutionRunStatus.FAILED
                            ):
                                return settled
                            first_error = errors[0]
                            if isinstance(
                                first_error,
                                (
                                    AgentRuntimeError,
                                    AgentSupervisorError,
                                    WorkspaceAgentRuntimeError,
                                ),
                            ):
                                raise first_error
                            raise TaskWorkbenchConflictError(str(first_error))
                        outcomes = tuple(
                            item for item in wave_results if isinstance(item, WorkspaceAgentOutcome)
                        )
                        if len(outcomes) != len(wave_results):
                            raise TaskWorkbenchConflictError(
                                "Workspace Agent wave returned an invalid outcome"
                            )
                        outcome = next(
                            (item for item in outcomes if not item.in_progress),
                            outcomes[-1],
                        )
                        if not all(item.in_progress for item in outcomes):
                            break
                except (
                    AgentRuntimeError,
                    AgentSupervisorError,
                    WorkspaceAgentRuntimeError,
                ) as error:
                    raise TaskWorkbenchConflictError(str(error)) from error
                if outcome is None or outcome.in_progress:
                    raise TaskWorkbenchConflictError(
                        "Workspace Agent graph did not converge within its bounded wave budget"
                    )
                if outcome.needs_user_input:
                    message = cast(str, outcome.question)
                elif outcome.needs_user_action:
                    preview = outcome.patch_preview
                    if preview is None:
                        raise TaskWorkbenchConflictError("Workspace Agent returned no preview")
                    message = (
                        f"Agent 已读取并复核 {preview.changes[0].relative_path}，提出 1 个"
                        "无写权限的精确替换建议。原文件尚未修改；只有确认当前摘要后才会"
                        "提交，并立即运行服务器绑定的固定测试。"
                    )
                else:
                    workspace_result = outcome.result
                    if isinstance(workspace_result, WorkspaceFileRead):
                        message = (
                            f"已只读打开工作区文件 {workspace_result.relative_path}，"
                            f"共 {workspace_result.byte_count} 字节；两轮 Agent 决策、"
                            "Route 观察与结果摘要已经绑定。"
                        )
                    elif isinstance(workspace_result, WorkspaceDirectoryRead):
                        suffix = "，结果已按上限截断" if workspace_result.truncated else ""
                        message = (
                            f"已只读列出工作区目录 {workspace_result.relative_path}，"
                            f"返回 {len(workspace_result.entries)} 个受限子项{suffix}；"
                            "两轮 Agent 决策、Route 观察与结果摘要已经绑定。"
                        )
                    else:
                        raise TaskWorkbenchConflictError("Workspace Agent returned no result")
                await self._add_assistant_message(task_id, message)
                return await self.get(task_id)
            try:
                (
                    knowledge,
                    mcp,
                    workspace_file,
                    _,
                    _,
                    workspace_directory,
                    workspace_check,
                    workspace_python_test,
                    workspace_node_test,
                    _,
                ) = await self._router.execute_direct(task_id)
            except TurnRouterError as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            if knowledge is not None:
                if knowledge.citations:
                    message = (
                        f"本地知识查询完成，找到 {len(knowledge.citations)} 条"
                        "带行号和检索证明的引用。"
                        "右侧可以检查来源；这些片段不会获得系统指令权限。"
                    )
                else:
                    message = "本地知识查询完成，但当前有效来源中没有匹配片段；我没有编造答案。"
            elif mcp is not None:
                metrics = mcp.structured_content
                message = (
                    "只读文本统计完成："
                    f"{metrics['character_count']} 个字符，{metrics['line_count']} 行，"
                    f"{metrics['word_count']} 个词。调用已写入 MCP 审计链。"
                )
            elif workspace_file is not None:
                message = (
                    f"已只读打开工作区文件 {workspace_file.relative_path}，"
                    f"共 {workspace_file.byte_count} 字节；结果绑定了文件版本与内容摘要。"
                )
            elif workspace_directory is not None:
                suffix = "，结果已按上限截断" if workspace_directory.truncated else ""
                message = (
                    f"已只读列出工作区目录 {workspace_directory.relative_path}，"
                    f"返回 {len(workspace_directory.entries)} 个受限子项{suffix}。"
                )
            elif workspace_check is not None:
                if workspace_check.status == "passed":
                    message = (
                        f"固定检查 {workspace_check.profile} 已通过，共核验 "
                        f"{workspace_check.checked_file_count} 个只读快照文件；执行进程已断网隔离。"
                    )
                else:
                    message = (
                        f"固定检查 {workspace_check.profile} 已完成，发现 "
                        f"{len(workspace_check.issues)} 个解析问题；没有执行仓库代码。"
                    )
            elif workspace_python_test is not None:
                summary = (
                    f"{workspace_python_test.passed_count} passed"
                    f"，{workspace_python_test.failed_count} failed"
                    f"，{workspace_python_test.skipped_count} skipped"
                )
                message = (
                    f"Python 测试 {workspace_python_test.test_path} 已完成：{summary}。"
                    "执行使用只读项目快照、断网 AppContainer 和固定 pytest 协议。"
                )
            elif workspace_node_test is not None:
                summary = (
                    f"{workspace_node_test.passed_count} passed"
                    f"，{workspace_node_test.failed_count} failed"
                    f"，{workspace_node_test.skipped_count} skipped"
                )
                message = (
                    f"Node 测试 {workspace_node_test.test_path} 已完成：{summary}。"
                    "执行使用有界项目快照、断网 AppContainer 和固定 node:test 协议。"
                )
            else:
                raise TaskWorkbenchConflictError("Direct Route returned no result")
            await self._add_assistant_message(task_id, message)
            return await self.get(task_id)
        current_run = workbench.executions.runs[-1] if workbench.executions.runs else None
        if current_run is None:
            return workbench
        enabled = {item.action for item in workbench.actions if item.enabled}
        try:
            if WorkbenchAction.RUN_RESEARCH in enabled:
                if self._research is None:
                    raise TaskWorkbenchConflictError("Research runtime is disabled")
                claimed = await self._execution.claim_next(
                    current_run.run_id, "workbench-auto-v1", lease_seconds=600
                )
                if claimed is None:
                    raise TaskWorkbenchConflictError("Research node is no longer ready")
                await self._research.run(claimed)
                message = "公开来源取证已完成。我正在独立核验 Claim 与 Citation。"
            elif WorkbenchAction.VERIFY_CLAIMS in enabled:
                await self._artifacts.verify_research(current_run.run_id)
                message = "引用核验通过。我正在生成只使用已验证事实的 HTML Artifact。"
            elif WorkbenchAction.BUILD_ARTIFACT in enabled:
                await self._artifacts.build_html(current_run.run_id)
                message = "Artifact 已生成。我正在隔离、断网的浏览器中进行最终验收。"
            elif WorkbenchAction.VERIFY_BROWSER in enabled:
                await self._artifacts.verify_browser(current_run.run_id)
                message = "浏览器验收通过。我正在形成最终交付清单。"
            elif WorkbenchAction.FINALIZE_DELIVERY in enabled:
                delivery = await self._artifacts.finalize(current_run.run_id)
                await self._memory.propose_verified_episode(delivery)
                message = (
                    "任务完成。HTML、Markdown、PDF 与完整证据链已经就绪；"
                    "写入你选择的路径前仍需你的确认。"
                )
            else:
                return workbench
        except TaskWorkbenchError:
            raise
        except (
            AgentRuntimeError,
            AgentSupervisorError,
            ResearchRuntimeError,
            WorkspaceAgentRuntimeError,
            ArtifactDeliveryError,
            LongTermMemoryError,
        ) as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        await self._add_assistant_message(task_id, message)
        return await self.get(task_id)

    async def replan_failed_execution(self, task_id: str) -> TaskWorkbenchRead:
        """Apply the one bounded server-authorized replacement generation."""

        workbench = await self.get(task_id)
        continuation: AgentReplanContinuationIntent | None = None
        if (
            workbench.route is not None
            and workbench.route.route_id == "workspace_dynamic_patch_test"
            and self._replan_action_enabled(workbench)
        ):
            continuation = await self._record_replan_continuation(
                task_id,
                "生成新计划代",
                requested_via="workbench_action",
            )
        return await self._schedule_automatic(
            await self._replan_failed_execution(workbench, continuation)
        )

    @staticmethod
    def _replan_action_enabled(workbench: TaskWorkbenchRead) -> bool:
        return any(
            item.action is WorkbenchAction.REPLAN_FAILED_EXECUTION and item.enabled
            for item in workbench.actions
        )

    async def _replan_failed_execution(
        self,
        workbench: TaskWorkbenchRead,
        continuation: AgentReplanContinuationIntent | None = None,
    ) -> TaskWorkbenchRead:
        if not self._replan_action_enabled(workbench):
            raise TaskWorkbenchConflictError("Task has no eligible bounded Agent replan")
        run = workbench.executions.runs[-1] if workbench.executions.runs else None
        if run is None:
            raise TaskWorkbenchConflictError("Failed execution Run is missing")
        task_id = workbench.task.task_id
        route_id = workbench.route.route_id if workbench.route is not None else None
        if route_id == "workspace_directory_analyze":
            draft = workspace_directory_analyze_draft(task_id)
            message = (
                "上一代只读 Agent 图未通过协议校验。我已封存失败证据，并在同一任务"
                "合同下原子激活新的计划代；旧计划、运行和动态图保持不可变。"
            )
        elif route_id == "workspace_dynamic_patch_test":
            draft = workspace_dynamic_patch_test_draft(task_id)
            message = (
                "上一代图的固定测试条件未通过。我已按你的请求封存 false decision 并"
                "激活新的不可变计划代；旧补丁回执仍可审计，但旧确认不会授权新补丁，"
                "新 Patch 节点必须重新预演并等待你确认。"
            )
        else:
            raise TaskWorkbenchConflictError("Failed Agent Route is not replan eligible")
        try:
            await self._planning.replan_failed_agent_execution(
                task_id,
                run.run_id,
                draft,
                continuation,
            )
        except PlanningError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        await self._add_assistant_message(
            task_id,
            message,
        )
        return await self.get(task_id)

    async def _record_replan_continuation(
        self,
        task_id: str,
        content: str,
        *,
        requested_via: Literal["conversation_turn", "workbench_action"],
    ) -> AgentReplanContinuationIntent:
        intent_code = classify_agent_replan_continuation(content)
        if intent_code is None:
            raise TaskWorkbenchConflictError("Conversation turn is not an explicit Patch replan")
        message = await self._add_user_message(task_id, content)
        material = {
            "schema_version": "deskpilot.agent-replan-continuation-intent.v1",
            "task_id": task_id,
            "message_id": message.message_id,
            "message_digest": message.message_digest,
            "intent_code": intent_code,
            "requested_via": requested_via,
        }
        return AgentReplanContinuationIntent.model_validate(
            {**material, "intent_digest": sha256_digest(material)}
        )

    async def commit_workspace_edit(
        self, task_id: str, confirmation_digest: str
    ) -> TaskWorkbenchRead:
        workbench = await self.get(task_id)
        if not any(
            item.action is WorkbenchAction.COMMIT_WORKSPACE_EDIT and item.enabled
            for item in workbench.actions
        ):
            if isinstance(workbench.workspace_edit, WorkspaceEditReceipt):
                if workbench.workspace_edit.confirmation_digest == confirmation_digest:
                    return workbench
            raise TaskWorkbenchConflictError("Workspace replacement is not awaiting confirmation")
        try:
            receipt = await self._router.commit_workspace_edit(task_id, confirmation_digest)
        except TurnRouterError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        await self._add_assistant_message(
            task_id,
            f"已完成 {receipt.relative_path} 的单次精确替换。原文件保留在 "
            f"{receipt.backup_relative_path}，提交回执已写入审计链。",
        )
        return await self.get(task_id)

    async def commit_workspace_patch(
        self, task_id: str, confirmation_digest: str
    ) -> TaskWorkbenchRead:
        workbench = await self.get(task_id)
        if not any(
            item.action is WorkbenchAction.COMMIT_WORKSPACE_PATCH and item.enabled
            for item in workbench.actions
        ):
            generic_summary = (
                workbench.task_loop
                if isinstance(workbench.task_loop, TaskLoopExecutionWorkbenchRead)
                else None
            )
            if (
                isinstance(workbench.workspace_patch, WorkspacePatchPreview)
                and workbench.workspace_patch.confirmation_digest == confirmation_digest
                and generic_summary is not None
                and generic_summary.execution_status in {
                    "active",
                    "repairing",
                    "succeeded",
                }
            ):
                return workbench
            if isinstance(workbench.workspace_patch, WorkspacePatchReceipt):
                if (
                    workbench.workspace_patch.status == "committed"
                    and workbench.workspace_patch.confirmation_digest == confirmation_digest
                ):
                    return workbench
            if (
                workbench.route is not None
                and workbench.route.route_id == "workspace_dynamic_patch_test"
                and workbench.executions.runs
                and any(
                    node.approval is not None
                    and node.approval.confirmation_digest == confirmation_digest
                    and node.patch_result is not None
                    for graph in workbench.executions.runs[-1].task_graphs
                    for node in graph.nodes
                )
            ):
                return workbench
            raise TaskWorkbenchConflictError("Workspace patch is not awaiting confirmation")
        generic_task_loop = (
            workbench.task_loop
            if isinstance(workbench.task_loop, TaskLoopExecutionWorkbenchRead)
            else None
        )
        generic_patch = bool(
            generic_task_loop is not None
            and generic_task_loop.execution_status == "awaiting_user"
            and isinstance(workbench.workspace_patch, WorkspacePatchPreview)
        )
        if generic_patch:
            if (
                self._task_loop_execution is None
                or generic_task_loop is None
                or generic_task_loop.execution_revision is None
            ):
                raise TaskWorkbenchConflictError(
                    "Task Loop workspace patch runtime is unavailable"
                )
            try:
                preview = await self._task_loop_execution.approve_workspace_patch(
                    task_id,
                    confirmation_digest,
                    expected_execution_revision=(
                        generic_task_loop.execution_revision
                    ),
                )
            except TaskLoopExecutionCoordinatorError as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            await self._add_assistant_message(
                task_id,
                f"已批准 {len(preview.changes)} 个工作区文件的精确补丁；"
                "通用任务循环将按同一 Task/revision 恢复提交并核验回执。",
            )
            return await self._schedule_automatic(await self.get(task_id))
        is_agent_patch = bool(
            workbench.route is not None
            and workbench.route.route_id
            in {"workspace_agent_patch_test", "workspace_dynamic_patch_test"}
        )
        if is_agent_patch:
            if self._workspace_agents is None:
                raise TaskWorkbenchConflictError("Workspace Agent runtime is unavailable")
            try:
                outcome = await self._workspace_agents.commit_patch_test(
                    task_id, confirmation_digest
                )
            except (AgentRuntimeError, WorkspaceAgentRuntimeError) as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            test_result = outcome.python_test or outcome.node_test
            detail = (
                f"{test_result.passed_count} passed，{test_result.failed_count} failed"
                if test_result is not None
                else f"运行错误 {outcome.error_code}"
            )
            await self._add_assistant_message(
                task_id,
                f"已提交 Agent 的单文件补丁并保留安全备份；固定测试结果为 {detail}。"
                + (
                    "补丁与测试证明均已通过。"
                    if outcome.status == "verified"
                    else "写入事实已保留，测试未通过且不会自动扩大权限或重试修改。"
                ),
            )
        else:
            try:
                receipt = await self._router.commit_workspace_patch(task_id, confirmation_digest)
            except TurnRouterError as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            await self._add_assistant_message(
                task_id,
                f"已提交 {len(receipt.change_receipts)} 个工作区文件替换，"
                "每个原文件都保留了独立安全备份，补丁回执已写入审计链。",
            )
        updated = await self.get(task_id)
        if (
            workbench.route is not None
            and workbench.route.route_id == "workspace_dynamic_patch_test"
        ):
            return await self._schedule_automatic(updated)
        return updated

    async def commit_workspace_git(
        self,
        task_id: str,
        confirmation_digest: str,
    ) -> TaskWorkbenchRead:
        workbench = await self.get(task_id)
        git_commit = workbench.workspace_git_commit
        if not any(
            item.action is WorkbenchAction.COMMIT_WORKSPACE_GIT and item.enabled
            for item in workbench.actions
        ):
            if (
                isinstance(git_commit, GitCommitPreview)
                and git_commit.confirmation_digest == confirmation_digest
            ) or (
                isinstance(git_commit, GitCommitReceipt)
                and git_commit.confirmation_digest == confirmation_digest
            ):
                return workbench
            raise TaskWorkbenchConflictError("Git commit is not awaiting confirmation")
        generic_summary = (
            workbench.task_loop
            if isinstance(workbench.task_loop, TaskLoopExecutionWorkbenchRead)
            else None
        )
        if (
            self._task_loop_execution is None
            or generic_summary is None
            or generic_summary.execution_revision is None
            or not isinstance(git_commit, GitCommitPreview)
        ):
            raise TaskWorkbenchConflictError("Task Loop Git commit runtime is unavailable")
        try:
            preview = await self._task_loop_execution.approve_git_commit(
                task_id,
                confirmation_digest,
                expected_execution_revision=generic_summary.execution_revision,
            )
        except TaskLoopExecutionCoordinatorError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        await self._add_assistant_message(
            task_id,
            f"已批准受控 Git 提交到 {preview.target_branch}；hooks、签名和 push 均关闭，"
            "任务循环将恢复并核验提交回执。",
        )
        return await self._schedule_automatic(await self.get(task_id))

    async def commit_workspace_path_operation(
        self, task_id: str, confirmation_digest: str
    ) -> TaskWorkbenchRead:
        workbench = await self.get(task_id)
        if not any(
            item.action is WorkbenchAction.COMMIT_WORKSPACE_PATH_OPERATION and item.enabled
            for item in workbench.actions
        ):
            if isinstance(workbench.workspace_path_operation, WorkspacePathOperationReceipt):
                if workbench.workspace_path_operation.confirmation_digest == confirmation_digest:
                    return workbench
            raise TaskWorkbenchConflictError(
                "Workspace path operation is not awaiting confirmation"
            )
        try:
            receipt = await self._router.commit_workspace_path_operation(
                task_id, confirmation_digest
            )
        except TurnRouterError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        message = (
            f"已创建工作区文件 {receipt.target_path}。内容摘要与恢复清单已绑定到提交回执。"
            if receipt.operation == "create"
            else f"已将工作区文件 {receipt.source_path} 重命名为 {receipt.target_path}。"
            "文件内容与原版本身份保持不变，提交回执已写入审计链。"
        )
        await self._add_assistant_message(task_id, message)
        return await self.get(task_id)

    async def stop(self, task_id: str) -> TaskWorkbenchRead:
        workbench = await self.get(task_id)
        if not any(
            item.action is WorkbenchAction.STOP_EXECUTION and item.enabled
            for item in workbench.actions
        ):
            return workbench
        if workbench.turn_planning is not None and workbench.turn_planning.run.status in {
            "prepared",
            "dispatching",
        }:
            if self._auto_advance is not None:
                await self._auto_advance.cancel(task_id)
            if self._turn_planner is None:
                raise TaskWorkbenchConflictError("Turn Planner runtime is unavailable")
            try:
                await self._turn_planner.cancel(task_id)
            except TurnPlannerRuntimeError as error:
                raise TaskWorkbenchConflictError(str(error)) from error
            await self._add_assistant_message(
                task_id,
                "任务解释已停止，Planner 租约和 fencing token 已失效；迟到结果不会被采用。",
            )
            return await self.get(task_id)
        run = workbench.executions.runs[-1] if workbench.executions.runs else None
        if run is None:
            raise TaskWorkbenchConflictError("Task has no execution run")
        if self._auto_advance is not None:
            await self._auto_advance.cancel(task_id)
        await self._execution.cancel(run.run_id)
        await self._add_assistant_message(
            task_id,
            "任务已停止，所有未完成的执行凭证都已失效。你可以直接发送新的要求重新开始。",
        )
        return await self.get(task_id)

    async def _add_assistant_message(self, task_id: str, content: str) -> None:
        task = await self._tasks.get_task(task_id)
        if task.conversation_id is None:
            raise TaskWorkbenchConflictError("Task has no conversation")
        await self._context.add_message(
            task.conversation_id,
            CreateConversationMessageRequest(
                role="assistant",
                content=content,
                task_id=task_id,
                classification=DataClassification.INTERNAL,
            ),
        )

    async def _add_user_message(
        self,
        task_id: str,
        content: str,
    ) -> ConversationMessageRead:
        task = await self._tasks.get_task(task_id)
        if task.conversation_id is None:
            raise TaskWorkbenchConflictError("Task has no conversation")
        return await self._context.add_message(
            task.conversation_id,
            CreateConversationMessageRequest(
                role="user",
                content=content,
                task_id=task_id,
                classification=DataClassification.INTERNAL,
            ),
        )

    async def activate(self, task_id: str) -> TaskWorkbenchRead:
        try:
            await self._planning.get_state(task_id)
        except PlanningNotFoundError:
            self._ensure_research_enabled()
            contract = research_to_html_contract(
                task_id,
                self._capabilities,
                allow_user_path_export=True,
            )
            await self._planning.activate(contract, research_to_html_draft(task_id))
        return await self.get(task_id)

    async def _activate_route(
        self, task_id: str, route_id: str | None, parameters: dict[str, str]
    ) -> None:
        if route_id == "research_to_html":
            self._ensure_research_enabled()
            contract = research_to_html_contract(
                task_id,
                self._capabilities,
                allow_user_path_export=True,
            )
            draft = research_to_html_draft(task_id)
        elif route_id == "knowledge_lookup":
            contract = knowledge_lookup_contract(task_id, self._capabilities)
            draft = knowledge_lookup_draft(task_id)
        elif route_id == "mcp_text_metrics":
            contract = mcp_text_metrics_contract(task_id, self._capabilities)
            draft = mcp_text_metrics_draft(task_id)
        elif route_id == "workspace_file_read":
            contract = workspace_file_read_contract(task_id, self._capabilities)
            draft = workspace_file_read_draft(task_id)
        elif route_id == "workspace_directory_list":
            contract = workspace_directory_list_contract(task_id, self._capabilities)
            draft = workspace_directory_list_draft(task_id)
        elif route_id == "workspace_directory_analyze":
            contract = workspace_directory_analyze_contract(task_id, self._capabilities)
            draft = workspace_directory_analyze_draft(task_id)
        elif route_id == "workspace_snapshot_check":
            contract = workspace_snapshot_check_contract(task_id, self._capabilities)
            draft = workspace_snapshot_check_draft(task_id)
        elif route_id == "workspace_python_test":
            contract = workspace_python_test_contract(task_id, self._capabilities)
            draft = workspace_python_test_draft(task_id)
        elif route_id == "workspace_node_test":
            contract = workspace_node_test_contract(task_id, self._capabilities)
            draft = workspace_node_test_draft(task_id)
        elif route_id == "workspace_file_replace":
            contract = workspace_file_replace_contract(task_id, self._capabilities)
            draft = workspace_file_replace_draft(task_id)
        elif route_id == "workspace_file_create":
            contract = workspace_file_create_contract(task_id, self._capabilities)
            draft = workspace_file_create_draft(task_id)
        elif route_id == "workspace_file_rename":
            contract = workspace_file_rename_contract(task_id, self._capabilities)
            draft = workspace_file_rename_draft(task_id)
        elif route_id == "workspace_patch_bundle":
            contract = workspace_patch_bundle_contract(task_id, self._capabilities)
            draft = workspace_patch_bundle_draft(task_id)
        elif route_id == "workspace_agent_patch_test":
            test_kind = parameters.get("test_kind")
            if test_kind not in {"python", "node"}:
                raise TaskWorkbenchConflictError("Workspace patch test kind is invalid")
            contract = workspace_agent_patch_test_contract(
                task_id,
                self._capabilities,
                test_kind=cast(Literal["python", "node"], test_kind),
            )
            draft = workspace_agent_patch_test_draft(task_id)
        elif route_id == "workspace_dynamic_patch_test":
            test_kind = parameters.get("test_kind")
            if test_kind not in {"python", "node"}:
                raise TaskWorkbenchConflictError("Dynamic Patch test kind is invalid")
            contract = workspace_dynamic_patch_test_contract(
                task_id,
                self._capabilities,
                test_kind=cast(Literal["python", "node"], test_kind),
            )
            draft = workspace_dynamic_patch_test_draft(task_id)
        else:
            raise TaskWorkbenchConflictError("Turn Route is not registered")
        await self._planning.activate(contract, draft)

    @staticmethod
    def _route_acceptance_message(candidate: RouteCandidate, status: TurnRouteStatus) -> str:
        if candidate.decision is TurnRouteDecision.NEEDS_CLARIFICATION:
            if candidate.reason_code == "MCP_TEXT_MISSING":
                return "我可以统计文本，但还缺少要处理的内容。请用“统计字符数：你的文本”继续发送。"
            if candidate.reason_code == "KNOWLEDGE_QUERY_MISSING":
                return "我会继续查本地知识库。请直接告诉我要查的主题或关键词。"
            if candidate.reason_code == "RESEARCH_GOAL_MISSING":
                return "我会继续生成可核验的多格式报告。请直接告诉我要研究的主题。"
            if candidate.reason_code == "WORKSPACE_FILE_PATH_MISSING":
                return "我会继续读取文件。请直接发送工作区内的相对文件路径，例如 README.md。"
            if candidate.reason_code == "WORKSPACE_TEST_PATH_MISSING":
                return (
                    "我会继续运行这个项目的单文件测试。请发送一个明确的 tests/test_*.py、"
                    "*_test.py、*.spec.js 或 *.test.js 路径。"
                )
            if candidate.reason_code == "MULTIPLE_ROUTES_MATCHED":
                return (
                    "这条指令同时像知识库查询和联网研究。请说明要只查本地知识，"
                    "还是研究公开来源并生成 HTML。"
                )
            if candidate.reason_code == "WORKSPACE_COMMAND_INVALID":
                return (
                    "工作区命令格式不明确。请用“读取工作区文件：README.md”，或"
                    '“在工作区文件 README.md 中把 "旧文本" 替换为 "新文本"”。'
                )
            if candidate.reason_code == "WORKSPACE_PATCH_INVALID":
                return "批量补丁需要 2–8 个不同文件的精确替换，每项用分号分隔。"
            return "我还无法确定要完成的具体结果。请补充对象和期望交付物。"
        if candidate.decision is TurnRouteDecision.UNSUPPORTED:
            if candidate.reason_code == "RESEARCH_RUNTIME_DISABLED":
                return "我识别到联网研究任务，但当前研究 Provider 未启用，因此没有建立执行运行。"
            if candidate.reason_code == "WORKSPACE_RUNTIME_DISABLED":
                return (
                    "我识别到工作区文件任务，但当前没有配置 DESKPILOT_CONVERSATION_WORKSPACE_ROOT，"
                    "因此没有获得任何文件访问权限。"
                )
            if candidate.reason_code == "WORKSPACE_CHECK_RUNTIME_DISABLED":
                return "固定工作区检查需要断网 AppContainer，但当前隔离运行时不可用，因此没有执行。"
            if candidate.reason_code == "WORKSPACE_PYTHON_TEST_RUNTIME_DISABLED":
                return "Python 项目测试需要断网 AppContainer，但当前隔离运行时不可用。"
            if candidate.reason_code == "WORKSPACE_NODE_TEST_RUNTIME_DISABLED":
                return "Node 项目测试需要固定 Node 与断网 AppContainer，但当前运行时不可用。"
            if candidate.reason_code == "WORKSPACE_PATH_OPERATION_RUNTIME_DISABLED":
                return "工作区新建和重命名需要 Windows 同卷原子提交与恢复目录，但当前运行时不可用。"
            return (
                "当前受信能力只支持公开研究并生成 HTML/Markdown/PDF、本地知识查询和只读文本统计。"
                "我没有把这条未知请求强行改成研究任务。"
            )
        if candidate.route_id == "knowledge_lookup":
            return "我已识别为本地知识查询，将只读取已导入且来源版本仍有效的片段，并保留行号证明。"
        if candidate.route_id == "mcp_text_metrics":
            if status is TurnRouteStatus.NEEDS_USER_ACTION:
                return (
                    "我已识别为只读文本统计。内置 MCP Server 当前未启用，"
                    "需要你明确启用后才能启动本地进程。"
                )
            return "我已识别为只读文本统计，将通过固定内置 MCP Schema 执行并保留审计回执。"
        if candidate.route_id == "workspace_file_read":
            if candidate.reason_code == "WORKSPACE_FILE_PATH_MISSING":
                return (
                    "我已识别为工作区文件读取。Workspace Reader 会先判断缺少的路径，"
                    "再暂停并向你提出一个可续接的问题。"
                )
            return "我已识别为工作区文件读取，只会访问配置根目录内的受限 UTF-8 文本文件。"
        if candidate.route_id == "workspace_directory_list":
            return "我已识别为工作区目录读取，只会列出受限直接子项，不递归读取文件内容。"
        if candidate.route_id == "workspace_directory_analyze":
            if candidate.reason_code == "WORKSPACE_DIRECTORY_TEST_GRAPH_MATCHED":
                return (
                    "我已识别为固定测试任务图；模型只能选择服务器绑定的目录、Python "
                    "pytest 和 Node node:test 输入槽，不能生成 executable、argv 或安装命令。"
                )
            return (
                "我已识别为异构只读目录分析；模型只能选择服务器公布的目录路径和"
                "显式文件路径输入槽，不能生成其他路径。"
            )
        if candidate.route_id == "workspace_snapshot_check":
            return (
                "我已识别为固定工作区检查，将把有界只读快照送入断网 "
                "AppContainer 解析，不执行仓库代码。"
            )
        if candidate.route_id == "workspace_python_test":
            return (
                "我已识别为 Python 项目测试，将在独立只读快照中运行一个"
                "显式 pytest 文件；进程断网且不会修改原项目。"
            )
        if candidate.route_id == "workspace_node_test":
            return (
                "我已识别为 Node 项目测试，将在独立有界快照中运行一个"
                "显式 node:test 文件；进程断网且不会修改原项目。"
            )
        if candidate.route_id == "workspace_file_replace":
            return "我已生成单次精确替换预览；文件尚未修改，确认摘要后才会原子提交并保留备份。"
        if candidate.route_id == "workspace_patch_bundle":
            return "我已在隔离副本中预演多文件补丁；原文件尚未修改，一次确认后才会提交。"
        if candidate.route_id == "workspace_agent_patch_test":
            return (
                "我已建立 Agent 补丁与固定测试计划。模型只能读取显式目标并提出一个"
                "无写权限建议；预览确认前不会修改文件。"
            )
        if candidate.route_id == "workspace_dynamic_patch_test":
            return (
                "我已建立动态多 Agent 修复图。模型只能选择服务器公布的目录与单个"
                "Patch/Approval 输入槽；补丁节点暂停后必须确认自己的当前摘要，才会"
                "原子提交并运行固定测试。"
            )
        if candidate.route_id == "workspace_file_create":
            return "我已生成新建文件预览；目标当前不存在，确认前不会写入，也绝不会覆盖同名文件。"
        if candidate.route_id == "workspace_file_rename":
            return "我已生成文件重命名预览；源版本和目标目录已绑定，确认前路径不会变化。"
        return (
            "我已建立可验证的研究执行计划。取证、独立核验、多格式 Artifact 构建和"
            "隔离浏览器验收会自动推进；你可以随时停止。"
        )

    def _ensure_research_enabled(self) -> None:
        if not self._research_enabled():
            raise TaskWorkbenchConflictError(
                "Research runtime is disabled; configure a SearchProvider before creating this task"
            )

    def _research_enabled(self) -> bool:
        return bool(
            self._research is not None
            and self._capabilities.resolve_preferred("research.read.v1").runtime_enabled
        )

    def _turn_planner_variant_keys(self) -> frozenset[str]:
        """Return only variants backed by the currently configured runtimes."""

        variants = {"knowledge_lookup", "mcp_text_metrics"}
        if self._research_enabled():
            variants.add("research_to_html")
        if not self._router.workspace_enabled:
            return frozenset(variants)
        variants.update({"workspace_file_read", "workspace_directory_list"})
        variants.update(
            {
                "workspace_project_search",
                "workspace_project_batch_read",
                "workspace_git_inspect",
            }
        )
        if self._workspace_agents is not None:
            variants.add("workspace_directory_analyze")
        if self._router.workspace_patch_enabled:
            variants.update({"workspace_file_replace", "workspace_patch_bundle"})
        if self._router.workspace_path_operation_enabled:
            variants.update({"workspace_file_create", "workspace_file_rename"})
        if self._router.workspace_check_enabled:
            variants.add("workspace_snapshot_check")
        if self._router.workspace_python_test_enabled:
            variants.add("workspace_python_test")
            if self._router.workspace_patch_enabled and self._workspace_agents is not None:
                variants.update(
                    {
                        "workspace_agent_patch_test:python",
                        "workspace_dynamic_patch_test:python",
                    }
                )
        if self._router.workspace_node_test_enabled:
            variants.add("workspace_node_test")
            if self._router.workspace_patch_enabled and self._workspace_agents is not None:
                variants.update(
                    {
                        "workspace_agent_patch_test:node",
                        "workspace_dynamic_patch_test:node",
                    }
                )
        variants.update(
            f"workspace_command_profile:{profile_id}"
            for profile_id in self._command_profile_ids
        )
        return frozenset(variants)

    async def get(self, task_id: str) -> TaskWorkbenchRead:
        # The projection is assembled from several independently verified
        # aggregates. A background Coordinator can commit between those reads,
        # especially with SQLite's legacy SELECT transaction behaviour, so one
        # attempt may legitimately combine two adjacent committed revisions.
        # Retry the whole projection instead of exposing that transient mixture.
        # Stable corruption still fails closed after the bounded attempts.
        for attempt in range(self._PROJECTION_READ_ATTEMPTS):
            try:
                return await self._get_once(task_id)
            except TaskWorkbenchConflictError:
                if attempt + 1 >= self._PROJECTION_READ_ATTEMPTS:
                    raise
                await asyncio.sleep(0.005 * (attempt + 1))
        raise AssertionError("unreachable")

    async def _get_once(self, task_id: str) -> TaskWorkbenchRead:
        try:
            task = await self._tasks.get_task(task_id)
        except TaskNotFoundError as error:
            raise TaskWorkbenchNotFoundError("Task does not exist") from error

        conversation = (
            await self._context.list_conversation_messages(task.conversation_id)
            if task.conversation_id is not None
            else await self._context.list_task_messages(task_id)
        )
        route: TurnRouteRead | None
        try:
            route = await self._router.get(task_id)
        except TurnRouterError as error:
            if error.code == "TURN_ROUTE_CONFLICT":
                route = None
            else:
                raise TaskWorkbenchConflictError(str(error)) from error
        try:
            turn_planning = (
                await self._turn_planner.get(task_id) if self._turn_planner is not None else None
            )
        except TurnPlannerRuntimeError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        try:
            task_loop = await self._task_loop.get(task_id) if self._task_loop is not None else None
        except MultiStepPlanRuntimeError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        try:
            task_loop_execution = (
                await self._task_loop_activation.get(task_id)
                if self._task_loop_activation is not None
                else None
            )
        except TaskLoopActivationError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        try:
            planning = await self._planning.get_state(task_id)
            contract = await self._planning.get_current_contract(task_id)
            plans = await self._planning.list_plans(task_id)
        except PlanningNotFoundError:
            planning = None
            contract = None
            plans = ExecutablePlanPage(plans=())
        except PlanningError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        try:
            replans = await self._planning.list_replans(task_id)
        except PlanningError as error:
            raise TaskWorkbenchConflictError(str(error)) from error

        try:
            executions = await self._execution.list_for_task(task_id)
        except (AgentRuntimeError, AgentSupervisorError) as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        run = executions.runs[-1] if executions.runs else None
        research = await self._latest_research(task_id)
        verification = None
        workspace = None
        browser = None
        delivery = None
        if run is not None:
            try:
                verification = await self._artifacts.get_verification(run.run_id)
            except ArtifactDeliveryNotFoundError:
                pass
            try:
                browser = await self._artifacts.get_browser(run.run_id)
            except ArtifactDeliveryNotFoundError:
                pass
            try:
                delivery = await self._artifacts.get_delivery(run.run_id)
            except ArtifactDeliveryNotFoundError:
                pass
            async with self._database.session() as session:
                workspace_id = await session.scalar(
                    select(TaskArtifactWorkspaceRecord.workspace_id).where(
                        TaskArtifactWorkspaceRecord.run_id == run.run_id
                    )
                )
            if workspace_id is not None:
                workspace = await self._artifacts.get_workspace(workspace_id)

        try:
            exports = await self._exports.list_for_task(task_id)
        except ArtifactExportError as error:
            raise TaskWorkbenchConflictError("Artifact export proof drifted") from error
        try:
            (
                knowledge,
                mcp,
                workspace_file,
                workspace_edit,
                workspace_patch,
                workspace_directory,
                workspace_check,
                workspace_python_test,
                workspace_node_test,
                workspace_path_operation,
            ) = await self._router.get_result(task_id)
        except TurnRouterError as error:
            raise TaskWorkbenchConflictError(str(error)) from error
        if (
            task_loop_execution is not None
            and task_loop_execution.workspace_patch is not None
        ):
            workspace_patch = task_loop_execution.workspace_patch
        workspace_git_commit = (
            task_loop_execution.git_commit
            if task_loop_execution is not None
            else None
        )
        mcp_enabled = bool(
            route is not None
            and route.route_id == "mcp_text_metrics"
            and await self._router.mcp_enabled()
        )
        stage = self._stage(
            executions,
            planning,
            delivery,
            exports,
            route,
            turn_planning,
            task_loop,
            task_loop_execution,
        )
        repair_loop = self._repair_loop_status(executions, contract, route)
        actions = self._actions(
            executions,
            contract,
            delivery,
            route,
            mcp_enabled,
            repair_loop,
            turn_planning,
            task_loop,
            task_loop_execution,
        )
        material = {
            "schema_version": "deskpilot.task-workbench.v1",
            "task": task,
            "stage": stage,
            "actions": actions,
            "conversation": conversation,
            "route": route,
            "turn_planning": (
                TurnPlanningWorkbenchRead.from_internal(turn_planning)
                if turn_planning is not None
                else None
            ),
            "task_loop": (
                task_loop_execution.workbench
                if (
                    task_loop_execution is not None
                    and task_loop_execution.execution is not None
                )
                else (
                    TaskLoopWorkbenchRead.from_internal(task_loop)
                    if task_loop is not None
                    else None
                )
            ),
            "planning": planning,
            "contract": contract,
            "plans": plans,
            "executions": executions,
            "replans": replans,
            "repair_loop": repair_loop,
            "research": research,
            "verification": verification,
            "workspace": workspace,
            "browser": browser,
            "delivery": delivery,
            "knowledge": knowledge,
            "mcp": mcp,
            "workspace_file": workspace_file,
            "workspace_edit": workspace_edit,
            "workspace_patch": workspace_patch,
            "workspace_git_commit": workspace_git_commit,
            "workspace_path_operation": workspace_path_operation,
            "workspace_directory": workspace_directory,
            "workspace_check": workspace_check,
            "workspace_python_test": workspace_python_test,
            "workspace_node_test": workspace_node_test,
            "exports": exports,
        }
        return TaskWorkbenchRead.model_validate(
            {**material, "projection_digest": sha256_digest(material)}
        )

    async def _latest_research(self, task_id: str) -> ResearchSessionRead | None:
        async with self._database.session() as session:
            record = await session.scalar(
                select(ResearchSessionRecord)
                .where(ResearchSessionRecord.task_id == task_id)
                .order_by(ResearchSessionRecord.updated_at.desc())
                .limit(1)
            )
            if record is None:
                return None
            session_id = record.research_session_id
            calls = tuple(
                (
                    await session.scalars(
                        select(ResearchSearchCallRecord)
                        .where(ResearchSearchCallRecord.research_session_id == session_id)
                        .order_by(ResearchSearchCallRecord.attempt)
                    )
                ).all()
            )
            pages = tuple(
                (
                    await session.scalars(
                        select(ResearchPageSnapshotRecord)
                        .where(ResearchPageSnapshotRecord.research_session_id == session_id)
                        .order_by(ResearchPageSnapshotRecord.created_at)
                    )
                ).all()
            )
            claims = tuple(
                (
                    await session.scalars(
                        select(ResearchClaimRecord)
                        .where(ResearchClaimRecord.research_session_id == session_id)
                        .order_by(ResearchClaimRecord.created_at)
                    )
                ).all()
            )
            citations = tuple(
                (
                    await session.scalars(
                        select(ResearchCitationRecord)
                        .where(ResearchCitationRecord.research_session_id == session_id)
                        .order_by(ResearchCitationRecord.created_at)
                    )
                ).all()
            )
            return ResearchSessionRead(
                research_session_id=record.research_session_id,
                task_id=record.task_id,
                invocation_id=record.invocation_id,
                status=cast(
                    Literal[
                        "created",
                        "running",
                        "awaiting_verification",
                        "verified",
                        "rejected",
                        "failed",
                    ],
                    record.status,
                ),
                search_calls=tuple(
                    SearchCallRead(
                        search_call_id=item.search_call_id,
                        research_session_id=item.research_session_id,
                        attempt=item.attempt,
                        provider_id=item.provider_id,
                        query_digest=item.query_digest,
                        hits=tuple(SearchHit.model_validate(hit) for hit in item.hits),
                        created_at=item.created_at,
                    )
                    for item in calls
                ),
                page_snapshots=tuple(PageSnapshot.model_validate(item.manifest) for item in pages),
                claims=tuple(ResearchClaim.model_validate(item.manifest) for item in claims),
                citations=tuple(
                    CitationEvidence.model_validate(item.manifest) for item in citations
                ),
                created_at=record.created_at,
                updated_at=record.updated_at,
            )

    @staticmethod
    def _stage(
        executions: ExecutionRunPage,
        planning: PlanningStateRead | None,
        delivery: DeliveryManifestRead | None,
        exports: tuple[ArtifactExportRead, ...],
        route: TurnRouteRead | None,
        turn_planning: TurnPlanningRead | None,
        task_loop: TaskLoop | None,
        task_loop_execution: TaskLoopExecutionRead | None,
    ) -> WorkbenchStage:
        if task_loop_execution is not None:
            execution = task_loop_execution.execution
            if task_loop_execution.loop_status == "observed":
                return WorkbenchStage.INTERPRETING
            if execution is None:
                return WorkbenchStage.PLANNED
            if execution.status == "succeeded":
                return WorkbenchStage.DELIVERED
            if execution.status in {"failed", "cancelled"}:
                return WorkbenchStage.BLOCKED
            if task_loop_execution.phase == "awaiting_user":
                return WorkbenchStage.NEEDS_USER_ACTION
            if task_loop_execution.phase == "verify":
                return WorkbenchStage.AWAITING_VERIFICATION
            return WorkbenchStage.EXECUTING
        if task_loop is not None:
            if task_loop.status == "observed":
                return WorkbenchStage.INTERPRETING
            if task_loop.status == "planned":
                return WorkbenchStage.PLANNED
            return WorkbenchStage.BLOCKED
        if turn_planning is not None:
            if turn_planning.run.status in {"prepared", "dispatching"}:
                return WorkbenchStage.INTERPRETING
            if TaskWorkbenchService._is_planner_only_single(turn_planning):
                return WorkbenchStage.PLANNED
            adjudication = turn_planning.adjudication
            if adjudication is not None:
                if adjudication.outcome == "needs_user_input":
                    return WorkbenchStage.NEEDS_CLARIFICATION
                if adjudication.outcome in {"multi_step_deferred", "unsupported"}:
                    return WorkbenchStage.UNSUPPORTED
        if route is not None:
            if route.decision is TurnRouteDecision.NEEDS_CLARIFICATION:
                return WorkbenchStage.NEEDS_CLARIFICATION
            if route.decision is TurnRouteDecision.UNSUPPORTED:
                return WorkbenchStage.UNSUPPORTED
            if route.status is TurnRouteStatus.NEEDS_USER_ACTION:
                return WorkbenchStage.NEEDS_USER_ACTION
            if route.status is TurnRouteStatus.WAITING_USER_INPUT:
                latest = executions.runs[-1] if executions.runs else None
                if latest is not None and latest.status in {
                    ExecutionRunStatus.CANCELLED,
                    ExecutionRunStatus.FAILED,
                }:
                    return WorkbenchStage.BLOCKED
                return WorkbenchStage.NEEDS_CLARIFICATION
            if route.status is TurnRouteStatus.SUCCEEDED:
                return WorkbenchStage.DELIVERED
            if route.status is TurnRouteStatus.FAILED:
                return WorkbenchStage.BLOCKED
            if route.route_id != "research_to_html":
                return WorkbenchStage.EXECUTING
        if any(item.status == "committed" for item in exports):
            return WorkbenchStage.EXPORTED
        if delivery is not None:
            return WorkbenchStage.DELIVERED
        if not executions.runs:
            return WorkbenchStage.PLANNED if planning is not None else WorkbenchStage.IDLE
        run = executions.runs[-1]
        nodes = {item.local_key: item for item in run.nodes}
        if run.status in {
            ExecutionRunStatus.CANCELLED,
            ExecutionRunStatus.FAILED,
            ExecutionRunStatus.SUPERSEDED,
        } or any(item.status is ExecutionNodeStatus.FAILED for item in run.nodes):
            return WorkbenchStage.BLOCKED
        if run.status is ExecutionRunStatus.SUCCEEDED and delivery is None:
            return WorkbenchStage.BLOCKED
        if (
            nodes.get("final_acceptance")
            and nodes["final_acceptance"].status is ExecutionNodeStatus.READY
        ):
            return WorkbenchStage.READY_TO_DELIVER
        if (
            nodes.get("browser_verify")
            and nodes["browser_verify"].status is ExecutionNodeStatus.READY
        ):
            return WorkbenchStage.VERIFYING_BROWSER
        if nodes.get("build_html") and nodes["build_html"].status is ExecutionNodeStatus.READY:
            return WorkbenchStage.BUILDING_ARTIFACT
        if (
            nodes.get("research")
            and nodes["research"].status is ExecutionNodeStatus.AWAITING_VERIFICATION
        ):
            return WorkbenchStage.AWAITING_VERIFICATION
        return WorkbenchStage.RESEARCHING

    @staticmethod
    def _repair_loop_status(
        executions: ExecutionRunPage,
        contract: TaskContractVersionRead | None,
        route: TurnRouteRead | None,
    ) -> AgentRepairLoopStatus | None:
        run = executions.runs[-1] if executions.runs else None
        if (
            run is None
            or route is None
            or route.route_id != "workspace_dynamic_patch_test"
            or contract is None
            or BOUNDED_PATCH_REPAIR_LOOP_CONSTRAINT not in contract.contract.constraints
        ):
            return None
        maximum_plan_generations = condition_replan_generation_limit(contract.contract.constraints)
        if maximum_plan_generations is None:
            raise TaskWorkbenchConflictError(
                "Patch repair-loop Contract constraints are incomplete"
            )
        budget_limit = AgentReplanBudgetTotals.from_task_budget(contract.contract.budget)
        budget_allocated = AgentReplanBudgetTotals.from_plan_budgets(
            node.budget
            for execution in executions.runs
            if execution.plan_generation <= run.plan_generation
            for node in execution.nodes
        )
        if not budget_limit.contains(budget_allocated):
            raise TaskWorkbenchConflictError(
                "Patch repair-loop cross-generation budget proof was exceeded"
            )
        next_plan = workspace_dynamic_patch_test_draft(
            run.task_id,
            contract.contract.version,
        )
        next_plan_allocation = AgentReplanBudgetTotals.from_plan_budgets(
            node.budget for node in next_plan.nodes
        )
        budget_remaining = budget_limit.remaining_after(budget_allocated)
        remaining_replans = max(
            0,
            maximum_plan_generations - run.plan_generation,
        )
        next_replan_available = bool(
            remaining_replans > 0 and budget_remaining.contains(next_plan_allocation)
        )
        reason_code = (
            "AVAILABLE"
            if next_replan_available
            else (
                "GENERATION_LIMIT_REACHED"
                if remaining_replans == 0
                else "CROSS_GENERATION_BUDGET_EXHAUSTED"
            )
        )
        material = {
            "schema_version": "deskpilot.agent-repair-loop-status.v1",
            "task_id": run.task_id,
            "current_plan_generation": run.plan_generation,
            "maximum_plan_generations": maximum_plan_generations,
            "remaining_replans": remaining_replans,
            "budget_limit": budget_limit,
            "budget_allocated": budget_allocated,
            "budget_remaining": budget_remaining,
            "next_plan_allocation": next_plan_allocation,
            "next_replan_available": next_replan_available,
            "reason_code": reason_code,
        }
        return AgentRepairLoopStatus.model_validate(
            {**material, "status_digest": sha256_digest(material)}
        )

    @staticmethod
    def _actions(
        executions: ExecutionRunPage,
        contract: TaskContractVersionRead | None,
        delivery: DeliveryManifestRead | None,
        route: TurnRouteRead | None,
        mcp_enabled: bool,
        repair_loop: AgentRepairLoopStatus | None,
        turn_planning: TurnPlanningRead | None,
        task_loop: TaskLoop | None,
        task_loop_execution: TaskLoopExecutionRead | None,
    ) -> tuple[WorkbenchActionRead, ...]:
        run = executions.runs[-1] if executions.runs else None
        nodes = {item.local_key: item for item in run.nodes} if run else {}
        has_plan = contract is not None
        planner_only_single = TaskWorkbenchService._is_planner_only_single(turn_planning)
        deferred_task_loop = bool(
            turn_planning is not None
            and turn_planning.run.status == "succeeded"
            and turn_planning.adjudication is not None
            and turn_planning.adjudication.outcome == "multi_step_deferred"
            and turn_planning.binding is not None
            and turn_planning.binding.status == "multi_step_deferred"
        )
        is_research = route is None or route.route_id == "research_to_html"
        is_direct = bool(
            route is not None
            and route.route_id
            in {
                "knowledge_lookup",
                "mcp_text_metrics",
                "workspace_file_read",
                "workspace_directory_list",
                "workspace_directory_analyze",
                "workspace_snapshot_check",
                "workspace_python_test",
                "workspace_node_test",
                "workspace_agent_patch_test",
                "workspace_dynamic_patch_test",
            }
        )
        export_allowed = bool(
            delivery is not None
            and contract is not None
            and contract.contract.workspace is not None
            and contract.contract.workspace.allow_user_path_export
        )
        uses_generic_task_loop = task_loop_execution is not None
        generic_execution = (
            task_loop_execution.execution if task_loop_execution is not None else None
        )
        conditions = {
            WorkbenchAction.INTERPRET_TURN: (
                bool(turn_planning is not None and turn_planning.run.status == "prepared"),
                "TURN_INTERPRETATION_NOT_PREPARED",
            ),
            WorkbenchAction.PLAN_TASK_LOOP: (
                bool(
                    (deferred_task_loop or planner_only_single)
                    and (task_loop is None or task_loop.status == "observed")
                ),
                "TASK_LOOP_PLAN_NOT_RECOVERABLE",
            ),
            WorkbenchAction.ADVANCE_TASK_LOOP: (
                bool(
                    task_loop_execution is not None
                    and task_loop_execution.loop_status == "planned"
                    and task_loop_execution.recoverable
                    and (
                        generic_execution is None
                        or generic_execution.status in {"active", "repairing"}
                    )
                ),
                "TASK_LOOP_NOT_RECOVERABLE",
            ),
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: (
                not uses_generic_task_loop and is_research and route is None and not has_plan,
                "TASK_ALREADY_PLANNED_OR_NOT_RESEARCH",
            ),
            WorkbenchAction.START_EXECUTION: (
                bool(
                    has_plan
                    and run is None
                    and not uses_generic_task_loop
                    and not planner_only_single
                    and (
                        route is None
                        or (
                            route.decision is TurnRouteDecision.ROUTED
                            and route.status is not TurnRouteStatus.FAILED
                        )
                    )
                ),
                "EXECUTION_ALREADY_STARTED",
            ),
            WorkbenchAction.RUN_RESEARCH: (
                bool(
                    not uses_generic_task_loop
                    and nodes.get("research")
                    and nodes["research"].status is ExecutionNodeStatus.READY
                ),
                "RESEARCH_NOT_READY",
            ),
            WorkbenchAction.VERIFY_CLAIMS: (
                bool(
                    not uses_generic_task_loop
                    and nodes.get("research")
                    and nodes["research"].status is ExecutionNodeStatus.AWAITING_VERIFICATION
                ),
                "CLAIMS_NOT_AWAITING_VERIFICATION",
            ),
            WorkbenchAction.BUILD_ARTIFACT: (
                bool(
                    not uses_generic_task_loop
                    and nodes.get("build_html")
                    and nodes["build_html"].status is ExecutionNodeStatus.READY
                ),
                "VERIFIED_EDGE_NOT_READY",
            ),
            WorkbenchAction.VERIFY_BROWSER: (
                bool(
                    not uses_generic_task_loop
                    and nodes.get("browser_verify")
                    and nodes["browser_verify"].status is ExecutionNodeStatus.READY
                ),
                "ARTIFACT_EDGE_NOT_READY",
            ),
            WorkbenchAction.FINALIZE_DELIVERY: (
                bool(
                    not uses_generic_task_loop
                    and delivery is None
                    and nodes.get("final_acceptance")
                    and nodes["final_acceptance"].status is ExecutionNodeStatus.READY
                ),
                "BROWSER_EDGE_NOT_READY",
            ),
            WorkbenchAction.EXECUTE_ROUTE: (
                bool(
                    not uses_generic_task_loop
                    and is_direct
                    and run is not None
                    and run.status is ExecutionRunStatus.ACTIVE
                    and route is not None
                    and route.status
                    in {
                        TurnRouteStatus.READY,
                        TurnRouteStatus.NEEDS_USER_ACTION,
                        TurnRouteStatus.RUNNING,
                    }
                    and (route.route_id != "mcp_text_metrics" or mcp_enabled)
                    and any(
                        node.status is ExecutionNodeStatus.READY
                        and (
                            node.local_key
                            in {
                                "knowledge_lookup",
                                "mcp_text_metrics",
                                "workspace_file_read",
                                "workspace_directory_list",
                                "workspace_directory_analyze",
                                "workspace_snapshot_check",
                                "workspace_python_test",
                                "workspace_node_test",
                                "workspace_agent_patch_test",
                                "workspace_dynamic_patch_test",
                            }
                            or (
                                route.route_id
                                in {
                                    "workspace_directory_list",
                                    "workspace_directory_analyze",
                                    "workspace_dynamic_patch_test",
                                }
                                and node.bound_agent is not None
                            )
                        )
                        for node in run.nodes
                    )
                ),
                "ROUTE_NOT_READY_OR_USER_ACTION_REQUIRED",
            ),
            WorkbenchAction.REPLAN_FAILED_EXECUTION: (
                bool(
                    route is not None
                    and route.status is TurnRouteStatus.FAILED
                    and (
                        (
                            route.route_id == "workspace_directory_analyze"
                            and route.error_code
                            in {
                                "AGENT_TASK_GRAPH_REJECTED",
                                "AGENT_ROUTE_BINDING_REJECTED",
                                "AGENT_LOOP_NO_PROGRESS",
                            }
                        )
                        or (
                            route.route_id == "workspace_dynamic_patch_test"
                            and route.error_code == "AGENT_GRAPH_TEST_CONDITION_NOT_MET"
                            and contract is not None
                            and (
                                (repair_loop is not None and repair_loop.next_replan_available)
                                or (
                                    repair_loop is None
                                    and condition_replan_generation_limit(
                                        contract.contract.constraints
                                    )
                                    == 2
                                    and run is not None
                                    and run.plan_generation < 2
                                )
                            )
                        )
                    )
                    and run is not None
                    and run.status is ExecutionRunStatus.FAILED
                    and (
                        route.route_id == "workspace_dynamic_patch_test" or run.plan_generation == 1
                    )
                ),
                "FAILURE_NOT_REPLAN_ELIGIBLE_OR_LIMIT_REACHED",
            ),
            WorkbenchAction.COMMIT_WORKSPACE_EDIT: (
                bool(
                    route is not None
                    and route.route_id == "workspace_file_replace"
                    and route.status in {TurnRouteStatus.NEEDS_USER_ACTION, TurnRouteStatus.RUNNING}
                    and route.result_digest is not None
                    and run is not None
                    and run.status is ExecutionRunStatus.ACTIVE
                    and any(
                        node.local_key == "workspace_file_replace"
                        and node.status in {ExecutionNodeStatus.READY, ExecutionNodeStatus.RUNNING}
                        for node in run.nodes
                    )
                ),
                "WORKSPACE_EDIT_PREVIEW_OR_CONFIRMATION_MISSING",
            ),
            WorkbenchAction.COMMIT_WORKSPACE_PATCH: (
                bool(
                    (
                        task_loop_execution is not None
                        and generic_execution is not None
                        and generic_execution.status == "awaiting_user"
                        and isinstance(
                            task_loop_execution.workspace_patch,
                            WorkspacePatchPreview,
                        )
                        and any(
                            node.status == "waiting_user"
                            for node in task_loop_execution.nodes
                        )
                    )
                    or (
                        route is not None
                        and route.route_id
                        in {
                            "workspace_patch_bundle",
                            "workspace_agent_patch_test",
                            "workspace_dynamic_patch_test",
                        }
                        and route.status
                        in {TurnRouteStatus.NEEDS_USER_ACTION, TurnRouteStatus.RUNNING}
                        and route.result_digest is not None
                        and run is not None
                        and run.status
                        in {
                            ExecutionRunStatus.ACTIVE,
                            ExecutionRunStatus.PAUSED,
                        }
                        and any(
                            (
                                node.local_key == route.route_id
                                or (
                                    route.route_id == "workspace_dynamic_patch_test"
                                    and node.status is ExecutionNodeStatus.WAITING_USER
                                )
                            )
                            and node.status
                            in {
                                ExecutionNodeStatus.READY,
                                ExecutionNodeStatus.RUNNING,
                                ExecutionNodeStatus.WAITING_USER,
                            }
                            for node in run.nodes
                        )
                    )
                ),
                "WORKSPACE_PATCH_PREVIEW_OR_CONFIRMATION_MISSING",
            ),
            WorkbenchAction.COMMIT_WORKSPACE_GIT: (
                bool(
                    task_loop_execution is not None
                    and generic_execution is not None
                    and generic_execution.status == "awaiting_user"
                    and isinstance(task_loop_execution.git_commit, GitCommitPreview)
                    and any(
                        node.local_key.endswith("commit_git")
                        and node.status == "waiting_user"
                        for node in task_loop_execution.nodes
                    )
                ),
                "GIT_COMMIT_PREVIEW_OR_CONFIRMATION_MISSING",
            ),
            WorkbenchAction.COMMIT_WORKSPACE_PATH_OPERATION: (
                bool(
                    route is not None
                    and route.route_id in {"workspace_file_create", "workspace_file_rename"}
                    and route.status in {TurnRouteStatus.NEEDS_USER_ACTION, TurnRouteStatus.RUNNING}
                    and route.result_digest is not None
                    and run is not None
                    and run.status is ExecutionRunStatus.ACTIVE
                    and any(
                        node.local_key == route.route_id
                        and node.status in {ExecutionNodeStatus.READY, ExecutionNodeStatus.RUNNING}
                        for node in run.nodes
                    )
                ),
                "WORKSPACE_PATH_OPERATION_PREVIEW_OR_CONFIRMATION_MISSING",
            ),
            WorkbenchAction.PREPARE_EXPORT: (
                export_allowed,
                "DELIVERY_OR_EXPORT_AUTHORIZATION_MISSING",
            ),
            WorkbenchAction.STOP_EXECUTION: (
                bool(
                    not uses_generic_task_loop
                    and (
                        run is not None
                        and run.status.value in {"active", "awaiting_verification", "paused"}
                    )
                    or (
                        turn_planning is not None
                        and turn_planning.run.status in {"prepared", "dispatching"}
                    )
                ),
                "EXECUTION_NOT_ACTIVE",
            ),
        }
        explanations = {
            WorkbenchAction.INTERPRET_TURN: (
                "运行一次本地 Turn Planner，只能选择服务器预编译的 opaque Offer。"
            ),
            WorkbenchAction.PLAN_TASK_LOOP: (
                "复验已持久化 Offer，并组合一个不授予新权限的多步骤 DraftPlan。"
            ),
            WorkbenchAction.ADVANCE_TASK_LOOP: (
                "按持久 reducer 只推进一个已绑定节点，并在重启后从证明状态恢复。"
            ),
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: "启用受信 research_to_html 计划。",
            WorkbenchAction.START_EXECUTION: "创建绑定当前计划的执行运行。",
            WorkbenchAction.RUN_RESEARCH: "运行受控搜索与页面读取。",
            WorkbenchAction.VERIFY_CLAIMS: "独立核验 Claim 与 Citation。",
            WorkbenchAction.BUILD_ARTIFACT: "只使用已验证事实生成工作区 HTML。",
            WorkbenchAction.VERIFY_BROWSER: "在无登录、断网浏览器中验收 HTML。",
            WorkbenchAction.FINALIZE_DELIVERY: "根据完整证据形成交付清单。",
            WorkbenchAction.EXECUTE_ROUTE: "执行当前受信只读能力并复核结果证明。",
            WorkbenchAction.REPLAN_FAILED_EXECUTION: (
                "封存失败快照，并原子激活同一合同下的一代替换计划。"
            ),
            WorkbenchAction.COMMIT_WORKSPACE_EDIT: "提交已确认的单次文本替换并保留安全备份。",
            WorkbenchAction.COMMIT_WORKSPACE_PATCH: "提交已确认的多文件补丁并保留逐项回执。",
            WorkbenchAction.COMMIT_WORKSPACE_GIT: (
                "在服务器命名的新分支提交已测试文件；关闭 hooks、签名和 push。"
            ),
            WorkbenchAction.COMMIT_WORKSPACE_PATH_OPERATION: (
                "提交已确认的新建或重命名，并核验路径与版本证明。"
            ),
            WorkbenchAction.PREPARE_EXPORT: "预览精确用户路径写入；不会覆盖。",
            WorkbenchAction.STOP_EXECUTION: "立即停止运行并使未完成领取凭证失效。",
        }
        effects = {
            WorkbenchAction.INTERPRET_TURN: "read_only",
            WorkbenchAction.PLAN_TASK_LOOP: "read_only",
            WorkbenchAction.ADVANCE_TASK_LOOP: "execution_control",
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: "read_only",
            WorkbenchAction.START_EXECUTION: "read_only",
            WorkbenchAction.RUN_RESEARCH: "read_only",
            WorkbenchAction.VERIFY_CLAIMS: "read_only",
            WorkbenchAction.BUILD_ARTIFACT: "workspace_write",
            WorkbenchAction.VERIFY_BROWSER: "read_only",
            WorkbenchAction.FINALIZE_DELIVERY: "read_only",
            WorkbenchAction.EXECUTE_ROUTE: "read_only",
            WorkbenchAction.REPLAN_FAILED_EXECUTION: "execution_control",
            WorkbenchAction.COMMIT_WORKSPACE_EDIT: "user_path_write",
            WorkbenchAction.COMMIT_WORKSPACE_PATCH: "user_path_write",
            WorkbenchAction.COMMIT_WORKSPACE_GIT: "workspace_write",
            WorkbenchAction.COMMIT_WORKSPACE_PATH_OPERATION: "user_path_write",
            WorkbenchAction.PREPARE_EXPORT: "user_path_write",
            WorkbenchAction.STOP_EXECUTION: "execution_control",
        }
        return tuple(
            WorkbenchActionRead(
                action=action,
                enabled=enabled,
                reason_code="AVAILABLE" if enabled else reason,
                explanation=explanations[action],
                effect_class=cast(
                    Literal[
                        "read_only",
                        "workspace_write",
                        "user_path_write",
                        "execution_control",
                    ],
                    effects[action],
                ),
            )
            for action, (enabled, reason) in conditions.items()
        )

    @staticmethod
    def _is_planner_only_single(turn_planning: TurnPlanningRead | None) -> bool:
        if turn_planning is None:
            return False
        adjudication = turn_planning.adjudication
        binding = turn_planning.binding
        if (
            turn_planning.run.status != "succeeded"
            or adjudication is None
            or binding is None
            or adjudication.outcome != "single_step"
            or binding.status != "task_loop_deferred"
            or binding.offer is None
            or len(adjudication.selected_offers) != 1
            or binding.offer != adjudication.selected_offers[0]
        ):
            return False
        return any(
            item.ref == binding.offer
            and RouteRecipeCatalog.is_planner_only_route(item.trusted_recipe.route_id)
            for item in turn_planning.offers
        )
