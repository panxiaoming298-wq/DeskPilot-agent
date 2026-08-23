"""Read-only Task Contract, capability and Executable Plan projections."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response

from deskpilot.api.dependencies import (
    get_capability_catalog,
    get_plan_compilation_service,
)
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.plan_compilation_service import (
    PlanCompilationService,
    PlanningError,
    PlanningNotFoundError,
    PlanningProofRejectedError,
)
from deskpilot.domain.agent_replanning import AgentReplanPage
from deskpilot.domain.task_plans import (
    TASK_ID_PATTERN,
    CapabilityPackPage,
    ExecutablePlanPage,
    ExecutablePlanRead,
    PlanningStateRead,
    TaskContractVersionPage,
    TaskContractVersionRead,
)

router = APIRouter(tags=["planning"])
CapabilityCatalogDependency = Annotated[CapabilityCatalog, Depends(get_capability_catalog)]
PlanningDependency = Annotated[
    PlanCompilationService,
    Depends(get_plan_compilation_service),
]
TaskId = Annotated[str, Path(pattern=TASK_ID_PATTERN)]


@router.get("/capabilities", response_model=CapabilityPackPage)
def list_capabilities(
    catalog: CapabilityCatalogDependency,
    response: Response,
) -> CapabilityPackPage:
    response.headers["Cache-Control"] = "no-store"
    return CapabilityPackPage(capabilities=catalog.list_public())


@router.get("/tasks/{task_id}/planning", response_model=PlanningStateRead)
async def get_planning_state(
    task_id: TaskId,
    service: PlanningDependency,
    response: Response,
) -> PlanningStateRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_state(task_id)
    except PlanningError as error:
        raise _problem(error) from error


@router.get("/tasks/{task_id}/contract", response_model=TaskContractVersionRead)
async def get_current_contract(
    task_id: TaskId,
    service: PlanningDependency,
    response: Response,
) -> TaskContractVersionRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_current_contract(task_id)
    except PlanningError as error:
        raise _problem(error) from error


@router.get("/tasks/{task_id}/contracts", response_model=TaskContractVersionPage)
async def list_contracts(
    task_id: TaskId,
    service: PlanningDependency,
    response: Response,
) -> TaskContractVersionPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.list_contracts(task_id)
    except PlanningError as error:
        raise _problem(error) from error


@router.get("/tasks/{task_id}/plans", response_model=ExecutablePlanPage)
async def list_plans(
    task_id: TaskId,
    service: PlanningDependency,
    response: Response,
) -> ExecutablePlanPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.list_plans(task_id)
    except PlanningError as error:
        raise _problem(error) from error


@router.get(
    "/tasks/{task_id}/plans/{generation}",
    response_model=ExecutablePlanRead,
)
async def get_plan(
    task_id: TaskId,
    generation: Annotated[int, Path(ge=1)],
    service: PlanningDependency,
    response: Response,
) -> ExecutablePlanRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.get_plan(task_id, generation)
    except PlanningError as error:
        raise _problem(error) from error


@router.get("/tasks/{task_id}/replans", response_model=AgentReplanPage)
async def list_replans(
    task_id: TaskId,
    service: PlanningDependency,
    response: Response,
) -> AgentReplanPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.list_replans(task_id)
    except PlanningError as error:
        raise _problem(error) from error


def _problem(error: PlanningError) -> ProblemException:
    status = 404 if isinstance(error, PlanningNotFoundError) else 409
    title = (
        "规划记录不存在"
        if status == 404
        else (
            "规划证据校验失败"
            if isinstance(error, PlanningProofRejectedError)
            else "规划读取被拒绝"
        )
    )
    return ProblemException(
        status_code=status,
        code=error.code,
        title=title,
        detail=str(error),
    )
