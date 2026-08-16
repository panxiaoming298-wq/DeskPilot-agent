"""Authenticated effect-runtime operations, retention and audit endpoints."""

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Query, Response

from deskpilot.api.dependencies import get_effect_runtime_operations as get_operations_service
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.effect_runtime_operations import (
    EffectRuntimeOperationsAuditRejectedError,
    EffectRuntimeOperationsIdempotencyConflictError,
    EffectRuntimeOperationsService,
    OutboxDeadLetterNotFoundError,
)
from deskpilot.domain.effect_runtime_operations import (
    EffectRuntimeAuditExportPage,
    EffectRuntimeAuditPage,
    EffectRuntimeOperationsSnapshot,
    MetricsAuditResult,
    OperationsAlertNotificationPage,
    OutboxRequeueResult,
    RetentionRunRequest,
    RetentionRunResult,
)

router = APIRouter(prefix="/operations", tags=["operations"])
OperationsDependency = Annotated[
    EffectRuntimeOperationsService,
    Depends(get_operations_service),
]
IdempotencyHeader = Annotated[str | None, Header(alias="Idempotency-Key")]
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{15,127}$")
_ACTOR_ID = "local-session"


@router.get("/effect-runtime", response_model=EffectRuntimeOperationsSnapshot)
async def get_effect_runtime_operations(
    service: OperationsDependency,
    response: Response,
    sample_limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> EffectRuntimeOperationsSnapshot:
    response.headers["Cache-Control"] = "no-store"
    return await service.snapshot(sample_limit=sample_limit)


@router.get("/effect-runtime/audit", response_model=EffectRuntimeAuditPage)
async def list_effect_runtime_audit(
    service: OperationsDependency,
    response: Response,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> EffectRuntimeAuditPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.audit_page(after_sequence=after_sequence, limit=limit)
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


@router.get(
    "/effect-runtime/alerts",
    response_model=OperationsAlertNotificationPage,
)
async def list_effect_runtime_alert_notifications(
    service: OperationsDependency,
    response: Response,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> OperationsAlertNotificationPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.alert_notification_page(
            after_sequence=after_sequence,
            limit=limit,
        )
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


@router.get(
    "/effect-runtime/audit/export",
    response_model=EffectRuntimeAuditExportPage,
)
async def export_effect_runtime_audit(
    service: OperationsDependency,
    response: Response,
    cursor: Annotated[str | None, Query(min_length=1, max_length=2_048)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 500,
) -> EffectRuntimeAuditExportPage:
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    try:
        return await service.audit_export_page(cursor=cursor, limit=limit)
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


@router.post("/effect-runtime:sample", response_model=MetricsAuditResult)
async def sample_effect_runtime_metrics(
    service: OperationsDependency,
    response: Response,
    sample_limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> MetricsAuditResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.sample_metrics(actor_id=_ACTOR_ID, sample_limit=sample_limit)
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


@router.post("/effect-runtime:run-retention", response_model=RetentionRunResult)
async def run_effect_runtime_retention(
    command: RetentionRunRequest,
    service: OperationsDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> RetentionRunResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.run_retention(
            actor_id=_ACTOR_ID,
            idempotency_key=_idempotency_key(idempotency_key),
            retention_days=command.retention_days,
        )
    except EffectRuntimeOperationsIdempotencyConflictError as error:
        raise _idempotency_conflict() from error
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


@router.post("/outbox/{message_id}:requeue", response_model=OutboxRequeueResult)
async def requeue_outbox_dead_letter(
    message_id: str,
    service: OperationsDependency,
    response: Response,
    idempotency_key: IdempotencyHeader = None,
) -> OutboxRequeueResult:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.requeue_dead_letter(
            message_id,
            actor_id=_ACTOR_ID,
            idempotency_key=_idempotency_key(idempotency_key),
        )
    except OutboxDeadLetterNotFoundError as error:
        raise ProblemException(
            status_code=404,
            code=error.code,
            title="Outbox DLQ 消息不存在",
            detail="该消息不存在、已经发布，或当前不在 dead-letter 状态。",
        ) from error
    except EffectRuntimeOperationsIdempotencyConflictError as error:
        raise _idempotency_conflict() from error
    except EffectRuntimeOperationsAuditRejectedError as error:
        raise _audit_rejected() from error


def _idempotency_key(value: str | None) -> str:
    if value is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_REQUIRED",
            title="缺少幂等键",
            detail="运维写请求必须携带 Idempotency-Key。",
        )
    if _IDEMPOTENCY_PATTERN.fullmatch(value) is None:
        raise ProblemException(
            status_code=400,
            code="IDEMPOTENCY_KEY_INVALID",
            title="幂等键格式错误",
            detail="Idempotency-Key 必须是 16 到 128 位安全 ASCII 标识符。",
        )
    return value


def _idempotency_conflict() -> ProblemException:
    return ProblemException(
        status_code=409,
        code="EFFECT_RUNTIME_OPERATIONS_IDEMPOTENCY_CONFLICT",
        title="运维幂等键冲突",
        detail="该 Idempotency-Key 已用于不同的运维请求。",
    )


def _audit_rejected() -> ProblemException:
    return ProblemException(
        status_code=409,
        code="EFFECT_RUNTIME_OPERATIONS_AUDIT_REJECTED",
        title="运维审计证明无效",
        detail="审计序号、内容摘要或 hash-chain 连续性校验失败。",
    )
