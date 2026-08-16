"""Authenticated manual reconciliation for uncertain Tool call outcomes."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from deskpilot.api.dependencies import get_processor, get_task_service
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.processor import (
    ReconciliationCompensationResourceConflictError,
    TaskProcessor,
)
from deskpilot.application.task_service import (
    ReconciliationAlreadyResolvedError,
    ReconciliationAttemptAlreadyCreatedError,
    ReconciliationAttemptNotAllowedError,
    ReconciliationCompensationAlreadyCreatedError,
    ReconciliationCompensationNotAllowedError,
    ReconciliationGraphRecoveryAlreadyAppliedError,
    ReconciliationGraphRecoveryNotAllowedError,
    ReconciliationIdempotencyConflictError,
    ReconciliationNotFoundError,
    TaskService,
)
from deskpilot.domain.reconciliations import (
    ReconciliationAttemptRead,
    ReconciliationCompensationRead,
    ReconciliationEvidenceRefreshRead,
    ReconciliationGraphRecoveryRead,
    ReconciliationRead,
    ReconciliationResolutionRead,
    ReconciliationStatus,
    RecoverGraphCommand,
    ResolveReconciliationCommand,
)
from deskpilot.domain.schemas import TaskStatus

router = APIRouter(prefix="/reconciliations", tags=["reconciliations"])

TaskServiceDependency = Annotated[TaskService, Depends(get_task_service)]
ProcessorDependency = Annotated[TaskProcessor, Depends(get_processor)]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
ReconciliationStatusFilter = Annotated[ReconciliationStatus | None, Query()]
TaskIdFilter = Annotated[str | None, Query(min_length=1, max_length=40)]

_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            title="缺少幂等键",
            detail="Reconciliation 写请求必须携带 Idempotency-Key。",
        )
    if _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="幂等键格式错误",
            detail="Idempotency-Key 必须是 16 到 128 位安全 ASCII 标识符。",
        )
    return value


def _not_found(reconciliation_id: str) -> ProblemException:
    return ProblemException(
        status_code=404,
        code="RECONCILIATION_NOT_FOUND",
        title="对账记录不存在",
        detail=f"没有找到对账记录 {reconciliation_id}。",
    )


@router.get("", response_model=list[ReconciliationRead])
async def list_reconciliations(
    service: TaskServiceDependency,
    response: Response,
    status: ReconciliationStatusFilter = None,
    task_id: TaskIdFilter = None,
) -> list[ReconciliationRead]:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_reconciliations(status=status, task_id=task_id)


@router.get("/{reconciliation_id}", response_model=ReconciliationRead)
async def get_reconciliation(
    reconciliation_id: str,
    service: TaskServiceDependency,
    response: Response,
) -> ReconciliationRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_reconciliation(reconciliation_id)
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error


@router.post(
    "/{reconciliation_id}:refresh-evidence",
    response_model=ReconciliationEvidenceRefreshRead,
)
async def refresh_reconciliation_evidence(
    reconciliation_id: str,
    processor: ProcessorDependency,
    response: Response,
) -> ReconciliationEvidenceRefreshRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await processor.refresh_reconciliation_evidence(reconciliation_id)
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error


@router.post(
    "/{reconciliation_id}:resolve",
    response_model=ReconciliationResolutionRead,
)
async def resolve_reconciliation(
    reconciliation_id: str,
    command: ResolveReconciliationCommand,
    service: TaskServiceDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> ReconciliationResolutionRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.resolve_reconciliation(
            reconciliation_id,
            outcome=command.outcome,
            evidence_summary=command.evidence_summary,
            idempotency_key=_idempotency_key(idempotency_key),
        )
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error
    except ReconciliationAlreadyResolvedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="对账记录已经裁决",
            detail="人工裁决不可改写；如需执行新调用，请使用显式新 attempt。",
            extensions={"outcome": error.outcome.value},
        ) from error
    except ReconciliationIdempotencyConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="幂等键冲突",
            detail="该 Idempotency-Key 已用于另一项 reconciliation 写请求。",
        ) from error


@router.post(
    "/{reconciliation_id}:recover-graph",
    response_model=ReconciliationGraphRecoveryRead,
)
async def recover_reconciliation_graph(
    reconciliation_id: str,
    command: RecoverGraphCommand,
    processor: ProcessorDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> ReconciliationGraphRecoveryRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await processor.recover_reconciliation_graph(
            reconciliation_id,
            action=command.action,
            idempotency_key=_idempotency_key(idempotency_key),
        )
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error
    except ReconciliationGraphRecoveryNotAllowedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="不能恢复 Tool effect graph",
            detail="当前裁决、回执、图状态或受保护检查点不足以安全执行该动作。",
            extensions={"reason_code": error.reason_code},
        ) from error
    except ReconciliationGraphRecoveryAlreadyAppliedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="图恢复动作已经提交",
            detail="同一条 reconciliation 只允许提交一次图恢复动作。",
            extensions={"action": error.action.value},
        ) from error
    except ReconciliationIdempotencyConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="幂等键冲突",
            detail="该 Idempotency-Key 已用于另一项 reconciliation 写请求。",
        ) from error


@router.post(
    "/{reconciliation_id}:create-compensation",
    response_model=ReconciliationCompensationRead,
    status_code=201,
)
async def create_reconciliation_compensation(
    reconciliation_id: str,
    processor: ProcessorDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> ReconciliationCompensationRead:
    """Create a fresh approved reverse task from server-side receipt evidence only."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return await processor.create_reconciliation_compensation(
            reconciliation_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error
    except ReconciliationCompensationNotAllowedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="不能创建反向补偿任务",
            detail="只有经签名 Runner 查询确认已提交的 file.move 才能派生反向任务。",
            extensions={"reason_code": error.reason_code},
        ) from error
    except ReconciliationCompensationAlreadyCreatedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="反向补偿任务已创建",
            detail="一条已提交回执只允许派生一个直接反向任务。",
            extensions={"task_id": error.task_id},
        ) from error
    except ReconciliationCompensationResourceConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="反向资源已变化",
            detail="当前反向源文件与 receipt 版本不一致，或原源路径不再为空。",
        ) from error
    except ReconciliationIdempotencyConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="幂等键冲突",
            detail="该 Idempotency-Key 已用于另一项 reconciliation 写请求。",
        ) from error


