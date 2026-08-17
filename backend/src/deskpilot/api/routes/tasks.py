"""Task command/query endpoints."""

import asyncio
import os
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from deskpilot.api.dependencies import get_processor, get_task_service, get_telemetry
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.effect_graph_control_router import (
    EffectGraphControlDeliveryTimeoutError,
)
from deskpilot.application.processor import TaskProcessor, TaskRuntimeUnavailableError
from deskpilot.application.task_service import (
    EffectGraphNotFoundError,
    InvalidTaskTransitionError,
    TaskNotFoundError,
    TaskService,
)
from deskpilot.domain.effect_graph import EffectGraphRead
from deskpilot.domain.schemas import (
    DiskPressureGuardedFileMoveRequest,
    FileMoveDagOperation,
    FileMoveDagRequest,
    FileMoveSagaOperation,
    FileMoveSagaRequest,
    FileMoveTaskRequest,
    TaskControlCommand,
    TaskCreate,
    TaskEventRead,
    TaskHistoryRead,
    TaskRead,
    TaskStatus,
)
from deskpilot.observability import TelemetryFacade
from deskpilot.runner.executor import ToolExecutorError
from deskpilot.tools.files import FileMoveInput, normalize_file_move_input

router = APIRouter(prefix="/tasks", tags=["tasks"])

TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]
ProcessorDependency = Annotated[TaskProcessor, Depends(get_processor)]
TelemetryDependency = Annotated[TelemetryFacade, Depends(get_telemetry)]
AfterSequence = Annotated[int, Query(ge=0)]
TaskStatusFilter = Annotated[TaskStatus | None, Query(alias="status")]
HistoryLimit = Annotated[int, Query(ge=1, le=100)]
HistoryOffset = Annotated[int, Query(ge=0)]


def _not_found_problem(task_id: str) -> ProblemException:
    return ProblemException(
        status_code=404,
        code="TASK_NOT_FOUND",
        title="任务不存在",
        detail=f"没有找到任务 {task_id}。",
    )


def _transition_problem(error: InvalidTaskTransitionError) -> ProblemException:
    return ProblemException(
        status_code=409,
        code="TASK_TRANSITION_NOT_ALLOWED",
        title="任务状态不允许此操作",
        detail=(f"任务当前为 {error.current.value}，不能切换到 {error.target.value}。"),
        extensions={
            "current_status": error.current.value,
            "target_status": error.target.value,
            "allowed_statuses": sorted(status.value for status in error.allowed),
        },
    )


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
async def create_task(
    command: TaskCreate,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    telemetry: TelemetryDependency,
) -> TaskRead:
    with telemetry.operation("deskpilot.task.accept", "task") as operation:
        normalized_command = await _normalize_task_command(command)
        task = await service.create_task(normalized_command)
        operation.set_attribute("deskpilot.subject.type", "task")
        operation.set_attribute("deskpilot.subject.id", task.task_id)
        events = await service.list_events(task.task_id)
        if events:
            operation.set_attribute(
                "deskpilot.task.correlation_id", events[0].trace_id
            )
        operation.set_outcome("accepted")
        processor.start(
            task.task_id,
            task.goal,
            privacy_mode=task.privacy_mode,
            constraints=tuple(task.constraints),
            tool_request=normalized_command.tool_request,
        )
        return task


@router.get("", response_model=TaskHistoryRead)
async def list_tasks(
    service: TaskServiceDependency,
    response: Response,
    task_status: TaskStatusFilter = None,
    limit: HistoryLimit = 50,
    offset: HistoryOffset = 0,
) -> TaskHistoryRead:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_tasks(
        status=task_status,
        limit=limit,
        offset=offset,
    )


