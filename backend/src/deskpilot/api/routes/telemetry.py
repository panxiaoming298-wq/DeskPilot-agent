"""Authenticated, no-store queries for the bounded local telemetry projection."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from deskpilot.api.dependencies import get_telemetry
from deskpilot.api.problem_details import ProblemException
from deskpilot.observability import TelemetryFacade
from deskpilot.observability.schema import TelemetryMetricsRead, TelemetryTracePage

router = APIRouter(prefix="/telemetry", tags=["telemetry"])
TelemetryDependency = Annotated[TelemetryFacade, Depends(get_telemetry)]


@router.get("/traces", response_model=TelemetryTracePage)
def query_traces(
    telemetry: TelemetryDependency,
    response: Response,
    trace_id: Annotated[str | None, Query(pattern=r"^[0-9a-f]{32}$")] = None,
    task_correlation_id: Annotated[str | None, Query(pattern=r"^trc_[0-9a-f]{32}$")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> TelemetryTracePage:
    response.headers["Cache-Control"] = "no-store"
    if (trace_id is None) == (task_correlation_id is None):
        raise ProblemException(
            status_code=422,
            code="TELEMETRY_QUERY_INVALID",
            title="遥测查询条件无效",
            detail="trace_id 与 task_correlation_id 必须且只能提供一个。",
        )
    return telemetry.query(
        trace_id=trace_id,
        task_correlation_id=task_correlation_id,
        limit=limit,
    )


@router.get("/metrics", response_model=TelemetryMetricsRead)
def query_metrics(
    telemetry: TelemetryDependency,
    response: Response,
) -> TelemetryMetricsRead:
    response.headers["Cache-Control"] = "no-store"
    return telemetry.metrics()
