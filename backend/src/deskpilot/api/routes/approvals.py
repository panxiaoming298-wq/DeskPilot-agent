"""Exact, one-shot approval query and decision endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from deskpilot.api.dependencies import get_processor, get_task_service
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.processor import (
    ApprovalContinuationState,
    TaskProcessor,
    TaskRuntimeUnavailableError,
)
from deskpilot.application.task_service import (
    ApprovalAlreadyResolvedError,
    ApprovalExpiredError,
    ApprovalNotFoundError,
    ApprovalStaleError,
    ApprovalTaskStateConflictError,
    TaskNotFoundError,
    TaskService,
)
from deskpilot.domain.approvals import (
    ApprovalRead,
    ApprovalResolutionRead,
    ApprovalStatus,
    ResolveCommand,
)

router = APIRouter(prefix="/approvals", tags=["approvals"])

TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]
ProcessorDependency = Annotated[TaskProcessor, Depends(get_processor)]
ApprovalStatusFilter = Annotated[ApprovalStatus | None, Query()]
TaskIdFilter = Annotated[str | None, Query(min_length=1, max_length=40)]


def _not_found(approval_id: str) -> ProblemException:
    return ProblemException(
        status_code=404,
        code="APPROVAL_NOT_FOUND",
        title="审批不存在",
        detail=f"没有找到审批 {approval_id}。",
    )


def _resolved_conflict(error: ApprovalAlreadyResolvedError) -> ProblemException:
    return ProblemException(
        status_code=409,
        code="APPROVAL_ALREADY_RESOLVED",
        title="审批已经处理",
        detail=(f"审批当前为 {error.current.value}，不能再改为 {error.requested.value}。"),
        extensions={
            "approval_status": error.current.value,
            "requested_decision": error.requested.value,
        },
    )


def _runtime_unavailable() -> ProblemException:
    return ProblemException(
        status_code=409,
        code="APPROVAL_RUNTIME_UNAVAILABLE",
        title="审批运行时不可恢复",
        detail=("此审批没有匹配且可验证的执行检查点，不会在授权后猜测或重放工具调用。"),
    )


@router.get("", response_model=list[ApprovalRead])
async def list_approvals(
    service: TaskServiceDependency,
    response: Response,
    status: ApprovalStatusFilter = None,
    task_id: TaskIdFilter = None,
) -> list[ApprovalRead]:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_approvals(status=status, task_id=task_id)


@router.get("/{approval_id}", response_model=ApprovalRead)
async def get_approval(
    approval_id: str,
    service: TaskServiceDependency,
    response: Response,
) -> ApprovalRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_approval(approval_id)
    except ApprovalNotFoundError as error:
        raise _not_found(approval_id) from error


async def _resolve(
    *,
    approval_id: str,
    command: ResolveCommand,
    decision: ApprovalStatus,
    service: TaskService,
    processor: TaskProcessor,
) -> ApprovalResolutionRead:
    try:
        if decision is ApprovalStatus.APPROVED:
            current = await service.get_approval(approval_id)
            if (
                current.status is ApprovalStatus.PENDING
                and processor.approval_continuation_state(current.task_id, approval_id)
                is ApprovalContinuationState.UNAVAILABLE
            ):
                raise _runtime_unavailable()

        result = await service.resolve_approval(
            approval_id,
            decision=decision,
            preview_hash=command.preview_hash,
            scope=command.scope,
            reason=command.reason,
        )
        if decision is ApprovalStatus.APPROVED:
            if result.approval.consumed_at is not None or result.task.status.is_terminal:
                return result
            continuation = processor.approval_continuation_state(
                result.task.task_id,
                result.approval.approval_id,
            )
            if continuation is ApprovalContinuationState.IN_PROGRESS:
                return result
            if continuation is ApprovalContinuationState.UNAVAILABLE:
                raise _runtime_unavailable()
            try:
                processor.continue_after_approval(
                    result.task.task_id,
                    result.approval.approval_id,
                )
            except TaskRuntimeUnavailableError as error:
                if (
                    processor.approval_continuation_state(
                        result.task.task_id,
                        result.approval.approval_id,
                    )
                    is ApprovalContinuationState.IN_PROGRESS
                ):
                    return result
                raise _runtime_unavailable() from error
        elif not result.replayed:
            processor.forget(result.task.task_id)
        return result
    except ApprovalNotFoundError as error:
        raise _not_found(approval_id) from error
    except ApprovalStaleError as error:
        raise ProblemException(
            status_code=409,
            code="APPROVAL_STALE",
            title="审批预览已失效",
            detail="审批绑定的预览摘要不匹配；请刷新并重新核对最终参数。",
        ) from error
    except ApprovalExpiredError as error:
        processor.forget((await service.get_approval(approval_id)).task_id)
        raise ProblemException(
            status_code=409,
            code="APPROVAL_EXPIRED",
            title="审批已过期",
            detail="该次授权窗口已经结束，工具没有执行。",
        ) from error
    except ApprovalAlreadyResolvedError as error:
        raise _resolved_conflict(error) from error
    except ApprovalTaskStateConflictError as error:
        raise ProblemException(
            status_code=409,
            code="APPROVAL_TASK_STATE_CONFLICT",
            title="任务状态不允许审批",
            detail=f"关联任务当前为 {error.task_status.value}，不能处理该审批。",
            extensions={"task_status": error.task_status.value},
        ) from error
    except TaskRuntimeUnavailableError as error:
        raise _runtime_unavailable() from error
    except TaskNotFoundError as error:
        raise ProblemException(
            status_code=404,
            code="TASK_NOT_FOUND",
            title="任务不存在",
            detail=f"没有找到审批关联的任务 {error.task_id}。",
        ) from error


@router.post("/{approval_id}:approve", response_model=ApprovalResolutionRead)
async def approve(
    approval_id: str,
    command: ResolveCommand,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    response: Response,
) -> ApprovalResolutionRead:
    response.headers["Cache-Control"] = "no-store"
    return await _resolve(
        approval_id=approval_id,
        command=command,
        decision=ApprovalStatus.APPROVED,
        service=service,
        processor=processor,
    )


@router.post("/{approval_id}:reject", response_model=ApprovalResolutionRead)
async def reject(
    approval_id: str,
    command: ResolveCommand,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    response: Response,
) -> ApprovalResolutionRead:
    response.headers["Cache-Control"] = "no-store"
    return await _resolve(
        approval_id=approval_id,
        command=command,
        decision=ApprovalStatus.REJECTED,
        service=service,
        processor=processor,
    )