async def _normalize_task_command(command: TaskCreate) -> TaskCreate:
    request = command.tool_request
    if request is None:
        return command
    if isinstance(request, DiskPressureGuardedFileMoveRequest):
        try:
            normalized = await asyncio.to_thread(
                normalize_file_move_input,
                FileMoveInput(source=request.source, destination=request.destination),
            )
        except (OSError, ToolExecutorError) as error:
            raise ProblemException(
                status_code=422,
                code="DISK_PRESSURE_GUARDED_FILE_MOVE_REQUEST_INVALID",
                title="磁盘压力保护文件移动请求无效",
                detail=("请选择现有普通源文件和同一磁盘上尚不存在的目标路径。"),
            ) from error
        return command.model_copy(
            update={
                "tool_request": DiskPressureGuardedFileMoveRequest(
                    source=normalized.source,
                    destination=normalized.destination,
                    maximum_used_percent=request.maximum_used_percent,
                )
            }
        )
    if isinstance(request, (FileMoveSagaRequest, FileMoveDagRequest)):
        normalized_operations: list[FileMoveSagaOperation | FileMoveDagOperation] = []
        resource_paths: set[str] = set()
        try:
            for operation in request.operations:
                normalized = await asyncio.to_thread(
                    normalize_file_move_input,
                    FileMoveInput(
                        source=operation.source,
                        destination=operation.destination,
                    ),
                )
                canonical_paths = {
                    os.path.normcase(normalized.source),
                    os.path.normcase(normalized.destination),
                }
                if resource_paths.intersection(canonical_paths):
                    raise ToolExecutorError(
                        "Saga operations must use disjoint source and destination paths"
                    )
                resource_paths.update(canonical_paths)
                operation_type = (
                    FileMoveDagOperation
                    if isinstance(request, FileMoveDagRequest)
                    else FileMoveSagaOperation
                )
                normalized_operations.append(
                    operation_type(
                        operation_id=operation.operation_id,
                        source=normalized.source,
                        destination=normalized.destination,
                        **(
                            {"depends_on": operation.depends_on}
                            if isinstance(operation, FileMoveDagOperation)
                            else {}
                        ),
                    )
                )
        except (OSError, ToolExecutorError) as error:
            raise ProblemException(
                status_code=422,
                code=(
                    "FILE_MOVE_DAG_REQUEST_INVALID"
                    if isinstance(request, FileMoveDagRequest)
                    else "FILE_MOVE_SAGA_REQUEST_INVALID"
                ),
                title=(
                    "DAG 文件移动请求无效"
                    if isinstance(request, FileMoveDagRequest)
                    else "多步文件移动请求无效"
                ),
                detail=(
                    "每个源必须是现有普通文件，目标必须不存在且位于同一磁盘；"
                    "各步骤的源和目标路径不能重叠。"
                ),
            ) from error
        normalized_request = (
            FileMoveDagRequest(
                operations=tuple(
                    operation
                    for operation in normalized_operations
                    if isinstance(operation, FileMoveDagOperation)
                )
            )
            if isinstance(request, FileMoveDagRequest)
            else FileMoveSagaRequest(
                operations=tuple(
                    operation
                    for operation in normalized_operations
                    if isinstance(operation, FileMoveSagaOperation)
                    and not isinstance(operation, FileMoveDagOperation)
                )
            )
        )
        return command.model_copy(update={"tool_request": normalized_request})
    try:
        normalized = await asyncio.to_thread(
            normalize_file_move_input,
            FileMoveInput(source=request.source, destination=request.destination),
        )
    except (OSError, ToolExecutorError) as error:
        raise ProblemException(
            status_code=422,
            code="FILE_MOVE_REQUEST_INVALID",
            title="文件移动请求无效",
            detail=("请选择一个存在的普通源文件和同一磁盘上尚不存在的目标路径。"),
        ) from error
    return command.model_copy(
        update={
            "tool_request": FileMoveTaskRequest(
                source=normalized.source,
                destination=normalized.destination,
            )
        }
    )


