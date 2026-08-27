"""DeskPilot FastAPI composition root."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException

from deskpilot.agents import create_builtin_agent_registry
from deskpilot.api.problem_details import (
    ProblemException,
    http_exception_handler,
    internal_exception_handler,
    problem_exception_handler,
    validation_exception_handler,
)
from deskpilot.api.routes import (
    agent_runtime,
    agents,
    approvals,
    context_memory,
    evaluations,
    health,
    knowledge,
    long_term_memory,
    mcp,
    model_providers,
    operations,
    planning,
    reconciliations,
    session,
    task_workbench,
    tasks,
    telemetry,
    websocket,
)
from deskpilot.api.security_middleware import LocalApiSecurityMiddleware
from deskpilot.application.agent_execution_runtime import AgentExecutionRuntime
from deskpilot.application.agent_model_admission import load_agent_model_admissions
from deskpilot.application.agent_model_loop import AgentModelLoopRuntime
from deskpilot.application.agent_release_lifecycle import load_agent_release_activations
from deskpilot.application.agent_supervisor_runtime import AgentSupervisorRuntime
from deskpilot.application.artifact_delivery_runtime import ArtifactDeliveryRuntime
from deskpilot.application.artifact_export_runtime import ArtifactExportRuntime
from deskpilot.application.browser_verifier import (
    BrowserVerifier,
    IsolatedChromiumVerifier,
)
from deskpilot.application.builtin_capability_executors import (
    create_builtin_capability_executor_registry,
)
from deskpilot.application.capability_catalog import create_builtin_capability_catalog
from deskpilot.application.capability_execution_engine import CapabilityExecutionEngine
from deskpilot.application.capability_execution_runtime import CapabilityExecutionRuntime
from deskpilot.application.capability_input_binding_catalog import (
    CapabilityInputBindingCatalog,
)
from deskpilot.application.command_profile_catalog import CommandProfileCatalog
from deskpilot.application.context_memory_runtime import ContextMemoryRuntime
from deskpilot.application.credential_resolver import CredentialResolver
from deskpilot.application.effect_dag_cluster_admission import (
    EffectDagClusterAdmissionController,
    EffectDagClusterAdmissionStore,
)
from deskpilot.application.effect_graph_control_router import (
    EffectGraphControlRouter,
    EffectGraphControlStore,
)
from deskpilot.application.effect_runtime_operations import EffectRuntimeOperationsService
from deskpilot.application.evaluation_service import EvaluationService
from deskpilot.application.event_broker import EventBroker
from deskpilot.application.inbox_consumer import InboxConsumer
from deskpilot.application.knowledge_base import LocalKnowledgeBase
from deskpilot.application.long_term_memory_runtime import LongTermMemoryRuntime
from deskpilot.application.mcp_control_plane import McpControlPlane
from deskpilot.application.model_gateway import (
    ModelGateway,
    ModelProvider,
)
from deskpilot.application.model_planner_composer import ModelPlannerComposer
from deskpilot.application.model_planner_node_binder import ModelPlannerNodeBinder
from deskpilot.application.multi_step_plan_runtime import MultiStepPlanRuntime
from deskpilot.application.outbox_publisher import DeliveryPublisher, OutboxPublisher
from deskpilot.application.pdf_artifact_renderer import (
    IsolatedPdfArtifactRenderer,
    PdfArtifactRenderer,
)
from deskpilot.application.plan_compilation_service import PlanCompilationService
from deskpilot.application.plan_compiler import PlanCompiler
from deskpilot.application.policy_engine import BuiltinPolicyEngine, PolicyEngine
from deskpilot.application.processor import TaskProcessor
from deskpilot.application.provider_catalog import ProviderCatalogService
from deskpilot.application.provider_health_service import ProviderHealthService
from deskpilot.application.provider_management_service import (
    ProviderManagementService,
)
from deskpilot.application.provider_runtime_codec import ProviderRuntimeConfigCodec
from deskpilot.application.provider_runtime_store import RuntimeConfigProtector
from deskpilot.application.research_runtime import ResearchRuntime
from deskpilot.application.runner_client import RunnerClient
from deskpilot.application.runner_supervisor import RunnerSupervisor
from deskpilot.application.task_checkpoint_codec import TaskCheckpointCodec
from deskpilot.application.task_loop_activation_runtime import TaskLoopActivationRuntime
from deskpilot.application.task_loop_agent_adapter_registry import (
    create_task_loop_agent_adapter_registry,
)
from deskpilot.application.task_loop_agent_runtime import TaskLoopAgentRuntime
from deskpilot.application.task_loop_execution_coordinator import (
    TaskLoopExecutionCoordinator,
)
from deskpilot.application.task_service import TaskService
from deskpilot.application.task_workbench_service import TaskWorkbenchService
from deskpilot.application.turn_planner_runtime import TurnPlannerRuntime
from deskpilot.application.turn_router import (
    TurnRouter,
    WorkspaceCheckPort,
    WorkspaceNodeTestPort,
    WorkspacePythonTestPort,
)
from deskpilot.application.web_research import (
    SafePageReader,
    SearchProvider,
    SearxngSearchProvider,
)
from deskpilot.application.workbench_runtime_coordinator import (
    WorkbenchRuntimeCoordinator,
)
from deskpilot.application.workspace_agent_runtime import WorkspaceAgentRuntime
from deskpilot.application.workspace_check_runtime import WorkspaceCheckRuntime
from deskpilot.application.workspace_coding_change_runtime import WorkspaceCodingChangeRuntime
from deskpilot.application.workspace_coding_exploration_binder import (
    WorkspaceCodingExplorationBinder,
)
from deskpilot.application.workspace_coding_explorer_runtime import (
    WorkspaceCodingExplorerRuntime,
)
from deskpilot.application.workspace_coding_runtime import WorkspaceCodingRuntime
from deskpilot.application.workspace_command_plan_binder import WorkspaceCommandPlanBinder
from deskpilot.application.workspace_command_plan_compiler import WorkspaceCommandPlanCompiler
from deskpilot.application.workspace_command_runtime import WorkspaceCommandRuntime
from deskpilot.application.workspace_file_runtime import WorkspaceFileRuntime
from deskpilot.application.workspace_node_test_runtime import WorkspaceNodeTestRuntime
from deskpilot.application.workspace_python_test_runtime import WorkspacePythonTestRuntime
from deskpilot.core.config import Settings
from deskpilot.core.security import LocalSessionSecurity
from deskpilot.domain.provider_management import (
    ProviderCatalogDefinition,
    ProviderCatalogDefinitionEntry,
)
from deskpilot.infrastructure.credential_resolvers import (
    create_default_credential_resolver,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.provider_catalog_repository import (
    SqlAlchemyProviderCatalogRepository,
)
from deskpilot.infrastructure.provider_management_repository import (
    SqlAlchemyProviderManagementRepository,
)
from deskpilot.infrastructure.rabbitmq_transport import (
    RabbitMqEventTransport,
    verify_task_event_delivery,
)
from deskpilot.infrastructure.windows_dpapi import WindowsDpapiProtector
from deskpilot.model_providers.factory import (
    effective_model_provider_configs,
)
from deskpilot.observability import TelemetryFacade
from deskpilot.tools import create_builtin_registry
from deskpilot.tools.files import (
    FILE_MOVE_DESTINATION_CAPABILITY,
    FILE_MOVE_SOURCE_CAPABILITY,
)


def create_app(
    settings: Settings | None = None,
    *,
    model_provider: ModelProvider | None = None,
    credential_resolver: CredentialResolver | None = None,
    runtime_config_protector: RuntimeConfigProtector | None = None,
    runner_supervisor: RunnerSupervisor | None = None,
    policy_engine: PolicyEngine | None = None,
    search_provider: SearchProvider | None = None,
    page_reader: SafePageReader | None = None,
    browser_verifier: BrowserVerifier | None = None,
    pdf_artifact_renderer: PdfArtifactRenderer | None = None,
    workspace_check_runtime: WorkspaceCheckPort | None = None,
    workspace_python_test_runtime: WorkspacePythonTestPort | None = None,
    workspace_node_test_runtime: WorkspaceNodeTestPort | None = None,
) -> FastAPI:
    resolved_settings = settings or Settings()
    instance_id = f"api_{uuid4().hex}"
    telemetry_facade = TelemetryFacade(
        capacity=resolved_settings.telemetry_local_span_capacity,
        enabled=resolved_settings.telemetry_enabled,
    )
    session_security = LocalSessionSecurity.create(
        resolved_settings.session_token,
        resolved_settings.cors_origins,
    )
    model_gateway = ModelGateway(
        default_provider_id=resolved_settings.model_default_provider_id,
        policy=resolved_settings.model_gateway_policy,
        telemetry=telemetry_facade,
    )
    provider_configs = effective_model_provider_configs(resolved_settings)
    resolved_search_provider = search_provider
    if resolved_search_provider is None and resolved_settings.research_search_base_url:
        resolved_search_provider = SearxngSearchProvider(resolved_settings.research_search_base_url)
    if resolved_settings.research_runtime_enabled and resolved_search_provider is None:
        raise ValueError("research_runtime_enabled requires a configured SearchProvider")
    canonical_disk_usage_path = str(
        Path(resolved_settings.disk_usage_path).expanduser().resolve(strict=True)
    )
    provider_catalog_definition: ProviderCatalogDefinition | None = None
    if model_provider is not None:
        model_gateway.register(model_provider)
        model_gateway.validate_configuration()
        provider_catalog_definition = ProviderCatalogDefinition(
            default_provider_id=resolved_settings.model_default_provider_id,
            providers=(
                ProviderCatalogDefinitionEntry(
                    descriptor=model_provider.descriptor,
                    enabled=True,
                ),
            ),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        database = Database(resolved_settings.database_url)
        resolved_runtime_config_protector = runtime_config_protector or WindowsDpapiProtector()
        provider_health_service = ProviderHealthService(
            model_gateway,
            cache_ttl_seconds=resolved_settings.model_health_cache_ttl_seconds,
            max_concurrency=resolved_settings.model_health_max_concurrency,
            probe_timeout_seconds=(resolved_settings.model_health_probe_timeout_seconds),
        )
        provider_management: ProviderManagementService | None = None
        try:
            await database.migrate()
            provider_catalog_repository = SqlAlchemyProviderCatalogRepository(database)
            if model_provider is not None:
                if provider_catalog_definition is None:
                    raise RuntimeError("Injected Provider catalog is missing")
                await provider_catalog_repository.import_definition(provider_catalog_definition)
            else:
                resolved_credential_resolver = (
                    credential_resolver or create_default_credential_resolver()
                )
                management_repository = SqlAlchemyProviderManagementRepository(
                    database,
                    ProviderRuntimeConfigCodec(resolved_runtime_config_protector),
                )
                provider_management = ProviderManagementService(
                    store=management_repository,
                    credential_resolver=resolved_credential_resolver,
                    gateway=model_gateway,
                    health_service=provider_health_service,
                )
                await provider_management.initialize(
                    provider_configs,
                    default_provider_id=(resolved_settings.model_default_provider_id),
                )
        except BaseException:
            await provider_health_service.shutdown()
            await database.dispose()
            telemetry_facade.shutdown()
            raise
        provider_catalog = ProviderCatalogService(
            store=provider_catalog_repository,
            health_service=provider_health_service,
        )
        broker = EventBroker()
        rabbitmq_transport: RabbitMqEventTransport | None = None
        delivery_publisher: DeliveryPublisher = broker
        if resolved_settings.event_transport == "rabbitmq":
            rabbitmq_secret = resolved_settings.rabbitmq_url
            if rabbitmq_secret is None:
                raise RuntimeError("Validated RabbitMQ URL is missing")

            inbox_consumer = InboxConsumer(
                database,
                consumer_name="rabbitmq.task-event.websocket-v1",
                handler=verify_task_event_delivery,
            )
            rabbitmq_transport = RabbitMqEventTransport(
                rabbitmq_secret.get_secret_value(),
                exchange_name=resolved_settings.rabbitmq_exchange,
                queue_name=resolved_settings.rabbitmq_queue,
                routing_key=resolved_settings.rabbitmq_routing_key,
                inbox_consumer=inbox_consumer,
                live_broker=broker,
                prefetch_count=resolved_settings.rabbitmq_prefetch_count,
                connection_timeout_seconds=(resolved_settings.rabbitmq_connection_timeout_seconds),
                publish_timeout_seconds=resolved_settings.rabbitmq_publish_timeout_seconds,
            )
            delivery_publisher = rabbitmq_transport.publisher
        outbox_publisher = OutboxPublisher(
            database,
            delivery_publisher,
            poll_interval_seconds=resolved_settings.outbox_poll_interval_seconds,
            batch_size=resolved_settings.outbox_batch_size,
            retry_base_seconds=resolved_settings.outbox_retry_base_seconds,
            retry_max_seconds=resolved_settings.outbox_retry_max_seconds,
            instance_id=instance_id,
            claim_ttl_seconds=resolved_settings.outbox_claim_ttl_seconds,
            max_attempts=resolved_settings.outbox_max_attempts,
        )
        effect_runtime_operations = EffectRuntimeOperationsService(
            database,
            outbox_notify=outbox_publisher.notify,
            retention_days=resolved_settings.operations_retention_days,
            retention_interval_seconds=(resolved_settings.operations_retention_interval_seconds),
            metrics_interval_seconds=resolved_settings.operations_metrics_interval_seconds,
            retention_batch_size=resolved_settings.operations_retention_batch_size,
            stalled_after_seconds=resolved_settings.operations_stalled_after_seconds,
        )
        task_service = TaskService(
            database,
            resolved_settings.api_prefix,
            outbox_notify=outbox_publisher.notify,
            checkpoint_codec=TaskCheckpointCodec(resolved_runtime_config_protector),
        )
        knowledge_base = LocalKnowledgeBase(database)
        mcp_control_plane = McpControlPlane(database, telemetry=telemetry_facade)
        evaluation_service = EvaluationService(database, telemetry=telemetry_facade)
        registry = create_builtin_registry()
        agent_model_admissions = load_agent_model_admissions(
            (
                Path(resolved_settings.model_admission_bundle_path)
                if resolved_settings.model_admission_bundle_path is not None
                else None
            ),
            explicitly_allowed=resolved_settings.model_admission_allow,
        )
        agent_release_activations = load_agent_release_activations(
            (
                Path(resolved_settings.agent_release_bundle_path)
                if resolved_settings.agent_release_bundle_path is not None
                else None
            ),
            explicitly_allowed=resolved_settings.agent_release_allow,
        )
        agent_registry = create_builtin_agent_registry(
            registry,
            model_gateway.descriptors(),
            agent_model_admissions,
            agent_release_activations,
        )
        capability_catalog = create_builtin_capability_catalog(
            research_runtime_enabled=(
                resolved_settings.research_runtime_enabled and resolved_search_provider is not None
            )
        )
        plan_compiler = PlanCompiler(agent_registry, registry, capability_catalog)
        plan_compilation_service = PlanCompilationService(
            database,
            plan_compiler,
        )
        agent_execution_runtime = AgentExecutionRuntime(database, plan_compiler, agent_registry)
        long_term_memory_runtime = LongTermMemoryRuntime(
            database, resolved_runtime_config_protector
        )
        context_memory_runtime = ContextMemoryRuntime(database, long_term_memory_runtime)
        research_runtime = (
            ResearchRuntime(
                database,
                agent_execution_runtime,
                model_gateway,
                resolved_search_provider,
                page_reader or SafePageReader(),
                context_memory_runtime,
            )
            if resolved_settings.research_runtime_enabled and resolved_search_provider is not None
            else None
        )
        artifact_delivery_runtime = ArtifactDeliveryRuntime(
            database,
            model_gateway,
            browser_verifier or IsolatedChromiumVerifier(resolved_settings.browser_executable_path),
            pdf_artifact_renderer
            or IsolatedPdfArtifactRenderer(
                resolved_settings.browser_executable_path,
                resolved_settings.pdfinfo_executable_path,
                resolved_settings.pdftoppm_executable_path,
            ),
            resolved_settings.artifact_workspace_root,
        )
        artifact_export_runtime = ArtifactExportRuntime(
            database,
            resolved_settings.artifact_workspace_root,
        )
        workspace_file_runtime = WorkspaceFileRuntime(
            resolved_settings.conversation_workspace_root,
            resolved_settings.artifact_workspace_root,
        )
        workspace_coding_runtime = WorkspaceCodingRuntime(workspace_file_runtime)
        workspace_coding_explorations = WorkspaceCodingExplorationBinder(
            database,
            workspace_coding_runtime,
            agent_registry,
            capability_catalog,
            plan_compilation_service,
        )
        command_profile_catalog = CommandProfileCatalog()
        workspace_command_plan_binder = WorkspaceCommandPlanBinder(
            WorkspaceCommandPlanCompiler(
                command_profile_catalog,
                workspace_file_runtime,
            )
        )
        workspace_command_runtime = WorkspaceCommandRuntime(
            resolved_settings.runner_worker_runtime_root,
            resolved_settings.runner_appcontainer_profile_journal_path,
        )
        agent_model_loop_runtime = AgentModelLoopRuntime(
            database,
            agent_execution_runtime,
            agent_registry,
            model_gateway,
            context_memory_runtime,
        )
        workspace_coding_explorer_runtime = WorkspaceCodingExplorerRuntime(
            database,
            workspace_coding_explorations,
            agent_registry,
            plan_compilation_service,
            agent_execution_runtime,
            agent_model_loop_runtime,
        )
        workspace_coding_change_runtime = WorkspaceCodingChangeRuntime(
            database,
            workspace_coding_explorations,
            agent_registry,
            capability_catalog,
            plan_compilation_service,
            agent_execution_runtime,
            agent_model_loop_runtime,
        )
        agent_supervisor_runtime = AgentSupervisorRuntime(
            database,
            agent_registry,
            capability_catalog,
        )
        resolved_workspace_check_runtime = workspace_check_runtime or WorkspaceCheckRuntime(
            resolved_settings.runner_worker_runtime_root,
            resolved_settings.runner_appcontainer_profile_journal_path,
        )
        resolved_workspace_python_test_runtime = (
            workspace_python_test_runtime
            or WorkspacePythonTestRuntime(
                resolved_settings.runner_worker_runtime_root,
                resolved_settings.runner_appcontainer_profile_journal_path,
            )
        )
        resolved_workspace_node_test_runtime = (
            workspace_node_test_runtime
            or WorkspaceNodeTestRuntime(
                resolved_settings.node_test_runtime_root,
                resolved_settings.runner_appcontainer_profile_journal_path,
                resolved_settings.node_test_executable_path,
            )
        )
        workspace_agent_runtime = WorkspaceAgentRuntime(
            database,
            agent_execution_runtime,
            agent_model_loop_runtime,
            workspace_file_runtime,
            agent_registry,
            agent_supervisor_runtime,
            resolved_workspace_python_test_runtime,
            resolved_workspace_node_test_runtime,
        )
        turn_router = TurnRouter(
            database,
            knowledge_base,
            mcp_control_plane,
            workspace_file_runtime,
            resolved_workspace_check_runtime,
            resolved_workspace_python_test_runtime,
            resolved_workspace_node_test_runtime,
        )
        turn_planner_runtime = TurnPlannerRuntime(
            database,
            agent_registry,
            model_gateway,
            capability_catalog,
            plan_compilation_service,
        )
        model_planner_composer = ModelPlannerComposer(
            plan_compiler,
            capability_catalog,
        )
        task_loop_runtime = MultiStepPlanRuntime(
            database,
            turn_planner_runtime,
            model_planner_composer,
            command_plans=workspace_command_plan_binder,
        )
        task_loop_capability_executors = create_builtin_capability_executor_registry(
            capability_catalog,
            knowledge=knowledge_base,
            mcp=mcp_control_plane,
            workspace=workspace_file_runtime,
            workspace_checks=resolved_workspace_check_runtime,
            python_tests=resolved_workspace_python_test_runtime,
            node_tests=resolved_workspace_node_test_runtime,
            workspace_patches=workspace_file_runtime,
            workspace_coding=workspace_coding_runtime,
            command_profiles=command_profile_catalog,
            command_snapshots=workspace_coding_runtime,
            command_runtime=workspace_command_runtime,
            artifacts=artifact_delivery_runtime,
        )
        task_loop_agent_adapters = create_task_loop_agent_adapter_registry(
            research_available=research_runtime is not None,
            workspace_file_available=True,
            workspace_coding_loop_available=True,
        )
        task_loop_activation_runtime = TaskLoopActivationRuntime(
            database,
            task_loop_runtime,
            turn_planner_runtime,
            plan_compilation_service,
            agent_execution_runtime,
            ModelPlannerNodeBinder(
                agent_registry,
                task_loop_capability_executors,
                task_loop_agent_adapters,
            ),
            command_plans=workspace_command_plan_binder,
            workspace_coding_explorations=workspace_coding_explorations,
            workspace_coding_changes=workspace_coding_change_runtime,
        )
        task_loop_capability_runtime = CapabilityExecutionRuntime(
            database,
            CapabilityInputBindingCatalog(capability_catalog),
            task_loop_capability_executors,
            CapabilityExecutionEngine(task_loop_capability_executors),
            command_plans=workspace_command_plan_binder,
            workspace_coding_changes=workspace_coding_change_runtime,
        )
        task_loop_agent_runtime = TaskLoopAgentRuntime(
            database,
            agent_execution_runtime,
            task_loop_agent_adapters,
            research=research_runtime,
            workspace=workspace_file_runtime,
            model_loop=agent_model_loop_runtime,
            workspace_coding_changes=workspace_coding_change_runtime,
        )
        task_loop_execution_runtime = TaskLoopExecutionCoordinator(
            database,
            task_loop_activation_runtime,
            capabilities=task_loop_capability_runtime,
            agents=task_loop_agent_runtime,
            artifacts=artifact_delivery_runtime,
            turn_planner=turn_planner_runtime,
        )
        task_workbench_service = TaskWorkbenchService(
            database,
            task_service,
            context_memory_runtime,
            plan_compilation_service,
            capability_catalog,
            agent_execution_runtime,
            research_runtime,
            workspace_agent_runtime,
            long_term_memory_runtime,
            artifact_delivery_runtime,
            artifact_export_runtime,
            turn_router,
            turn_planner_runtime,
            task_loop_runtime,
            task_loop_activation_runtime,
            task_loop_execution_runtime,
            command_profile_ids=workspace_command_runtime.enabled_profile_ids,
            workspace_coding_explorations=workspace_coding_explorations,
            workspace_coding_changes=workspace_coding_change_runtime,
            workspace_coding_explorer=workspace_coding_explorer_runtime,
        )
        workbench_runtime = (
            WorkbenchRuntimeCoordinator(
                database,
                task_workbench_service,
                instance_id=instance_id,
                poll_interval_seconds=(resolved_settings.workbench_runtime_poll_interval_seconds),
                claim_ttl_seconds=(resolved_settings.workbench_runtime_claim_ttl_seconds),
                concurrency=resolved_settings.workbench_runtime_concurrency,
                max_failures=resolved_settings.workbench_runtime_max_failures,
                retry_base_seconds=(resolved_settings.workbench_runtime_retry_base_seconds),
                retry_max_seconds=(resolved_settings.workbench_runtime_retry_max_seconds),
            )
            if resolved_settings.workbench_runtime_enabled
            else None
        )
        if workbench_runtime is not None:
            task_workbench_service.bind_auto_advance(workbench_runtime)
        resolved_policy_engine = policy_engine or BuiltinPolicyEngine(
            allowed_capabilities=(
                "filesystem.metadata.read",
                FILE_MOVE_DESTINATION_CAPABILITY,
                FILE_MOVE_SOURCE_CAPABILITY,
            ),
            allowed_resource_scopes=(("filesystem_path", canonical_disk_usage_path),),
            allow_user_selected_file_move=True,
            allow_user_selected_disk_usage=True,
            require_approval_for_r0=(resolved_settings.policy_require_approval_for_r0),
            approval_ttl_seconds=resolved_settings.policy_approval_ttl_seconds,
        )
        resolved_runner_supervisor = runner_supervisor or RunnerSupervisor(
            client_factory=lambda: RunnerClient(
                registry=registry,
                heartbeat_interval_seconds=(resolved_settings.runner_heartbeat_interval_seconds),
                heartbeat_timeout_seconds=(resolved_settings.runner_heartbeat_timeout_seconds),
                startup_timeout_seconds=(resolved_settings.runner_startup_timeout_seconds),
                shutdown_timeout_seconds=(resolved_settings.runner_shutdown_timeout_seconds),
                require_windows_sandbox=(resolved_settings.runner_require_windows_sandbox),
                require_network_isolation=(resolved_settings.runner_require_network_isolation),
                worker_runtime_root=(resolved_settings.runner_worker_runtime_root),
                appcontainer_profile_journal_path=(
                    resolved_settings.runner_appcontainer_profile_journal_path
                ),
                commit_receipt_database_path=(
                    resolved_settings.runner_commit_receipt_database_path
                ),
                worker_memory_limit_bytes=(resolved_settings.runner_worker_memory_limit_bytes),
                worker_active_process_limit=(resolved_settings.runner_worker_active_process_limit),
                telemetry=telemetry_facade,
            ),
            restart_base_delay_seconds=(resolved_settings.runner_restart_base_delay_seconds),
            restart_max_delay_seconds=(resolved_settings.runner_restart_max_delay_seconds),
            circuit_failure_threshold=(resolved_settings.runner_circuit_failure_threshold),
            circuit_recovery_timeout_seconds=(
                resolved_settings.runner_circuit_recovery_timeout_seconds
            ),
            stable_window_seconds=resolved_settings.runner_stable_window_seconds,
        )
        effect_dag_admission = EffectDagClusterAdmissionController(
            EffectDagClusterAdmissionStore(database),
            owner_id=f"{instance_id[:58]}:dag",
            global_limit=resolved_settings.effect_dag_global_concurrency,
            per_graph_limit=resolved_settings.effect_dag_graph_concurrency,
            default_tool_limit=resolved_settings.effect_dag_tool_concurrency,
            lease_ttl_seconds=(resolved_settings.effect_dag_admission_lease_ttl_seconds),
            poll_interval_seconds=(resolved_settings.effect_dag_admission_poll_interval_seconds),
        )
        processor = TaskProcessor(
            task_service,
            model_gateway,
            resolved_policy_engine,
            resolved_runner_supervisor,
            step_delay_seconds=resolved_settings.fake_step_delay_seconds,
            disk_usage_path=canonical_disk_usage_path,
            model_timeout_seconds=resolved_settings.model_request_timeout_seconds,
            instance_id=instance_id,
            graph_lease_ttl_seconds=resolved_settings.graph_lease_ttl_seconds,
            effect_dag_admission=effect_dag_admission,
            effect_dag_max_concurrency=(resolved_settings.effect_dag_graph_concurrency),
            effect_dag_ready_page_size=(resolved_settings.effect_dag_ready_page_size),
        )
        effect_graph_control_router = EffectGraphControlRouter(
            EffectGraphControlStore(database),
            task_service,
            owner_id=processor.dag_owner_id,
            handler=processor.apply_effect_graph_control,
            applied_callback=processor.forget,
            poll_interval_seconds=(resolved_settings.effect_graph_control_poll_interval_seconds),
            claim_ttl_seconds=(resolved_settings.effect_graph_control_claim_ttl_seconds),
            request_timeout_seconds=(
                resolved_settings.effect_graph_control_request_timeout_seconds
            ),
            graph_lease_ttl_seconds=resolved_settings.graph_lease_ttl_seconds,
        )
        processor.bind_effect_graph_control_router(effect_graph_control_router)

        app.state.database = database
        app.state.event_broker = broker
        app.state.outbox_publisher = outbox_publisher
        app.state.rabbitmq_transport = rabbitmq_transport
        app.state.effect_runtime_operations = effect_runtime_operations
        app.state.task_service = task_service
        app.state.agent_registry = agent_registry
        app.state.agent_model_admissions = agent_model_admissions
        app.state.agent_release_activations = agent_release_activations
        app.state.capability_catalog = capability_catalog
        app.state.plan_compilation_service = plan_compilation_service
        app.state.agent_execution_runtime = agent_execution_runtime
        app.state.agent_supervisor_runtime = agent_supervisor_runtime
        app.state.context_memory_runtime = context_memory_runtime
        app.state.long_term_memory_runtime = long_term_memory_runtime
        app.state.research_runtime = research_runtime
        app.state.artifact_delivery_runtime = artifact_delivery_runtime
        app.state.artifact_export_runtime = artifact_export_runtime
        app.state.task_workbench_service = task_workbench_service
        app.state.workbench_runtime = workbench_runtime
        app.state.turn_router = turn_router
        app.state.turn_planner_runtime = turn_planner_runtime
        app.state.task_loop_runtime = task_loop_runtime
        app.state.task_loop_activation_runtime = task_loop_activation_runtime
        app.state.task_loop_execution_runtime = task_loop_execution_runtime
        app.state.task_loop_capability_runtime = task_loop_capability_runtime
        app.state.task_loop_agent_runtime = task_loop_agent_runtime
        app.state.workspace_file_runtime = workspace_file_runtime
        app.state.workspace_coding_runtime = workspace_coding_runtime
        app.state.workspace_coding_explorations = workspace_coding_explorations
        app.state.workspace_coding_explorer_runtime = workspace_coding_explorer_runtime
        app.state.workspace_coding_change_runtime = workspace_coding_change_runtime
        app.state.command_profile_catalog = command_profile_catalog
        app.state.workspace_command_runtime = workspace_command_runtime
        app.state.workspace_agent_runtime = workspace_agent_runtime
        app.state.workspace_check_runtime = resolved_workspace_check_runtime
        app.state.workspace_python_test_runtime = resolved_workspace_python_test_runtime
        app.state.workspace_node_test_runtime = resolved_workspace_node_test_runtime
        app.state.knowledge_base = knowledge_base
        app.state.mcp_control_plane = mcp_control_plane
        app.state.evaluation_service = evaluation_service
        app.state.telemetry = telemetry_facade
        app.state.processor = processor
        app.state.effect_dag_admission = effect_dag_admission
        app.state.effect_graph_control_router = effect_graph_control_router
        app.state.model_gateway = model_gateway
        app.state.policy_engine = resolved_policy_engine
        app.state.provider_catalog = provider_catalog
        app.state.provider_management = provider_management
        app.state.runner_client = resolved_runner_supervisor
        app.state.runner_supervisor = resolved_runner_supervisor
        app.state.session_security = session_security

        try:
            app.state.task_runtime_recovery = await processor.prepare_durable_recovery()
            app.state.approval_recovery = await task_service.recover_pending_approvals(
                recoverable_task_ids=(app.state.task_runtime_recovery.restored_task_ids),
                excluded_task_ids=(app.state.task_runtime_recovery.contended_task_ids),
                lease_owner_id=instance_id,
                lease_ttl_seconds=resolved_settings.graph_lease_ttl_seconds,
            )
            app.state.tool_call_recovery = await task_service.recover_incomplete_tool_calls(
                recoverable_requested_call_ids=(
                    app.state.task_runtime_recovery.recoverable_requested_call_ids
                ),
                excluded_task_ids=(app.state.task_runtime_recovery.contended_task_ids),
                lease_owner_id=instance_id,
                lease_ttl_seconds=resolved_settings.graph_lease_ttl_seconds,
            )
            await resolved_runner_supervisor.start()
            if rabbitmq_transport is not None:
                await rabbitmq_transport.start()
            outbox_publisher.start()
            effect_runtime_operations.start()
            effect_graph_control_router.start()
            if workbench_runtime is not None:
                workbench_runtime.start()
            processor.activate_durable_recovery()
            yield
        finally:
            if workbench_runtime is not None:
                await workbench_runtime.shutdown()
            await provider_catalog.shutdown()
            await effect_runtime_operations.shutdown()
            await effect_graph_control_router.shutdown()
            await processor.shutdown()
            await effect_dag_admission.shutdown()
            await resolved_runner_supervisor.stop()
            await outbox_publisher.shutdown()
            if rabbitmq_transport is not None:
                await rabbitmq_transport.shutdown()
            await database.dispose()
            telemetry_facade.shutdown()

    app = FastAPI(
        title=resolved_settings.app_name,
        version="0.1.0",
        lifespan=lifespan,
    )
    app.add_exception_handler(ProblemException, problem_exception_handler)
    app.add_exception_handler(HTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(Exception, internal_exception_handler)
    app.add_middleware(
        LocalApiSecurityMiddleware,
        security=session_security,
        api_prefix=resolved_settings.api_prefix,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=resolved_settings.cors_origins,
        allow_credentials=False,
        allow_methods=["DELETE", "GET", "POST", "PUT"],
        allow_headers=[
            "Authorization",
            "Content-Type",
            "Idempotency-Key",
            "If-Match",
            "X-DeskPilot-Client",
        ],
        expose_headers=["ETag"],
    )
    app.include_router(health.router, prefix=resolved_settings.api_prefix)
    app.include_router(agents.router, prefix=resolved_settings.api_prefix)
    app.include_router(planning.router, prefix=resolved_settings.api_prefix)
    app.include_router(agent_runtime.router, prefix=resolved_settings.api_prefix)
    app.include_router(context_memory.router, prefix=resolved_settings.api_prefix)
    app.include_router(long_term_memory.router, prefix=resolved_settings.api_prefix)
    app.include_router(session.router, prefix=resolved_settings.api_prefix)
    app.include_router(model_providers.router, prefix=resolved_settings.api_prefix)
    app.include_router(tasks.router, prefix=resolved_settings.api_prefix)
    app.include_router(task_workbench.router, prefix=resolved_settings.api_prefix)
    app.include_router(approvals.router, prefix=resolved_settings.api_prefix)
    app.include_router(reconciliations.router, prefix=resolved_settings.api_prefix)
    app.include_router(operations.router, prefix=resolved_settings.api_prefix)
    app.include_router(knowledge.router, prefix=resolved_settings.api_prefix)
    app.include_router(mcp.router, prefix=resolved_settings.api_prefix)
    app.include_router(evaluations.router, prefix=resolved_settings.api_prefix)
    app.include_router(telemetry.router, prefix=resolved_settings.api_prefix)
    app.include_router(websocket.router, prefix=resolved_settings.api_prefix)
    return app


app = create_app()
