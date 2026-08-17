"""Read-only public projections of the trusted frozen Agent Registry."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from deskpilot.api.dependencies import get_agent_registry
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.agent_registry import AgentNotRegisteredError, AgentRegistry
from deskpilot.domain.agent_contracts import (
    AgentDescriptor,
    AgentDescriptorPage,
    AgentRegistrySnapshot,
    AgentRegistryStatus,
)

router = APIRouter(prefix="/agents", tags=["agents"])
AgentRegistryDependency = Annotated[AgentRegistry, Depends(get_agent_registry)]


@router.get("", response_model=AgentDescriptorPage)
def list_agents(
    registry: AgentRegistryDependency,
    response: Response,
    status: AgentRegistryStatus | None = None,
    kind: Annotated[str | None, Query(pattern=r"^(?:worker|synthesizer)$")] = None,
    capability: Annotated[
        str | None, Query(pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$")
    ] = None,
) -> AgentDescriptorPage:
    response.headers["Cache-Control"] = "no-store"
    return AgentDescriptorPage(
        agents=registry.list_public(status=status, kind=kind, capability=capability)
    )


@router.get("/registry-snapshot", response_model=AgentRegistrySnapshot)
def registry_snapshot(
    registry: AgentRegistryDependency,
    response: Response,
) -> AgentRegistrySnapshot:
    response.headers["Cache-Control"] = "no-store"
    return registry.snapshot()


@router.get("/{agent_id}/versions/{version}", response_model=AgentDescriptor)
def get_agent(
    agent_id: str,
    version: str,
    registry: AgentRegistryDependency,
    response: Response,
) -> AgentDescriptor:
    response.headers["Cache-Control"] = "no-store"
    try:
        return registry.descriptor_exact(agent_id, version)
    except AgentNotRegisteredError as error:
        raise ProblemException(
            status_code=404,
            code=error.code,
            title="Agent 版本不存在",
            detail="没有找到请求的精确 Agent 版本。",
        ) from error
