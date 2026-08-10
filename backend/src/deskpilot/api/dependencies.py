"""FastAPI dependency adapters."""

from typing import cast

from fastapi import Request

from deskpilot.api.problem_details import ProblemException
from deskpilot.application.event_broker import EventBroker
from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.processor import TaskProcessor
from deskpilot.application.provider_catalog import ProviderCatalogService
from deskpilot.application.provider_management_service import (
    ProviderManagementService,
)
from deskpilot.application.task_service import TaskService


def get_task_service(request: Request) -> TaskService:
    return cast(TaskService, request.app.state.task_service)


def get_processor(request: Request) -> TaskProcessor:
    return cast(TaskProcessor, request.app.state.processor)


def get_event_broker(request: Request) -> EventBroker:
    return cast(EventBroker, request.app.state.event_broker)


def get_provider_catalog(request: Request) -> ProviderCatalogService:
    return cast(ProviderCatalogService, request.app.state.provider_catalog)


def get_model_gateway(request: Request) -> ModelGateway:
    return cast(ModelGateway, request.app.state.model_gateway)


def get_provider_management(request: Request) -> ProviderManagementService:
    service = getattr(request.app.state, "provider_management", None)
    if service is None:
        raise ProblemException(
            status_code=503,
            code="MODEL_PROVIDER_MANAGEMENT_UNAVAILABLE",
            title="Provider 管理不可用",
            detail="当前应用使用注入式测试 Provider，未启用持久化管理。",
        )
    return cast(ProviderManagementService, service)
