"""Authenticated local knowledge-base endpoints."""

from typing import Annotated

from fastapi import APIRouter, Depends, Response

from deskpilot.api.dependencies import get_knowledge_base
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.knowledge_base import (
    KnowledgeProofRejectedError,
    KnowledgeSourceError,
    LocalKnowledgeBase,
)
from deskpilot.domain.knowledge import (
    KnowledgeImportRequest,
    KnowledgeSearchRead,
    KnowledgeSearchRequest,
    KnowledgeSourceRead,
)

router = APIRouter(prefix="/knowledge", tags=["knowledge"])
KnowledgeDependency = Annotated[LocalKnowledgeBase, Depends(get_knowledge_base)]


@router.get("/sources", response_model=tuple[KnowledgeSourceRead, ...])
async def list_sources(
    service: KnowledgeDependency,
    response: Response,
) -> tuple[KnowledgeSourceRead, ...]:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_sources()


@router.post("/sources:import", response_model=KnowledgeSourceRead)
async def import_source(
    command: KnowledgeImportRequest,
    service: KnowledgeDependency,
    response: Response,
) -> KnowledgeSourceRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.import_file(command.path)
    except KnowledgeSourceError as error:
        raise ProblemException(
            status_code=422,
            code=error.code,
            title="本地知识源无效",
            detail=str(error),
        ) from error
    except KnowledgeProofRejectedError as error:
        raise _proof_problem(error) from error


@router.post("/search", response_model=KnowledgeSearchRead)
async def search_knowledge(
    command: KnowledgeSearchRequest,
    service: KnowledgeDependency,
    response: Response,
) -> KnowledgeSearchRead:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.search(command.query, command.limit)
    except KnowledgeSourceError as error:
        raise ProblemException(
            status_code=422,
            code=error.code,
            title="知识检索请求无效",
            detail=str(error),
        ) from error
    except KnowledgeProofRejectedError as error:
        raise _proof_problem(error) from error


def _proof_problem(error: KnowledgeProofRejectedError) -> ProblemException:
    return ProblemException(
        status_code=409,
        code=error.code,
        title="知识证据校验失败",
        detail="本地知识证据与其内容寻址证明不一致，已拒绝返回检索结果。",
    )