@router.get("/{task_id}/effect-graph", response_model=EffectGraphRead)
async def get_task_effect_graph(
    task_id: str,
    service: TaskServiceDependency,
) -> EffectGraphRead:
    try:
        return await service.get_effect_graph(task_id)
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error
    except EffectGraphNotFoundError as error:
        raise ProblemException(
            status_code=404,
            code="EFFECT_GRAPH_NOT_FOUND",
            title="Tool effect graph 不存在",
            detail="此任务尚未建立可查询的 Tool effect graph。",
        ) from error


@router.get("/{task_id}", response_model=TaskRead)
async def get_task(
    task_id: str,
    service: TaskServiceDependency,
) -> TaskRead:
    try:
        return await service.get_task(task_id)
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error


@router.get("/{task_id}/events", response_model=list[TaskEventRead])
async def list_task_events(
    task_id: str,
    service: TaskServiceDependency,
    after_seq: AfterSequence = 0,
) -> list[TaskEventRead]:
    try:
        return await service.list_events(task_id, after_seq=after_seq)
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error


@router.post("/{task_id}:pause", response_model=TaskRead)
async def pause_task(
    task_id: str,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    command: TaskControlCommand | None = None,
) -> TaskRead:
    try:
        task = await service.get_task(task_id)
        if task.status is not TaskStatus.PAUSED:
            if task.status is not TaskStatus.RUNNING:
                raise InvalidTaskTransitionError(task_id, task.status, TaskStatus.PAUSED)
            await processor.pause(task_id)
        return await service.transition_task(
            task_id,
            TaskStatus.PAUSED,
            command="pause",
            reason=command.reason if command else None,
        )
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error
    except InvalidTaskTransitionError as error:
        raise _transition_problem(error) from error


@router.post("/{task_id}:resume", response_model=TaskRead)
async def resume_task(
    task_id: str,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    command: TaskControlCommand | None = None,
) -> TaskRead:
    try:
        task = await service.get_task(task_id)
        if task.status is not TaskStatus.PAUSED:
            raise InvalidTaskTransitionError(task_id, task.status, TaskStatus.RUNNING)
        if not processor.can_resume(task_id):
            raise TaskRuntimeUnavailableError(task_id)
        resumed = await service.transition_task(
            task_id,
            TaskStatus.RUNNING,
            command="resume",
            reason=command.reason if command else None,
        )
        processor.resume(task_id)
        return resumed
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error
    except InvalidTaskTransitionError as error:
        raise _transition_problem(error) from error
    except TaskRuntimeUnavailableError as error:
        raise ProblemException(
            status_code=409,
            code="TASK_RUNTIME_UNAVAILABLE",
            title="任务运行时不可恢复",
            detail="此任务没有可验证的可恢复检查点，不能安全恢复。",
        ) from error


@router.post("/{task_id}:cancel", response_model=TaskRead)
async def cancel_task(
    task_id: str,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    command: TaskControlCommand | None = None,
) -> TaskRead:
    try:
        task = await service.get_task(task_id)
        if task.status is not TaskStatus.CANCELLED:
            if task.status.is_terminal:
                raise InvalidTaskTransitionError(task_id, task.status, TaskStatus.CANCELLED)
            await processor.cancel(
                task_id,
                reason=command.reason if command else None,
            )
        cancelled = await service.cancel_task(
            task_id,
            reason=command.reason if command else None,
        )
        processor.forget(task_id)
        return cancelled
    except TaskNotFoundError as error:
        raise _not_found_problem(task_id) from error
    except EffectGraphControlDeliveryTimeoutError as error:
        raise ProblemException(
            status_code=503,
            code=error.code,
            title="Graph cancellation routing is still pending",
            detail=(
                "The cancellation command is durable, but its live graph owner "
                "has not acknowledged it yet. Read the task before retrying."
            ),
            extensions={"control_id": error.control_id},
        ) from error
    except InvalidTaskTransitionError as error:
        raise _transition_problem(error) from error
