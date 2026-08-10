"""RFC 9457-style Problem Details responses and exception handlers."""

import logging
from http import HTTPStatus
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

logger = logging.getLogger(__name__)


class ProblemException(Exception):
    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        title: str,
        detail: str,
        extensions: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.code = code
        self.title = title
        self.detail = detail
        self.extensions = extensions or {}


def problem_response(
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    instance: str,
    extensions: dict[str, Any] | None = None,
) -> JSONResponse:
    content: dict[str, Any] = {
        "type": f"urn:deskpilot:problem:{code.casefold().replace('_', '-')}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": instance,
        "code": code,
    }
    if extensions:
        content.update(extensions)
    return JSONResponse(
        status_code=status_code,
        content=content,
        media_type="application/problem+json",
    )


async def problem_exception_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, ProblemException):
        raise error
    return problem_response(
        status_code=error.status_code,
        code=error.code,
        title=error.title,
        detail=error.detail,
        instance=request.url.path,
        extensions=error.extensions,
    )


async def http_exception_handler(request: Request, error: Exception) -> JSONResponse:
    if not isinstance(error, HTTPException):
        raise error
    code = "HTTP_ERROR"
    detail = HTTPStatus(error.status_code).phrase
    if isinstance(error.detail, dict):
        code = str(error.detail.get("code", code))
        detail = str(error.detail.get("message", detail))
    elif error.detail:
        detail = str(error.detail)
    return problem_response(
        status_code=error.status_code,
        code=code,
        title=HTTPStatus(error.status_code).phrase,
        detail=detail,
        instance=request.url.path,
    )


async def validation_exception_handler(
    request: Request,
    error: Exception,
) -> JSONResponse:
    if not isinstance(error, RequestValidationError):
        raise error
    violations = [
        {
            "pointer": "/" + "/".join(str(part) for part in violation["loc"]),
            "detail": violation["msg"],
            "code": violation["type"],
        }
        for violation in error.errors()
    ]
    return problem_response(
        status_code=422,
        code="REQUEST_VALIDATION_FAILED",
        title="请求参数校验失败",
        detail="请求中有一个或多个字段不符合 API 契约。",
        instance=request.url.path,
        extensions={"errors": violations},
    )


async def internal_exception_handler(request: Request, error: Exception) -> JSONResponse:
    logger.error(
        "Unhandled API exception",
        exc_info=(type(error), error, error.__traceback__),
    )
    return problem_response(
        status_code=500,
        code="INTERNAL_ERROR",
        title="服务内部错误",
        detail="DeskPilot 未能完成请求，且没有执行额外操作。",
        instance=request.url.path,
    )
