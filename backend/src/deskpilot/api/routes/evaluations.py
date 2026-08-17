"""Authenticated built-in golden evaluation and trace replay endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from deskpilot.api.dependencies import get_evaluation_service
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.evaluation_service import (
    EvaluationError,
    EvaluationProofRejectedError,
    EvaluationRunNotFoundError,
    EvaluationService,
)
from deskpilot.domain.evaluations import EvaluationReportRead, EvaluationRunPage, EvaluationRunRead

router = APIRouter(prefix="/evaluations", tags=["evaluations"])
EvaluationDependency = Annotated[EvaluationService, Depends(get_evaluation_service)]


@router.get("/runs", response_model=EvaluationRunPage)
async def list_runs(
    service: EvaluationDependency,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> EvaluationRunPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.list_runs(limit)
    except EvaluationProofRejectedError as error:
        raise _problem(409, error, "评测证据校验失败") from error


@router.get("/runs/{run_id}", response_model=EvaluationRunRead)
async def get_run(
    run_id: str,
    service: EvaluationDependency,
    response: Response,
) -> EvaluationRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_run(run_id)
    except EvaluationRunNotFoundError as error:
        raise _problem(404, error, "评测运行不存在") from error
    except EvaluationProofRejectedError as error:
        raise _problem(409, error, "评测证据校验失败") from error


@router.get("/reports/latest", response_model=EvaluationReportRead)
async def latest_report(
    service: EvaluationDependency,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EvaluationReportRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.report(limit)
    except EvaluationProofRejectedError as error:
        raise _problem(409, error, "评测报告证据校验失败") from error


@router.get("/reports/latest:export", response_model=EvaluationReportRead)
async def export_latest_report(
    service: EvaluationDependency,
    response: Response,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> EvaluationReportRead:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Disposition"] = (
        'attachment; filename="deskpilot-evaluation-report-v1.json"'
    )
    try:
        return await service.report(limit)
    except EvaluationProofRejectedError as error:
        raise _problem(409, error, "评测报告证据校验失败") from error


@router.post("/golden:run", response_model=EvaluationRunRead)
async def run_golden(
    service: EvaluationDependency,
    response: Response,
) -> EvaluationRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.run_builtin()
    except EvaluationError as error:
        raise _problem(409, error, "黄金任务运行被拒绝") from error


@router.post("/runs/{run_id}:replay", response_model=EvaluationRunRead)
async def replay_run(
    run_id: str,
    service: EvaluationDependency,
    response: Response,
) -> EvaluationRunRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.replay(run_id)
    except EvaluationRunNotFoundError as error:
        raise _problem(404, error, "评测运行不存在") from error
    except EvaluationError as error:
        raise _problem(409, error, "评测重放被拒绝") from error


def _problem(status: int, error: EvaluationError, title: str) -> ProblemException:
    return ProblemException(
        status_code=status,
        code=error.code,
        title=title,
        detail=str(error),
    )
