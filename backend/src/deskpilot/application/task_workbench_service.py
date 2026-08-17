"""Server-owned phase-76 Conversation/Research/Artifact projection."""

from typing import Literal, cast

from sqlalchemy import select

from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryNotFoundError,
    ArtifactDeliveryRuntime,
)
from deskpilot.application.artifact_export_runtime import (
    ArtifactExportError,
    ArtifactExportRuntime,
)
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.context_memory_runtime import ContextMemoryRuntime
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningNotFoundError,
)
from deskpilot.application.plan_compiler import (
    research_to_html_contract,
    research_to_html_draft,
)
from deskpilot.application.task_service import TaskNotFoundError, TaskService
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import (
    ExecutionNodeStatus,
    ExecutionRunPage,
    ExecutionRunStatus,
)
from deskpilot.domain.artifact_runtime import DeliveryManifestRead
from deskpilot.domain.context_memory import (
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
from deskpilot.domain.task_plans import (
    ExecutablePlanPage,
    PlanningStateRead,
    TaskContractVersionRead,
)
from deskpilot.domain.task_workbench import (
    ArtifactExportRead,
    CreateResearchWorkbenchTask,
    TaskWorkbenchRead,
    WorkbenchAction,
    WorkbenchActionRead,
    WorkbenchStage,
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


class TaskWorkbenchService:
    def __init__(
        self,
        database: Database,
        tasks: TaskService,
        context: ContextMemoryRuntime,
        planning: PlanCompilationService,
        capabilities: CapabilityCatalog,
        execution: AgentExecutionRuntime,
        artifacts: ArtifactDeliveryRuntime,
        exports: ArtifactExportRuntime,
    ) -> None:
        self._database = database
        self._tasks = tasks
        self._context = context
        self._planning = planning
        self._capabilities = capabilities
        self._execution = execution
        self._artifacts = artifacts
        self._exports = exports

    async def create(self, command: CreateResearchWorkbenchTask) -> TaskWorkbenchRead:
        self._ensure_research_enabled()
        conversation = await self._context.create_conversation(
            CreateConversationRequest(title=command.goal.strip()[:200])
        )
        task = await self._tasks.create_task(
            TaskCreate(
                conversation_id=conversation.conversation_id,
                goal=command.goal,
                privacy_mode=command.privacy_mode,
                constraints=list(command.constraints),
            )
        )
        await self._context.add_message(
            conversation.conversation_id,
            CreateConversationMessageRequest(
                role="user",
                content=command.goal,
                task_id=task.task_id,
                classification=DataClassification.INTERNAL,
            ),
        )
        await self.activate(task.task_id)
        await self._execution.start(task.task_id)
        return await self.get(task.task_id)

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

    def _ensure_research_enabled(self) -> None:
        if not self._capabilities.resolve_preferred("research.read.v1").runtime_enabled:
            raise TaskWorkbenchConflictError(
                "Research runtime is disabled; configure a SearchProvider before creating this task"
            )

    async def get(self, task_id: str) -> TaskWorkbenchRead:
        try:
            task = await self._tasks.get_task(task_id)
        except TaskNotFoundError as error:
            raise TaskWorkbenchNotFoundError("Task does not exist") from error

        conversation = await self._context.list_task_messages(task_id)
        try:
            planning = await self._planning.get_state(task_id)
            contract = await self._planning.get_current_contract(task_id)
            plans = await self._planning.list_plans(task_id)
        except PlanningNotFoundError:
            planning = None
            contract = None
            plans = ExecutablePlanPage(plans=())

        executions = await self._execution.list_for_task(task_id)
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
        stage = self._stage(executions, planning, delivery, exports)
        actions = self._actions(executions, contract, delivery)
        material = {
            "schema_version": "deskpilot.task-workbench.v1",
            "task": task,
            "stage": stage,
            "actions": actions,
            "conversation": conversation,
            "planning": planning,
            "contract": contract,
            "plans": plans,
            "executions": executions,
            "research": research,
            "verification": verification,
            "workspace": workspace,
            "browser": browser,
            "delivery": delivery,
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
    ) -> WorkbenchStage:
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
    def _actions(
        executions: ExecutionRunPage,
        contract: TaskContractVersionRead | None,
        delivery: DeliveryManifestRead | None,
    ) -> tuple[WorkbenchActionRead, ...]:
        run = executions.runs[-1] if executions.runs else None
        nodes = {item.local_key: item for item in run.nodes} if run else {}
        has_plan = contract is not None
        export_allowed = bool(
            delivery is not None
            and contract is not None
            and contract.contract.workspace is not None
            and contract.contract.workspace.allow_user_path_export
        )
        conditions = {
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: (not has_plan, "TASK_ALREADY_PLANNED"),
            WorkbenchAction.START_EXECUTION: (
                has_plan and run is None,
                "EXECUTION_ALREADY_STARTED",
            ),
            WorkbenchAction.RUN_RESEARCH: (
                bool(
                    nodes.get("research")
                    and nodes["research"].status is ExecutionNodeStatus.READY
                ),
                "RESEARCH_NOT_READY",
            ),
            WorkbenchAction.VERIFY_CLAIMS: (
                bool(
                    nodes.get("research")
                    and nodes["research"].status is ExecutionNodeStatus.AWAITING_VERIFICATION
                ),
                "CLAIMS_NOT_AWAITING_VERIFICATION",
            ),
            WorkbenchAction.BUILD_ARTIFACT: (
                bool(
                    nodes.get("build_html")
                    and nodes["build_html"].status is ExecutionNodeStatus.READY
                ),
                "VERIFIED_EDGE_NOT_READY",
            ),
            WorkbenchAction.VERIFY_BROWSER: (
                bool(
                    nodes.get("browser_verify")
                    and nodes["browser_verify"].status is ExecutionNodeStatus.READY
                ),
                "ARTIFACT_EDGE_NOT_READY",
            ),
            WorkbenchAction.FINALIZE_DELIVERY: (
                bool(
                    delivery is None
                    and nodes.get("final_acceptance")
                    and nodes["final_acceptance"].status is ExecutionNodeStatus.READY
                ),
                "BROWSER_EDGE_NOT_READY",
            ),
            WorkbenchAction.PREPARE_EXPORT: (
                export_allowed,
                "DELIVERY_OR_EXPORT_AUTHORIZATION_MISSING",
            ),
            WorkbenchAction.STOP_EXECUTION: (
                bool(
                    run is not None
                    and run.status.value in {"active", "awaiting_verification", "paused"}
                ),
                "EXECUTION_NOT_ACTIVE",
            ),
        }
        explanations = {
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: "启用受信 research_to_html 计划。",
            WorkbenchAction.START_EXECUTION: "创建绑定当前计划的执行运行。",
            WorkbenchAction.RUN_RESEARCH: "运行受控搜索与页面读取。",
            WorkbenchAction.VERIFY_CLAIMS: "独立核验 Claim 与 Citation。",
            WorkbenchAction.BUILD_ARTIFACT: "只使用已验证事实生成工作区 HTML。",
            WorkbenchAction.VERIFY_BROWSER: "在无登录、断网浏览器中验收 HTML。",
            WorkbenchAction.FINALIZE_DELIVERY: "根据完整证据形成交付清单。",
            WorkbenchAction.PREPARE_EXPORT: "预览精确用户路径写入；不会覆盖。",
            WorkbenchAction.STOP_EXECUTION: "立即停止运行并使未完成领取凭证失效。",
        }
        effects = {
            WorkbenchAction.ACTIVATE_RESEARCH_PLAN: "read_only",
            WorkbenchAction.START_EXECUTION: "read_only",
            WorkbenchAction.RUN_RESEARCH: "read_only",
            WorkbenchAction.VERIFY_CLAIMS: "read_only",
            WorkbenchAction.BUILD_ARTIFACT: "workspace_write",
            WorkbenchAction.VERIFY_BROWSER: "read_only",
            WorkbenchAction.FINALIZE_DELIVERY: "read_only",
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
