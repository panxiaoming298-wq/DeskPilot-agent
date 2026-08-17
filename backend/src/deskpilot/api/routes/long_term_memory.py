"""User-owned long-term memory control-plane routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, Path, Response, status

from deskpilot.api.dependencies import get_long_term_memory_runtime
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.long_term_memory_runtime import (
    LongTermMemoryConflictError,
    LongTermMemoryError,
    LongTermMemoryNotFoundError,
    LongTermMemoryRuntime,
)
from deskpilot.domain.long_term_memory import (
    MEMORY_CONFLICT_ID_PATTERN,
    MEMORY_ID_PATTERN,
    MEMORY_PROPOSAL_ID_PATTERN,
    CreateLongTermMemoryRequest,
    EditLongTermMemoryRequest,
    LongTermMemoryExport,
    LongTermMemoryPage,
    ResolveMemoryConflictRequest,
)

router = APIRouter(tags=["long-term-memory"])
RuntimeDependency = Annotated[LongTermMemoryRuntime, Depends(get_long_term_memory_runtime)]
MemoryId = Annotated[str, Path(pattern=MEMORY_ID_PATTERN)]
ProposalId = Annotated[str, Path(pattern=MEMORY_PROPOSAL_ID_PATTERN)]
ConflictId = Annotated[str, Path(pattern=MEMORY_CONFLICT_ID_PATTERN)]


@router.get("/memory", response_model=LongTermMemoryPage)
async def list_memory(runtime: RuntimeDependency, response: Response) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.list_all()
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.post("/memory", response_model=LongTermMemoryPage, status_code=status.HTTP_201_CREATED)
async def create_memory(
    request: CreateLongTermMemoryRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.create_user_memory(request)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.post("/memory/proposals/{proposal_id}:confirm", response_model=LongTermMemoryPage)
async def confirm_memory(
    proposal_id: ProposalId, runtime: RuntimeDependency, response: Response
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.confirm(proposal_id)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.post("/memory/proposals/{proposal_id}:reject", response_model=LongTermMemoryPage)
async def reject_memory(
    proposal_id: ProposalId, runtime: RuntimeDependency, response: Response
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.reject(proposal_id)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.patch("/memory/{memory_id}", response_model=LongTermMemoryPage)
async def edit_memory(
    memory_id: MemoryId,
    request: EditLongTermMemoryRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.edit(memory_id, request)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.delete("/memory/{memory_id}", response_model=LongTermMemoryPage)
async def delete_memory(
    memory_id: MemoryId, runtime: RuntimeDependency, response: Response
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.delete(memory_id)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.post("/memory-conflicts/{conflict_id}:resolve", response_model=LongTermMemoryPage)
async def resolve_memory_conflict(
    conflict_id: ConflictId,
    request: ResolveMemoryConflictRequest,
    runtime: RuntimeDependency,
    response: Response,
) -> LongTermMemoryPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.resolve_conflict(conflict_id, request.selected_memory_id)
    except LongTermMemoryError as error:
        raise _problem(error) from error


@router.get("/memory/export", response_model=LongTermMemoryExport)
async def export_memory(runtime: RuntimeDependency, response: Response) -> LongTermMemoryExport:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await runtime.export()
    except LongTermMemoryError as error:
        raise _problem(error) from error


def _problem(error: LongTermMemoryError) -> ProblemException:
    return ProblemException(
        status_code=(
            404
            if isinstance(error, LongTermMemoryNotFoundError)
            else 409
            if isinstance(error, LongTermMemoryConflictError)
            else 400
        ),
        code=error.code,
        title="长期记忆命令被拒绝",
        detail=str(error),
    )