@router.post(
    "/{reconciliation_id}:create-attempt",
    response_model=ReconciliationAttemptRead,
    status_code=201,
)
async def create_reconciliation_attempt(
    reconciliation_id: str,
    service: TaskServiceDependency,
    processor: ProcessorDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> ReconciliationAttemptRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        result = await service.create_reconciliation_attempt(
            reconciliation_id,
            idempotency_key=_idempotency_key(idempotency_key),
        )
        if result.task.status is TaskStatus.CREATED and not processor.has_runtime(
            result.task.task_id
        ):
            processor.start(
                result.task.task_id,
                result.task.goal,
                privacy_mode=result.task.privacy_mode,
                constraints=tuple(result.task.constraints),
            )
        return result
    except ReconciliationNotFoundError as error:
        raise _not_found(reconciliation_id) from error
    except ReconciliationAttemptNotAllowedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="不能安全创建新 attempt",
            detail="只有人工确认原调用未产生任何效果后，才能创建全新任务。",
            extensions={
                "reconciliation_status": error.status.value,
                "outcome": error.outcome.value if error.outcome is not None else None,
            },
        ) from error
    except ReconciliationAttemptAlreadyCreatedError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="新 attempt 已经创建",
            detail="该对账记录只允许创建一个直接后继任务。",
            extensions={"task_id": error.task_id},
        ) from error
    except ReconciliationIdempotencyConflictError as error:
        raise ProblemException(
            status_code=409,
            code=error.code,
            title="幂等键冲突",
            detail="该 Idempotency-Key 已用于另一项 reconciliation 写请求。",
        ) from error
