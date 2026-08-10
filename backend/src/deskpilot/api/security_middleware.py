"""HTTP security boundary for the local API."""

from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from deskpilot.api.problem_details import problem_response
from deskpilot.core.security import LocalSessionSecurity

RequestHandler = Callable[[Request], Awaitable[Response]]


class LocalApiSecurityMiddleware(BaseHTTPMiddleware):
    def __init__(
        self,
        app: Callable[..., Awaitable[None]],
        *,
        security: LocalSessionSecurity,
        api_prefix: str,
    ) -> None:
        super().__init__(app)
        self._security = security
        self._api_prefix = api_prefix.rstrip("/")
        self._public_paths = {
            f"{self._api_prefix}/health",
            f"{self._api_prefix}/session",
        }

    async def dispatch(self, request: Request, call_next: RequestHandler) -> Response:
        path = request.url.path
        if not path.startswith(f"{self._api_prefix}/") or path in self._public_paths:
            return await call_next(request)

        origin = request.headers.get("origin")
        if origin is not None and not self._security.is_allowed_origin(origin):
            return problem_response(
                status_code=403,
                code="ORIGIN_NOT_ALLOWED",
                title="请求来源不受信任",
                detail="Origin 不在 DeskPilot 本地前端允许列表中。",
                instance=path,
            )

        if request.method not in {"GET", "HEAD", "OPTIONS"} and not (
            self._security.is_trusted_browser_request(
                origin=origin,
                fetch_site=request.headers.get("sec-fetch-site"),
                client_header=request.headers.get("x-deskpilot-client"),
            )
        ):
            return problem_response(
                status_code=403,
                code="ORIGIN_REQUIRED",
                title="缺少可信请求来源",
                detail="修改本地状态的请求必须来自受信任的 DeskPilot 前端。",
                instance=path,
            )

        if not self._security.authenticate_bearer(request.headers.get("authorization")):
            return problem_response(
                status_code=401,
                code="SESSION_TOKEN_INVALID",
                title="本地会话未认证",
                detail="请重新建立 DeskPilot 本地安全会话。",
                instance=path,
            )

        return await call_next(request)
