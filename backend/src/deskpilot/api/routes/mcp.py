"""Authenticated control plane for trusted MCP stdio servers."""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response

from deskpilot.api.dependencies import get_mcp_control_plane
from deskpilot.api.problem_details import ProblemException
from deskpilot.application.mcp_control_plane import (
    SERVER_ID,
    McpAuditRejectedError,
    McpControlError,
    McpControlPlane,
    McpServerDisabledError,
    McpToolRejectedError,
)
from deskpilot.application.mcp_stdio import McpStdioError, McpStdioTimeoutError
from deskpilot.domain.mcp import (
    McpAuditPage,
    McpServerMutationRead,
    McpServerRead,
    McpToolCallRead,
    McpToolCallRequest,
)

router = APIRouter(prefix="/mcp", tags=["mcp"])
McpDependency = Annotated[McpControlPlane, Depends(get_mcp_control_plane)]


@router.get("/servers", response_model=tuple[McpServerRead, ...])
async def list_servers(service: McpDependency, response: Response) -> tuple[McpServerRead, ...]:
    response.headers["Cache-Control"] = "no-store"
    return await service.list_servers()


@router.post("/servers/{server_id}:enable", response_model=McpServerMutationRead)
async def enable_server(
    server_id: str,
    service: McpDependency,
    response: Response,
) -> McpServerMutationRead:
    _require_server(server_id)
    response.headers["Cache-Control"] = "no-store"
    return await service.set_enabled(True)


@router.post("/servers/{server_id}:disable", response_model=McpServerMutationRead)
async def disable_server(
    server_id: str,
    service: McpDependency,
    response: Response,
) -> McpServerMutationRead:
    _require_server(server_id)
    response.headers["Cache-Control"] = "no-store"
    return await service.set_enabled(False)


@router.post("/servers/{server_id}/tools:call", response_model=McpToolCallRead)
async def call_tool(
    server_id: str,
    command: McpToolCallRequest,
    service: McpDependency,
    response: Response,
) -> McpToolCallRead:
    _require_server(server_id)
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.invoke(command.tool_name, command.arguments)
    except McpServerDisabledError as error:
        raise _problem(409, error, "MCP Server 未启用") from error
    except McpToolRejectedError as error:
        raise _problem(422, error, "MCP Tool 被本地策略拒绝") from error
    except McpStdioTimeoutError as error:
        raise _problem(504, error, "MCP 请求超时") from error
    except McpStdioError as error:
        raise _problem(502, error, "MCP 协议校验失败") from error
    except McpControlError as error:
        raise _problem(409, error, "MCP 控制面拒绝请求") from error


@router.get("/audit", response_model=McpAuditPage)
async def list_audit(
    service: McpDependency,
    response: Response,
    after_sequence: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> McpAuditPage:
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.list_audit(after_sequence, limit)
    except McpAuditRejectedError as error:
        raise _problem(409, error, "MCP 审计链校验失败") from error


def _require_server(server_id: str) -> None:
    if server_id != SERVER_ID:
        raise ProblemException(
            status_code=404,
            code="MCP_SERVER_NOT_FOUND",
            title="MCP Server 不存在",
            detail="请求的 MCP Server 未在受信组合根中注册。",
        )


def _problem(status: int, error: Exception, title: str) -> ProblemException:
    return ProblemException(
        status_code=status,
        code=str(getattr(error, "code", "MCP_CONTROL_REJECTED")),
        title=title,
        detail=str(error),
    )
