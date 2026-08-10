"""Local browser session bootstrap endpoint."""

from typing import cast

from fastapi import APIRouter, Request, Response

from deskpilot.api.problem_details import ProblemException
from deskpilot.core.security import WEBSOCKET_PROTOCOL, LocalSessionSecurity
from deskpilot.domain.schemas import SessionBootstrapRead

router = APIRouter(tags=["session"])


@router.get("/session", response_model=SessionBootstrapRead)
async def bootstrap_session(request: Request, response: Response) -> SessionBootstrapRead:
    security = cast(LocalSessionSecurity, request.app.state.session_security)
    if not security.is_trusted_browser_request(
        origin=request.headers.get("origin"),
        fetch_site=request.headers.get("sec-fetch-site"),
        client_header=request.headers.get("x-deskpilot-client"),
    ):
        raise ProblemException(
            status_code=403,
            code="ORIGIN_NOT_ALLOWED",
            title="请求来源不受信任",
            detail="只有受信任的 DeskPilot 前端可以建立本地会话。",
        )

    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    return SessionBootstrapRead(
        access_token=security.token,
        token_type="Bearer",
        websocket_protocol=WEBSOCKET_PROTOCOL,
    )
