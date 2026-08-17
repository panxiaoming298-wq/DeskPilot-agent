"""Explicit, feature-gated Agent execution and research commands."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from deskpilot.api.dependencies import (
    get_agent_execution_runtime,
    get_artifact_delivery_runtime,
    get_long_term_memory_runtime,
    get_research_runtime,
)
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.agent_execution_runtime import (
    AgentExecutionRuntime,
    AgentRuntimeDisabledError,
    AgentRuntimeError,
    AgentRuntimeNotFoundError,
)
from deskpilot.application.artifact_delivery_runtime import (
    ArtifactDeliveryError,
    ArtifactDeliveryNotFoundError,
    ArtifactDeliveryRuntime,
)
from deskpilot.application.long_term_memory_runtime import (
    LongTermMemoryError,
    LongTermMemoryRuntime,
)
from deskpilot.application.research_runtime import ResearchRuntime, ResearchRuntimeError
from deskpilot.domain.agent_runtime import (
    RUN_ID_PATTERN,
    ExecutionRunPage,
    ExecutionRunRead,
)
from deskpilot.domain.artifact_runtime import (
    PATCH_RECEIPT_ID_PATTERN,
    WORKSPACE_ID_PATTERN,
    BrowserRenderRunRead,
    DeliveryManifestRead,
    PatchReceiptRead,
    TaskWorkspaceRead,
    VerificationRunRead,
)
from deskpilot.domain.research import RESEARCH_SESSION_ID_PATTERN, ResearchSessionRead
from deskpilot.domain.task_plans import TASK_ID_PATTERN

router = APIRouter(tags=["agent-runtime"])
ExecutionDependency = Annotated[AgentExecutionRuntime, Depends(get_agent_execution_runtime)]
ResearchDependency = Annotated[ResearchRuntime, Depends(get_research_runtime)]
ArtifactDependency = Annotated[ArtifactDeliveryRuntime, Depends(get_artifact_delivery_runtime)]
MemoryDependency = Annotated[LongTermMemoryRuntime, Depends(get_long_term_memory_runtime)]
TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]
RunId = Annotated[str, Path(pattern=RUN_ID_PATTERN)]
ResearchSessionId = Annotated[str, Path(pattern=RESEARCH_SESSION_ID_PATTERN)]
WorkspaceId = Annotated[str, Path(pattern=WORKSPACE_ID_PATTERN)]
PatchReceiptId = Annotated[str, Path(pattern=PATCH_RECEIPT_ID_PATTERN)]


@router.post(
    "/tasks/{task_id}/execution-runs",
    response_model=ExecutionRunRead,
    status_code=status.HTTP_201_CREATED,
)
async def start_execution(
    task_id: TaskId,
    execution: ExecutionDependency,
    research: ResearchDependency,
    response: Response,
) -> ExecutionRunRead:
    del research
    response.headers["Cache-Control"] = "no-store"
    try:
        return await execution.start(task_id)
    except AgentRuntimeError as error:
        raise _runtime_problem(error) from error


@router.post(
    "/execution-runs/{run_id}/research:run",
    response_model=ResearchSessionRead,
)
async def run_research(
    run_id: RunId,
    execution: ExecutionDependency,
    research: ResearchDependency,
    response: Response,
) -> ResearchSessionRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        claimed = await execution.claim_next(run_id, "api-research-v1", lease_seconds=600)
        if claimed is None:
            raise ProblemException(
                status_code=409,
                code="NO_RESEARCH_NODE_READY",
                title="没有可运行的研究节点",
                detail="运行已被领取、停止，或正在等待验证。",
            )
        return await research.run(claimed)
    except AgentRuntimeError as error:
        raise _runtime_problem(error) from error
    except ResearchRuntimeError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="研究运行被拒绝",
            detail=str(error),
        ) from error


@router.get("/execution-runs/{run_id}", response_model=ExecutionRunRead)
async def get_execution(
    run_id: RunId,
    execution: ExecutionDependency,
    response: Response,
) -> ExecutionRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await execution.get(run_id)
    except AgentRuntimeError as error:
        raise _runtime_problem(error) from error


@router.get("/tasks/{task_id}/execution-runs", response_model=ExecutionRunPage)
async def list_executions(
    task_id: TaskId,
    execution: ExecutionDependency,
    response: Response,
) -> ExecutionRunPage:
    response.headers["Cache-Control"] = "no-store"
    return await execution.list_for_task(task_id)


@router.get(
    "/research-sessions/{research_session_id}",
    response_model=ResearchSessionRead,
)
async def get_research(
    research_session_id: ResearchSessionId,
    research: ResearchDependency,
    response: Response,
) -> ResearchSessionRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await research.get(research_session_id)
    except ResearchRuntimeError as error:
        raise ProblemException(
            status_code=404,
            code=error.code,
            title="研究记录不存在",
            detail=str(error),
        ) from error


@router.post(
    "/execution-runs/{run_id}/claims:verify",
    response_model=VerificationRunRead,
)
async def verify_claims(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> VerificationRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.verify_research(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.get(
    "/execution-runs/{run_id}/claim-verification",
    response_model=VerificationRunRead,
)
async def get_claim_verification(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> VerificationRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_verification(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.post(
    "/execution-runs/{run_id}/artifacts:build",
    response_model=TaskWorkspaceRead,
)
async def build_artifact(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> TaskWorkspaceRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.build_html(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.get("/task-workspaces/{workspace_id}", response_model=TaskWorkspaceRead)
async def get_workspace(
    workspace_id: WorkspaceId,
    runtime: ArtifactDependency,
    response: Response,
) -> TaskWorkspaceRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_workspace(workspace_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.get("/patch-receipts/{patch_receipt_id}", response_model=PatchReceiptRead)
async def get_patch_receipt(
    patch_receipt_id: PatchReceiptId,
    runtime: ArtifactDependency,
    response: Response,
) -> PatchReceiptRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_patch_receipt(patch_receipt_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.post(
    "/execution-runs/{run_id}/browser:verify",
    response_model=BrowserRenderRunRead,
)
async def verify_browser(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> BrowserRenderRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.verify_browser(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.get(
    "/execution-runs/{run_id}/browser-verification",
    response_model=BrowserRenderRunRead,
)
async def get_browser_verification(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> BrowserRenderRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_browser(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


@router.post(
    "/execution-runs/{run_id}/final-acceptance:run",
    response_model=DeliveryManifestRead,
)
async def finalize_delivery(
    run_id: RunId,
    runtime: ArtifactDependency,
    memory: MemoryDependency,
    response: Response,
) -> DeliveryManifestRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        delivery = await runtime.finalize(run_id)
        await memory.propose_verified_episode(delivery)
        return delivery
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error
    except LongTermMemoryError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="已验证交付的记忆提案失败",
            detail=str(error),
        ) from error


@router.get(
    "/execution-runs/{run_id}/delivery",
    response_model=DeliveryManifestRead,
)
async def get_delivery(
    run_id: RunId,
    runtime: ArtifactDependency,
    response: Response,
) -> DeliveryManifestRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get_delivery(run_id)
    except ArtifactDeliveryError as error:
        raise _artifact_problem(error) from error


def _runtime_problem(error: AgentRuntimeError) -> ProblemException:
    if isinstance(error, AgentRuntimeNotFoundError):
        status_code = 404
    elif isinstance(error, AgentRuntimeDisabledError):
        status_code = 409
    else:
        status_code = 409
    return ProblemException(
        status_code=status_code,
        code=error.code,
        title="Agent 运行命令被拒绝",
        detail=str(error),
    )


def _artifact_problem(error: ArtifactDeliveryError) -> ProblemException:
    return ProblemException(
        status_code=404 if isinstance(error, ArtifactDeliveryNotFoundError) else 409,
        code=error.code,
        title="验证或 Artifact 命令被拒绝",
        detail=str(error),
    )
