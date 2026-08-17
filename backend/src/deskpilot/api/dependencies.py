"""FastAPI dependency adapters."""

from typing import cast

from fastapi import Request

from deskpilot.api.problem_details import ProblemException
from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.agent_registry import AgentRegistry
from deskpilot.application.artifact_delivery_runtime import ArtifactDeliveryRuntime
from deskpilot.application.artifact_export_runtime import ArtifactExportRuntime
from deskpilot.application.capability_catalog import CapabilityCatalog
from deskpilot.application.context_memory_runtime import ContextMemoryRuntime
from deskpilot.application.effect_runtime_operations import EffectRuntimeOperationsService
from deskpilot.application.evaluation_service import EvaluationService
from deskpilot.application.event_broker import EventBroker
from deskpilot.application.knowledge_base import LocalKnowledgeBase
from deskpilot.application.long_term_memory_runtime import LongTermMemoryRuntime
from deskpilot.application.mcp_control_plane import McpControlPlane
from deskpilot.application.model_gateway import ModelGateway
from deskpilot.application.plan_compilation_service import PlanCompilationService
from deskpilot.application.processor import TaskProcessor
from deskpilot.application.provider_catalog import ProviderCatalogService
from deskpilot.application.provider_management_service import (
    ProviderManagementService,
)
from deskpilot.application.research_runtime import ResearchRuntime
from deskpilot.application.task_service import TaskService
from deskpilot.application.task_workbench_service import TaskWorkbenchService
from deskpilot.observability import TelemetryFacade


def get_task_service(request: Request) -> TaskService:
    return cast(TaskService, request.app.state.task_service)


def get_agent_registry(request: Request) -> AgentRegistry:
    return cast(AgentRegistry, request.app.state.agent_registry)


def get_capability_catalog(request: Request) -> CapabilityCatalog:
    return cast(CapabilityCatalog, request.app.state.capability_catalog)


def get_plan_compilation_service(request: Request) -> PlanCompilationService:
    return cast(PlanCompilationService, request.app.state.plan_compilation_service)


def get_agent_execution_runtime(request: Request) -> AgentExecutionRuntime:
    return cast(AgentExecutionRuntime, request.app.state.agent_execution_runtime)


def get_artifact_delivery_runtime(request: Request) -> ArtifactDeliveryRuntime:
    return cast(ArtifactDeliveryRuntime, request.app.state.artifact_delivery_runtime)


def get_artifact_export_runtime(request: Request) -> ArtifactExportRuntime:
    return cast(ArtifactExportRuntime, request.app.state.artifact_export_runtime)


def get_task_workbench_service(request: Request) -> TaskWorkbenchService:
    return cast(TaskWorkbenchService, request.app.state.task_workbench_service)


def get_context_memory_runtime(request: Request) -> ContextMemoryRuntime:
    return cast(ContextMemoryRuntime, request.app.state.context_memory_runtime)


def get_long_term_memory_runtime(request: Request) -> LongTermMemoryRuntime:
    return cast(LongTermMemoryRuntime, request.app.state.long_term_memory_runtime)


def get_research_runtime(request: Request) -> ResearchRuntime:
    service = getattr(request.app.state, "research_runtime", None)
    if service is None:
        raise ProblemException(
            status_code=503,
            code="RESEARCH_RUNTIME_DISABLED",
            title="联网研究运行时未启用",
            detail="需要显式启用研究运行时并配置 SearchProvider。",
        )
    return cast(ResearchRuntime, service)


def get_processor(request: Request) -> TaskProcessor:
    return cast(TaskProcessor, request.app.state.processor)


def get_event_broker(request: Request) -> EventBroker:
    return cast(EventBroker, request.app.state.event_broker)


def get_effect_runtime_operations(request: Request) -> EffectRuntimeOperationsService:
    return cast(
        EffectRuntimeOperationsService,
        request.app.state.effect_runtime_operations,
    )


def get_knowledge_base(request: Request) -> LocalKnowledgeBase:
    return cast(LocalKnowledgeBase, request.app.state.knowledge_base)


def get_mcp_control_plane(request: Request) -> McpControlPlane:
    return cast(McpControlPlane, request.app.state.mcp_control_plane)


def get_evaluation_service(request: Request) -> EvaluationService:
    return cast(EvaluationService, request.app.state.evaluation_service)


def get_telemetry(request: Request) -> TelemetryFacade:
    return cast(TelemetryFacade, request.app.state.telemetry)


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
