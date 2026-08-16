"""Application settings."""

from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from deskpilot.domain.model_routing import ModelGatewayPolicy
from deskpilot.domain.provider_config import ProviderConfig


def _default_model_gateway_policy() -> ModelGatewayPolicy:
    return ModelGatewayPolicy(
        default_max_attempts=2,
        default_retry_delay_budget_seconds=2,
    )


class Settings(BaseSettings):
    """Validated runtime configuration."""

    app_name: str = "DeskPilot API"
    api_prefix: str = "/api/v1"
    database_url: str = "sqlite+aiosqlite:///./data/deskpilot.db"
    fake_step_delay_seconds: float = 0.15
    graph_lease_ttl_seconds: float = Field(default=15, ge=1, le=3_600)
    effect_dag_global_concurrency: int = Field(default=8, ge=1, le=1_024)
    effect_dag_graph_concurrency: int = Field(default=4, ge=1, le=32)
    effect_dag_tool_concurrency: int = Field(default=4, ge=1, le=1_024)
    effect_dag_ready_page_size: int = Field(default=64, ge=1, le=1_000)
    effect_dag_admission_lease_ttl_seconds: int = Field(default=15, ge=1, le=3_600)
    effect_dag_admission_poll_interval_seconds: float = Field(
        default=0.05,
        gt=0,
        le=60,
    )
    effect_graph_control_poll_interval_seconds: float = Field(
        default=0.05,
        gt=0,
        le=60,
    )
    effect_graph_control_claim_ttl_seconds: float = Field(
        default=15,
        ge=1,
        le=3_600,
    )
    effect_graph_control_request_timeout_seconds: float = Field(
        default=30,
        gt=0,
        le=3_600,
    )
    session_token: SecretStr | None = None
    outbox_poll_interval_seconds: float = Field(default=0.05, gt=0, le=60)
    outbox_batch_size: int = Field(default=100, ge=1, le=1_000)
    outbox_retry_base_seconds: float = Field(default=0.25, ge=0, le=60)
    outbox_retry_max_seconds: float = Field(default=30.0, ge=0, le=3_600)
    outbox_claim_ttl_seconds: float = Field(default=15.0, ge=1, le=3_600)
    outbox_max_attempts: int = Field(default=8, ge=1, le=1_000)
    event_transport: Literal["local", "rabbitmq"] = "local"
    rabbitmq_url: SecretStr | None = None
    rabbitmq_exchange: str = Field(
        default="deskpilot.events.v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    rabbitmq_queue: str = Field(
        default="deskpilot.task-events.websocket.v1",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    rabbitmq_routing_key: str = Field(
        default="task.event",
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$",
    )
    rabbitmq_prefetch_count: int = Field(default=32, ge=1, le=1_000)
    rabbitmq_connection_timeout_seconds: float = Field(default=10, gt=0, le=120)
    rabbitmq_publish_timeout_seconds: float = Field(default=10, gt=0, le=120)
    operations_retention_days: int = Field(default=30, ge=1, le=3_650)
    operations_retention_interval_seconds: float = Field(
        default=3_600,
        ge=1,
        le=604_800,
    )
    operations_metrics_interval_seconds: float = Field(
        default=300,
        ge=1,
        le=86_400,
    )
    operations_retention_batch_size: int = Field(default=1_000, ge=1, le=10_000)
    operations_stalled_after_seconds: float = Field(default=60, ge=1, le=86_400)
    runner_heartbeat_interval_seconds: float = Field(default=0.5, ge=0.1, le=60)
    runner_heartbeat_timeout_seconds: float = Field(default=3.0, gt=0.1, le=300)
    runner_startup_timeout_seconds: float = Field(default=10.0, gt=0, le=60)
    runner_shutdown_timeout_seconds: float = Field(default=2.0, gt=0, le=30)
    runner_restart_base_delay_seconds: float = Field(default=0.25, gt=0, le=60)
    runner_restart_max_delay_seconds: float = Field(default=10.0, gt=0, le=300)
    runner_circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    runner_circuit_recovery_timeout_seconds: float = Field(default=30.0, gt=0, le=3_600)
    runner_stable_window_seconds: float = Field(default=10.0, gt=0, le=3_600)
    runner_require_windows_sandbox: bool = True
    runner_require_network_isolation: bool = False
    runner_worker_runtime_root: str = Field(
        default="./data/worker-runtime",
        min_length=1,
        max_length=32_767,
    )
    runner_appcontainer_profile_journal_path: str = Field(
        default="./data/runner/appcontainer-profiles.json",
        min_length=1,
        max_length=32_767,
    )
    runner_commit_receipt_database_path: str = Field(
        default="./data/runner/commit-receipts.db",
        min_length=1,
        max_length=32_767,
    )
    runner_worker_memory_limit_bytes: int = Field(
        default=268_435_456,
        ge=67_108_864,
        le=2_147_483_648,
    )
    runner_worker_active_process_limit: int = Field(default=1, ge=1, le=16)
    policy_require_approval_for_r0: bool = False
    policy_approval_ttl_seconds: int = Field(default=300, ge=10, le=3_600)
    disk_usage_path: str = Field(default=".", min_length=1, max_length=32_767)
    model_default_provider_id: str = Field(default="fake-local", pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    fake_model_provider_id: str = Field(default="fake-local", pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    fake_model_name: str = Field(default="deskpilot-fake-v1", min_length=1, max_length=200)
    fake_model_delay_seconds: float = Field(default=0, ge=0, le=60)
    model_providers: tuple[ProviderConfig, ...] = Field(default=(), max_length=32)
    model_request_timeout_seconds: float = Field(default=10, gt=0, le=600)
    model_gateway_policy: ModelGatewayPolicy = Field(default_factory=_default_model_gateway_policy)
    model_health_cache_ttl_seconds: float = Field(default=15, gt=0, le=300)
    model_health_max_concurrency: int = Field(default=4, ge=1, le=16)
    model_health_probe_timeout_seconds: float = Field(default=5, gt=0, le=30)
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="DESKPILOT_",
        extra="ignore",
    )

    @field_validator("model_providers")
    @classmethod
    def validate_unique_provider_ids(
        cls,
        providers: tuple[ProviderConfig, ...],
    ) -> tuple[ProviderConfig, ...]:
        provider_ids = [provider.provider_id for provider in providers]
        if len(provider_ids) != len(set(provider_ids)):
            raise ValueError("model_providers must use unique provider_id values")
        return providers

    @model_validator(mode="after")
    def validate_runner_recovery_policy(self) -> Self:
        if self.runner_heartbeat_timeout_seconds <= self.runner_heartbeat_interval_seconds:
            raise ValueError(
                "runner_heartbeat_timeout_seconds must exceed runner_heartbeat_interval_seconds"
            )
        if self.runner_restart_max_delay_seconds < self.runner_restart_base_delay_seconds:
            raise ValueError(
                "runner_restart_max_delay_seconds must be at least "
                "runner_restart_base_delay_seconds"
            )
        if self.effect_dag_graph_concurrency > self.effect_dag_global_concurrency:
            raise ValueError(
                "effect_dag_graph_concurrency must not exceed effect_dag_global_concurrency"
            )
        if self.effect_dag_tool_concurrency > self.effect_dag_global_concurrency:
            raise ValueError(
                "effect_dag_tool_concurrency must not exceed effect_dag_global_concurrency"
            )
        if self.event_transport == "rabbitmq" and self.rabbitmq_url is None:
            raise ValueError("rabbitmq_url is required when event_transport is rabbitmq")
        return self
