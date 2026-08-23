"""Unified phase-76 Task Workbench and exact Artifact export commands."""

from typing import Annotated

from fastapi import APIRouter, Depends, Header, Path, Response, status

from deskpilot.api.dependencies import (
    get_artifact_export_runtime,
    get_task_workbench_service,
)
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.artifact_export_runtime import (
    ArtifactExportConflictError,
    ArtifactExportError,
    ArtifactExportNotFoundError,
    ArtifactExportPathRejectedError,
    ArtifactExportProofRejectedError,
    ArtifactExportRuntime,
)
from deskpilot.application.task_workbench_service import (
    TaskWorkbenchError,
    TaskWorkbenchNotFoundError,
    TaskWorkbenchService,
)
from deskpilot.domain.artifact_runtime import DELIVERY_ID_PATTERN
from deskpilot.domain.task_plans import TASK_ID_PATTERN
from deskpilot.domain.task_workbench import (
    ARTIFACT_EXPORT_ID_PATTERN,
    ArtifactExportRead,
    CommitArtifactExport,
    ContinueConversationTurn,
    CreateConversationTurn,
    CreateResearchWorkbenchTask,
    PrepareArtifactExport,
    TaskWorkbenchRead,
)
from deskpilot.domain.workspace_files import (
    CommitWorkspaceEdit,
    CommitWorkspacePatch,
    CommitWorkspacePathOperation,
)

router = APIRouter(tags=["task-workbench"])
WorkbenchDependency = Annotated[TaskWorkbenchService, Depends(get_task_workbench_service)]
ExportDependency = Annotated[ArtifactExportRuntime, Depends(get_artifact_export_runtime)]
TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]
DeliveryId = Annotated[str, Path(pattern=DELIVERY_ID_PATTERN)]
ExportId = Annotated[str, Path(pattern=ARTIFACT_EXPORT_ID_PATTERN)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=8, max_length=200)]


@router.post(
    "/conversation-turns",
    response_model=TaskWorkbenchRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation_turn(
    command: CreateConversationTurn,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.create_turn(command)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/conversation-turns",
    response_model=TaskWorkbenchRead,
    status_code=status.HTTP_201_CREATED,
)
async def continue_conversation_turn(
    task_id: TaskId,
    command: ContinueConversationTurn,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.continue_turn(task_id, command)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workbench:advance",
    response_model=TaskWorkbenchRead,
)
async def advance_task_workbench(
    task_id: TaskId,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.advance(task_id)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workbench:stop",
    response_model=TaskWorkbenchRead,
)
async def stop_task_workbench(
    task_id: TaskId,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.stop(task_id)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workbench:replan",
    response_model=TaskWorkbenchRead,
)
async def replan_task_workbench(
    task_id: TaskId,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.replan_failed_execution(task_id)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workspace-edit:commit",
    response_model=TaskWorkbenchRead,
)
async def commit_workspace_edit(
    task_id: TaskId,
    command: CommitWorkspaceEdit,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.commit_workspace_edit(task_id, command.confirmation_digest)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workspace-patch:commit",
    response_model=TaskWorkbenchRead,
)
async def commit_workspace_patch(
    task_id: TaskId,
    command: CommitWorkspacePatch,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.commit_workspace_patch(task_id, command.confirmation_digest)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/workspace-path-operation:commit",
    response_model=TaskWorkbenchRead,
)
async def commit_workspace_path_operation(
    task_id: TaskId,
    command: CommitWorkspacePathOperation,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.commit_workspace_path_operation(task_id, command.confirmation_digest)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/research-workbench/tasks",
    response_model=TaskWorkbenchRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_research_task(
    command: CreateResearchWorkbenchTask,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.create(command)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/tasks/{task_id}/research-workbench:activate",
    response_model=TaskWorkbenchRead,
)
async def activate_research_task(
    task_id: TaskId,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.activate(task_id)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.get("/tasks/{task_id}/workbench", response_model=TaskWorkbenchRead)
async def get_task_workbench(
    task_id: TaskId,
    service: WorkbenchDependency,
    response: Response,
) -> TaskWorkbenchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get(task_id)
    except TaskWorkbenchError as error:
        raise _workbench_problem(error) from error


@router.post(
    "/deliveries/{delivery_id}/exports:prepare",
    response_model=ArtifactExportRead,
    status_code=status.HTTP_201_CREATED,
)
async def prepare_artifact_export(
    delivery_id: DeliveryId,
    command: PrepareArtifactExport,
    idempotency_key: IdempotencyKey,
    runtime: ExportDependency,
    response: Response,
) -> ArtifactExportRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.prepare(
            delivery_id,
            command.target_path,
            idempotency_key,
            artifact_id=command.artifact_id,
        )
    except ArtifactExportError as error:
        raise _export_problem(error) from error


@router.post(
    "/artifact-exports/{export_id}:commit",
    response_model=ArtifactExportRead,
)
async def commit_artifact_export(
    export_id: ExportId,
    command: CommitArtifactExport,
    idempotency_key: IdempotencyKey,
    runtime: ExportDependency,
    response: Response,
) -> ArtifactExportRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.commit(
            export_id,
            command.confirmation_digest,
            idempotency_key,
        )
    except ArtifactExportError as error:
        raise _export_problem(error) from error


@router.get("/artifact-exports/{export_id}", response_model=ArtifactExportRead)
async def get_artifact_export(
    export_id: ExportId,
    runtime: ExportDependency,
    response: Response,
) -> ArtifactExportRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.get(export_id)
    except ArtifactExportError as error:
        raise _export_problem(error) from error


def _workbench_problem(error: TaskWorkbenchError) -> ProblemException:
    return ProblemException(
        status_code=404 if isinstance(error, TaskWorkbenchNotFoundError) else 409,
        code=error.code,
        title="任务工作台读取或创建被拒绝",
        detail=str(error),
    )


def _export_problem(error: ArtifactExportError) -> ProblemException:
    if isinstance(error, ArtifactExportNotFoundError):
        status_code = 404
    elif isinstance(error, ArtifactExportPathRejectedError):
        status_code = 400
    elif isinstance(error, (ArtifactExportConflictError, ArtifactExportProofRejectedError)):
        status_code = 409
    else:
        status_code = 400
    return ProblemException(
        status_code=status_code,
        code=error.code,
        title="Artifact 精确导出被拒绝",
        detail=str(error),
    )
