"""Validated role routing, pricing, retry, and circuit-breaker contracts."""

import re
from datetime import datetime
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.model_contracts import (
    PROVIDER_ID_PATTERN,
    ModelRole,
    ModelUsage,
)


class ModelRouteStrategy(StrEnum):
    PRIORITY = "priority"
    LATENCY_AWARE = "latency_aware"


class ModelCircuitState(StrEnum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class ModelRoleRoute(BaseModel):
    """Ordered allowlist for one model role."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ModelRole
    provider_ids: tuple[str, ...] = Field(min_length=1, max_length=32)
    strategy: ModelRouteStrategy = ModelRouteStrategy.PRIORITY

    @model_validator(mode="after")
    def validate_provider_ids(self) -> Self:
        if len(set(self.provider_ids)) != len(self.provider_ids):
            raise ValueError("Role route Provider IDs must be unique")
        for provider_id in self.provider_ids:
            if re.fullmatch(PROVIDER_ID_PATTERN, provider_id) is None:
                raise ValueError("Role route Provider ID is invalid")
        return self


class ModelProviderPricing(BaseModel):
    """Integer micro-dollar rates per one million tokens."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    input_micros_per_million_tokens: int = Field(
        default=0,
        ge=0,
        le=1_000_000_000_000,
    )
    cached_input_micros_per_million_tokens: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000_000,
    )
    output_micros_per_million_tokens: int = Field(
        default=0,
        ge=0,
        le=1_000_000_000_000,
    )

    @model_validator(mode="after")
    def validate_cached_rate(self) -> Self:
        if (
            self.cached_input_micros_per_million_tokens is not None
            and self.cached_input_micros_per_million_tokens
            > self.input_micros_per_million_tokens
        ):
            raise ValueError("Cached input pricing cannot exceed input pricing")
        return self

    def cost_micros(self, usage: ModelUsage) -> int:
        cached_rate = (
            self.cached_input_micros_per_million_tokens
            if self.cached_input_micros_per_million_tokens is not None
            else self.input_micros_per_million_tokens
        )
        uncached_input = usage.input_tokens - usage.cached_input_tokens
        numerator = (
            uncached_input * self.input_micros_per_million_tokens
            + usage.cached_input_tokens * cached_rate
            + usage.output_tokens * self.output_micros_per_million_tokens
        )
        return _ceil_million(numerator)

    def upper_bound_cost_micros(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
    ) -> int:
        numerator = (
            input_tokens * self.input_micros_per_million_tokens
            + output_tokens * self.output_micros_per_million_tokens
        )
        return _ceil_million(numerator)


class ModelGatewayPolicy(BaseModel):
    """Process-local scheduling policy loaded from trusted application settings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role_routes: tuple[ModelRoleRoute, ...] = Field(default=(), max_length=5)
    provider_pricing: tuple[ModelProviderPricing, ...] = Field(
        default=(),
        max_length=32,
    )
    default_max_attempts: int = Field(default=1, ge=1, le=8)
    default_retry_delay_budget_seconds: float = Field(
        default=0,
        ge=0,
        le=300,
    )
    default_task_cost_budget_micros: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000_000,
    )
    retry_base_delay_seconds: float = Field(default=0.25, ge=0, le=30)
    retry_max_delay_seconds: float = Field(default=10, ge=0, le=300)
    latency_ewma_alpha: float = Field(default=0.2, gt=0, le=1)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=100)
    circuit_recovery_timeout_seconds: float = Field(default=30, gt=0, le=3_600)

    @model_validator(mode="after")
    def validate_unique_keys(self) -> Self:
        roles = [route.role for route in self.role_routes]
        if len(set(roles)) != len(roles):
            raise ValueError("Model role routes must use unique roles")
        provider_ids = [pricing.provider_id for pricing in self.provider_pricing]
        if len(set(provider_ids)) != len(provider_ids):
            raise ValueError("Model Provider pricing must use unique Provider IDs")
        if self.retry_max_delay_seconds < self.retry_base_delay_seconds:
            raise ValueError("Retry max delay cannot be lower than the base delay")
        return self

    def route_for(self, role: ModelRole) -> ModelRoleRoute | None:
        return next((route for route in self.role_routes if route.role is role), None)

    def pricing_for(self, provider_id: str) -> ModelProviderPricing | None:
        return next(
            (
                pricing
                for pricing in self.provider_pricing
                if pricing.provider_id == provider_id
            ),
            None,
        )


class ModelRoleRouteSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: ModelRole
    provider_ids: tuple[str, ...]
    strategy: ModelRouteStrategy
    configured: bool


class ModelProviderRoutingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    circuit_state: ModelCircuitState
    latency_ewma_ms: float | None = Field(default=None, ge=0)
    consecutive_failures: int = Field(ge=0)
    request_count: int = Field(ge=0)
    failure_count: int = Field(ge=0)
    retry_count: int = Field(ge=0)
    total_cost_micros: int = Field(ge=0)
    retry_after_until: datetime | None = None
    circuit_open_until: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=100)
    pricing: ModelProviderPricing | None = None

    @model_validator(mode="after")
    def validate_timestamps(self) -> Self:
        for value in (self.retry_after_until, self.circuit_open_until):
            if value is not None and value.utcoffset() is None:
                raise ValueError("Routing runtime timestamps must be timezone-aware")
        return self


class ModelGatewayRoutingSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    generated_at: datetime
    default_provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    default_max_attempts: int = Field(ge=1)
    default_retry_delay_budget_seconds: float = Field(ge=0)
    default_task_cost_budget_micros: int | None = Field(default=None, ge=0)
    latency_ewma_alpha: float = Field(gt=0, le=1)
    circuit_failure_threshold: int = Field(ge=1)
    circuit_recovery_timeout_seconds: float = Field(gt=0)
    active_task_budget_count: int = Field(ge=0)
    routes: tuple[ModelRoleRouteSnapshot, ...]
    providers: tuple[ModelProviderRoutingSnapshot, ...]

    @model_validator(mode="after")
    def validate_generated_at(self) -> Self:
        if self.generated_at.utcoffset() is None:
            raise ValueError("Routing snapshot timestamp must be timezone-aware")
        return self


def _ceil_million(numerator: int) -> int:
    return (numerator + 999_999) // 1_000_000 if numerator else 0
