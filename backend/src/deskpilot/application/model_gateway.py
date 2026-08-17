"""Capability-aware, privacy-aware routing across provider-neutral model ports."""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import RLock
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelExecutionBudget,
    ModelLocation,
    ModelProviderDescriptor,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ProviderHealth,
    ProviderHealthStatus,
    ToolCallingMode,
)
from deskpilot.domain.model_routing import (
    ModelCircuitState,
    ModelGatewayPolicy,
    ModelGatewayRoutingSnapshot,
    ModelProviderRoutingSnapshot,
    ModelRoleRouteSnapshot,
    ModelRouteStrategy,
)
from deskpilot.observability import TelemetryFacade

StructuredOutput = TypeVar("StructuredOutput", bound=BaseModel)


class ModelProvider(Protocol):
    @property
    def descriptor(self) -> ModelProviderDescriptor: ...

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]: ...

    async def health(self) -> ProviderHealth: ...


class ModelGatewayError(RuntimeError):
    code = "MODEL_GATEWAY_ERROR"
    retryable = False
    provider_id: str | None
    retry_after_seconds: float | None

    def __init__(
        self,
        message: str,
        *,
        provider_id: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_id = provider_id
        self.retry_after_seconds = retry_after_seconds


class DuplicateModelProviderError(ModelGatewayError):
    code = "MODEL_PROVIDER_ALREADY_REGISTERED"


class UnknownModelProviderError(ModelGatewayError):
    code = "MODEL_PROVIDER_NOT_FOUND"


class DisabledModelProviderError(ModelGatewayError):
    code = "MODEL_PROVIDER_DISABLED"


class ModelPrivacyRouteError(ModelGatewayError):
    code = "MODEL_PRIVACY_ROUTE_UNAVAILABLE"


class ModelCapabilityUnavailableError(ModelGatewayError):
    code = "MODEL_CAPABILITY_UNAVAILABLE"


class ModelProviderUnavailableError(ModelGatewayError):
    code = "MODEL_PROVIDER_UNAVAILABLE"
    retryable = True


class ModelAuthenticationError(ModelGatewayError):
    code = "MODEL_AUTHENTICATION_FAILED"


class ModelRateLimitError(ModelGatewayError):
    code = "MODEL_RATE_LIMITED"
    retryable = True


class ModelProviderCircuitOpenError(ModelGatewayError):
    code = "MODEL_PROVIDER_CIRCUIT_OPEN"
    retryable = True


class ModelProviderPricingRequiredError(ModelGatewayError):
    code = "MODEL_PROVIDER_PRICING_REQUIRED"


class ModelCostBudgetExceededError(ModelGatewayError):
    code = "MODEL_COST_BUDGET_EXCEEDED"


class ModelQuotaExceededError(ModelGatewayError):
    code = "MODEL_QUOTA_EXCEEDED"


class ModelRequestRejectedError(ModelGatewayError):
    code = "MODEL_REQUEST_REJECTED"


class ModelContentFilteredError(ModelGatewayError):
    code = "MODEL_CONTENT_FILTERED"


class ModelTimeoutError(ModelGatewayError):
    code = "MODEL_TIMEOUT"
    retryable = True


class ModelResponseInvalidError(ModelGatewayError):
    code = "MODEL_RESPONSE_INVALID"


class ModelStreamInvalidError(ModelGatewayError):
    code = "MODEL_STREAM_INVALID"


@dataclass
class _ProviderRuntime:
    circuit_state: ModelCircuitState = ModelCircuitState.CLOSED
    circuit_open_until: float | None = None
    half_open_in_flight: bool = False
    retry_after_until: float | None = None
    latency_ewma_ms: float | None = None
    consecutive_failures: int = 0
    request_count: int = 0
    failure_count: int = 0
    retry_count: int = 0
    total_cost_micros: int = 0
    last_error_code: str | None = None


@dataclass
class _TaskCostRuntime:
    spent_micros: int = 0
    reserved_micros: int = 0


@dataclass(frozen=True)
class _CostReservation:
    task_id: str
    provider_id: str
    reserved_micros: int
    limit_micros: int | None


@dataclass(frozen=True)
class _EffectiveBudget:
    max_attempts: int
    max_retry_delay_seconds: float
    max_task_cost_micros: int | None


class ModelGateway:
    def __init__(
        self,
        *,
        default_provider_id: str,
        policy: ModelGatewayPolicy | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        telemetry: TelemetryFacade | None = None,
    ) -> None:
        self._default_provider_id = default_provider_id
        self._policy = policy or ModelGatewayPolicy()
        self._monotonic = monotonic
        self._sleep = sleep
        self._providers: dict[str, ModelProvider] = {}
        self._runtime: dict[str, _ProviderRuntime] = {}
        self._task_costs: dict[str, _TaskCostRuntime] = {}
        self._state_lock = RLock()
        self._telemetry = telemetry

    @property
    def policy(self) -> ModelGatewayPolicy:
        return self._policy

    def register(self, provider: ModelProvider) -> None:
        provider_id = provider.descriptor.provider_id
        if provider_id in self._providers:
            raise DuplicateModelProviderError(
                f"Model provider is already registered: {provider_id}",
                provider_id=provider_id,
            )
        self._providers[provider_id] = provider
        self._runtime.setdefault(provider_id, _ProviderRuntime())

    def reconfigure(
        self,
        providers: tuple[ModelProvider, ...],
        *,
        default_provider_id: str,
    ) -> None:
        """Atomically replace the registry after a candidate set is validated."""
        by_id: dict[str, ModelProvider] = {}
        for provider in providers:
            provider_id = provider.descriptor.provider_id
            if provider_id in by_id:
                raise DuplicateModelProviderError(
                    f"Model provider is already registered: {provider_id}",
                    provider_id=provider_id,
                )
            by_id[provider_id] = provider
        if default_provider_id not in by_id:
            raise UnknownModelProviderError(
                f"Model provider is not registered: {default_provider_id}",
                provider_id=default_provider_id,
            )
        with self._state_lock:
            self._providers = by_id
            self._runtime = {
                provider_id: self._runtime.get(provider_id, _ProviderRuntime())
                for provider_id in by_id
            }
            self._default_provider_id = default_provider_id

    def descriptors(self) -> tuple[ModelProviderDescriptor, ...]:
        return tuple(
            self._providers[provider_id].descriptor
            for provider_id in sorted(self._providers)
        )

    def validate_configuration(self) -> None:
        self._resolve(self._default_provider_id)

    def default_descriptor(self) -> ModelProviderDescriptor:
        return self._resolve(self._default_provider_id).descriptor

    def descriptor(self, provider_id: str) -> ModelProviderDescriptor:
        return self._resolve(provider_id).descriptor

    def select_provider(self, request: ModelRequest) -> ModelProvider:
        return self._select_provider(
            request,
            excluded=set(),
            claim_half_open=False,
            budget=self._effective_budget(request.execution_budget),
        )

    async def complete(self, request: ModelRequest) -> ModelResponse:
        budget = self._effective_budget(request.execution_budget)
        deadline = self._monotonic() + request.timeout_seconds
        attempted: set[str] = set()
        retry_delay_spent = 0.0
        last_error: ModelGatewayError | None = None

        for attempt_number in range(1, budget.max_attempts + 1):
            try:
                provider = self._select_provider(
                    request,
                    excluded=attempted,
                    claim_half_open=True,
                    budget=budget,
                )
            except (ModelProviderCircuitOpenError, ModelRateLimitError):
                if last_error is None:
                    raise
                delay = self._retry_delay(
                    attempt_number=attempt_number - 1,
                    error=last_error,
                    request=request,
                )
                remaining_delay_budget = (
                    budget.max_retry_delay_seconds - retry_delay_spent
                )
                remaining_timeout = deadline - self._monotonic()
                delay = max(delay, self._next_route_availability_delay(request))
                if delay > remaining_delay_budget or delay >= remaining_timeout:
                    raise last_error from None
                await self._sleep(delay)
                retry_delay_spent += delay
                attempted.clear()
                provider = self._select_provider(
                    request,
                    excluded=attempted,
                    claim_half_open=True,
                    budget=budget,
                )

            provider_id = provider.descriptor.provider_id
            attempted.add(provider_id)
            reservation = self._reserve_cost(request, provider_id, budget)
            try:
                remaining_timeout = deadline - self._monotonic()
                if remaining_timeout <= 0:
                    raise ModelTimeoutError(
                        "Model request exhausted its total timeout",
                        provider_id=provider_id,
                    )
                async with asyncio.timeout(remaining_timeout):
                    response = await self._complete_provider_attempt(
                        provider,
                        request,
                        attempt_number,
                    )
                self._validate_response(request, provider.descriptor, response)
            except TimeoutError as error:
                mapped_timeout = ModelTimeoutError(
                    "Model provider exceeded the request timeout",
                    provider_id=provider_id,
                )
                self._release_cost(reservation)
                self._record_failure(provider_id, mapped_timeout)
                last_error = mapped_timeout
                cause: BaseException = error
            except asyncio.CancelledError:
                self._release_cost(reservation)
                self._release_half_open(provider_id)
                raise
            except ModelGatewayError as error:
                self._release_cost(reservation)
                self._record_failure(provider_id, error)
                last_error = error
                cause = error
            except Exception as error:
                mapped_unavailable = ModelProviderUnavailableError(
                    f"Model provider call failed: {type(error).__name__}",
                    provider_id=provider_id,
                )
                self._release_cost(reservation)
                self._record_failure(provider_id, mapped_unavailable)
                last_error = mapped_unavailable
                cause = error
            else:
                cost_micros = self._settle_cost(reservation, response)
                self._record_success(
                    provider_id,
                    latency_ms=response.latency_ms,
                    cost_micros=cost_micros,
                )
                return response

            if last_error is None:
                raise RuntimeError("Model Gateway lost the mapped Provider error")
            if not last_error.retryable or attempt_number >= budget.max_attempts:
                raise last_error from cause

            self._record_retry(provider_id)
            if self._has_candidate(request, excluded=attempted, budget=budget):
                continue

            delay = self._retry_delay(
                attempt_number=attempt_number,
                error=last_error,
                request=request,
            )
            delay = max(delay, self._next_route_availability_delay(request))
            remaining_delay_budget = budget.max_retry_delay_seconds - retry_delay_spent
            remaining_timeout = deadline - self._monotonic()
            if delay > remaining_delay_budget or delay >= remaining_timeout:
                raise last_error from cause
            await self._sleep(delay)
            retry_delay_spent += delay
            attempted.clear()

        if last_error is not None:
            raise last_error
        raise RuntimeError("Model Gateway exhausted attempts without a result")

    async def _complete_provider_attempt(
        self,
        provider: ModelProvider,
        request: ModelRequest,
        attempt_number: int,
    ) -> ModelResponse:
        telemetry = self._telemetry
        if telemetry is None:
            return await provider.complete(request)
        descriptor = provider.descriptor
        with telemetry.operation(
            "deskpilot.model.dispatch",
            "model",
            {
                "deskpilot.subject.type": "model_request",
                "deskpilot.subject.id": request.request_id,
                "deskpilot.model.provider_class": descriptor.location.value,
                "deskpilot.model.protocol": descriptor.protocol.value,
                "deskpilot.attempt.ordinal": attempt_number,
            },
        ) as operation:
            response = await provider.complete(request)
            operation.set_outcome("succeeded")
            return response

    async def complete_structured(
        self,
        request: ModelRequest,
        output_model: type[StructuredOutput],
    ) -> tuple[StructuredOutput, ModelResponse]:
        response = await self.complete(request)
        if response.structured_output is None:
            raise ModelResponseInvalidError(
                "Model provider did not return required structured output",
                provider_id=response.provider_id,
            )
        try:
            parsed = output_model.model_validate(response.structured_output)
        except ValidationError as error:
            raise ModelResponseInvalidError(
                "Model structured output failed application Schema validation",
                provider_id=response.provider_id,
            ) from error
        return parsed, response

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        budget = self._effective_budget(request.execution_budget)
        provider = self._select_provider(
            request,
            excluded=set(),
            claim_half_open=True,
            budget=budget,
        )
        provider_id = provider.descriptor.provider_id
        if not provider.descriptor.capabilities.streaming:
            self._release_half_open(provider_id)
            raise ModelCapabilityUnavailableError(
                "Selected model provider does not support streaming",
                provider_id=provider_id,
            )
        reservation = self._reserve_cost(request, provider_id, budget)
        expected_sequence = 0
        completed = False
        completed_response: ModelResponse | None = None
        try:
            async with asyncio.timeout(request.timeout_seconds):
                async for event in provider.stream(request):
                    if event.request_id != request.request_id:
                        raise ModelStreamInvalidError(
                            "Model stream request_id does not match the request",
                            provider_id=provider_id,
                        )
                    if completed:
                        raise ModelStreamInvalidError(
                            "Model stream emitted events after response.completed",
                            provider_id=provider_id,
                        )
                    if event.provider_id != provider_id:
                        raise ModelStreamInvalidError(
                            "Model stream provider_id changed",
                            provider_id=provider_id,
                        )
                    if event.sequence != expected_sequence:
                        raise ModelStreamInvalidError(
                            "Model stream sequence is not contiguous",
                            provider_id=provider_id,
                        )
                    expected_sequence += 1
                    if event.type is ModelStreamEventType.RESPONSE_COMPLETED:
                        if event.response is None:
                            raise ModelStreamInvalidError(
                                "Completed model event has no response",
                                provider_id=provider_id,
                            )
                        self._validate_response(
                            request, provider.descriptor, event.response
                        )
                        completed = True
                        completed_response = event.response
                    yield event
        except TimeoutError as error:
            mapped_timeout = ModelTimeoutError(
                "Model stream exceeded the request timeout",
                provider_id=provider_id,
            )
            self._release_cost(reservation)
            self._record_failure(provider_id, mapped_timeout)
            raise mapped_timeout from error
        except asyncio.CancelledError:
            self._release_cost(reservation)
            self._release_half_open(provider_id)
            raise
        except ModelGatewayError as error:
            self._release_cost(reservation)
            self._record_failure(provider_id, error)
            raise
        except Exception as error:
            mapped_unavailable = ModelProviderUnavailableError(
                f"Model stream failed: {type(error).__name__}",
                provider_id=provider_id,
            )
            self._release_cost(reservation)
            self._record_failure(provider_id, mapped_unavailable)
            raise mapped_unavailable from error
        if not completed:
            mapped_invalid = ModelStreamInvalidError(
                "Model stream ended without response.completed",
                provider_id=provider_id,
            )
            self._release_cost(reservation)
            self._record_failure(provider_id, mapped_invalid)
            raise mapped_invalid
        if completed_response is None:
            self._release_cost(reservation)
            raise RuntimeError("Completed model stream lost its response")
        cost_micros = self._settle_cost(reservation, completed_response)
        self._record_success(
            provider_id,
            latency_ms=completed_response.latency_ms,
            cost_micros=cost_micros,
        )

    def routing_snapshot(self) -> ModelGatewayRoutingSnapshot:
        now = self._monotonic()
        generated_at = datetime.now(UTC)
        with self._state_lock:
            default_ids = self._default_ordered_provider_ids()
            routes: list[ModelRoleRouteSnapshot] = []
            for role in ModelRole:
                configured = self._policy.route_for(role)
                routes.append(
                    ModelRoleRouteSnapshot(
                        role=role,
                        provider_ids=(
                            configured.provider_ids
                            if configured is not None
                            else default_ids
                        ),
                        strategy=(
                            configured.strategy
                            if configured is not None
                            else ModelRouteStrategy.PRIORITY
                        ),
                        configured=configured is not None,
                    )
                )

            providers: list[ModelProviderRoutingSnapshot] = []
            for provider_id in sorted(self._providers):
                runtime = self._runtime[provider_id]
                circuit_state = runtime.circuit_state
                if (
                    circuit_state is ModelCircuitState.OPEN
                    and runtime.circuit_open_until is not None
                    and runtime.circuit_open_until <= now
                ):
                    circuit_state = ModelCircuitState.HALF_OPEN
                providers.append(
                    ModelProviderRoutingSnapshot(
                        provider_id=provider_id,
                        circuit_state=circuit_state,
                        latency_ewma_ms=runtime.latency_ewma_ms,
                        consecutive_failures=runtime.consecutive_failures,
                        request_count=runtime.request_count,
                        failure_count=runtime.failure_count,
                        retry_count=runtime.retry_count,
                        total_cost_micros=runtime.total_cost_micros,
                        retry_after_until=self._future_wall_time(
                            runtime.retry_after_until,
                            now=now,
                            wall=generated_at,
                        ),
                        circuit_open_until=self._future_wall_time(
                            runtime.circuit_open_until,
                            now=now,
                            wall=generated_at,
                        ),
                        last_error_code=runtime.last_error_code,
                        pricing=self._policy.pricing_for(provider_id),
                    )
                )

            return ModelGatewayRoutingSnapshot(
                generated_at=generated_at,
                default_provider_id=self._default_provider_id,
                default_max_attempts=self._policy.default_max_attempts,
                default_retry_delay_budget_seconds=(
                    self._policy.default_retry_delay_budget_seconds
                ),
                default_task_cost_budget_micros=(
                    self._policy.default_task_cost_budget_micros
                ),
                latency_ewma_alpha=self._policy.latency_ewma_alpha,
                circuit_failure_threshold=self._policy.circuit_failure_threshold,
                circuit_recovery_timeout_seconds=(
                    self._policy.circuit_recovery_timeout_seconds
                ),
                active_task_budget_count=len(self._task_costs),
                routes=tuple(routes),
                providers=tuple(providers),
            )

    def forget_task_budget(self, task_id: str) -> None:
        with self._state_lock:
            self._task_costs.pop(task_id, None)

    def _effective_budget(self, budget: ModelExecutionBudget) -> _EffectiveBudget:
        return _EffectiveBudget(
            max_attempts=(
                budget.max_attempts
                if budget.max_attempts is not None
                else self._policy.default_max_attempts
            ),
            max_retry_delay_seconds=(
                budget.max_retry_delay_seconds
                if budget.max_retry_delay_seconds is not None
                else self._policy.default_retry_delay_budget_seconds
            ),
            max_task_cost_micros=(
                budget.max_task_cost_micros
                if budget.max_task_cost_micros is not None
                else self._policy.default_task_cost_budget_micros
            ),
        )

    def _select_provider(
        self,
        request: ModelRequest,
        *,
        excluded: set[str],
        claim_half_open: bool,
        budget: _EffectiveBudget,
    ) -> ModelProvider:
        self.validate_configuration()
        now = self._monotonic()
        with self._state_lock:
            candidates = self._base_candidates(request)
            privacy_candidates = [
                provider
                for provider in candidates
                if self._privacy_allows(provider.descriptor, request)
            ]
            if not privacy_candidates:
                raise ModelPrivacyRouteError(
                    "No approved model route satisfies the request privacy mode",
                    provider_id=request.provider_hint,
                )

            capable = [
                provider
                for provider in privacy_candidates
                if self._supports(provider.descriptor, request.requirements)
            ]
            if not capable:
                raise ModelCapabilityUnavailableError(
                    "No model provider satisfies the requested capabilities",
                    provider_id=request.provider_hint,
                )

            budget_candidates = self._budget_candidates(
                request,
                capable,
                budget=budget,
            )

            unexcluded = [
                provider
                for provider in budget_candidates
                if provider.descriptor.provider_id not in excluded
            ]
            ordered = self._sort_candidates(unexcluded, request)
            available = [
                provider
                for provider in ordered
                if self._provider_is_available(
                    provider.descriptor.provider_id,
                    now=now,
                    claim=False,
                )
            ]
            if not available:
                if unexcluded:
                    raise self._route_temporarily_unavailable(unexcluded, now=now)
                raise ModelProviderUnavailableError(
                    "No untried Provider remains in the model route",
                    provider_id=request.provider_hint,
                )

            provider = available[0]
            if claim_half_open:
                provider_id = provider.descriptor.provider_id
                if not self._provider_is_available(
                    provider_id,
                    now=now,
                    claim=True,
                ):
                    raise self._route_temporarily_unavailable(available, now=now)
            return provider

    def _base_candidates(self, request: ModelRequest) -> list[ModelProvider]:
        if request.provider_hint is not None:
            return [self._resolve(request.provider_hint)]
        route = self._policy.route_for(request.role)
        provider_ids = (
            route.provider_ids
            if route is not None
            else self._default_ordered_provider_ids()
        )
        candidates = [
            self._providers[provider_id]
            for provider_id in provider_ids
            if provider_id in self._providers
        ]
        if not candidates:
            raise ModelCapabilityUnavailableError(
                f"No registered Provider is available for role {request.role.value}"
            )
        return candidates

    def _default_ordered_provider_ids(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                self._providers,
                key=lambda provider_id: (
                    provider_id != self._default_provider_id,
                    provider_id,
                ),
            )
        )

    def _budget_candidates(
        self,
        request: ModelRequest,
        providers: list[ModelProvider],
        *,
        budget: _EffectiveBudget,
    ) -> list[ModelProvider]:
        limit = budget.max_task_cost_micros
        if limit is None:
            return providers
        task = self._task_costs.get(request.task_id, _TaskCostRuntime())
        affordable: list[ModelProvider] = []
        missing_pricing = False
        for provider in providers:
            provider_id = provider.descriptor.provider_id
            pricing = self._policy.pricing_for(provider_id)
            if pricing is None:
                missing_pricing = True
                continue
            estimated_cost = self._estimated_upper_cost(request, provider_id)
            if (
                task.spent_micros
                + task.reserved_micros
                + estimated_cost
                <= limit
            ):
                affordable.append(provider)
        if affordable:
            return affordable
        error_provider_id = (
            providers[0].descriptor.provider_id if len(providers) == 1 else None
        )
        if missing_pricing:
            raise ModelProviderPricingRequiredError(
                "A finite model cost budget requires configured Provider pricing",
                provider_id=error_provider_id,
            )
        raise ModelCostBudgetExceededError(
            "No eligible model Provider fits the remaining task cost budget",
            provider_id=error_provider_id,
        )

    def _sort_candidates(
        self,
        candidates: list[ModelProvider],
        request: ModelRequest,
    ) -> list[ModelProvider]:
        route = self._policy.route_for(request.role)
        position = {
            provider.descriptor.provider_id: index
            for index, provider in enumerate(candidates)
        }

        def location_key(provider: ModelProvider) -> bool:
            return bool(
                request.privacy_mode == "local_preferred"
                and request.cloud_fallback_approved
                and provider.descriptor.location is not ModelLocation.LOCAL
            )

        if route is not None and route.strategy is ModelRouteStrategy.LATENCY_AWARE:
            return sorted(
                candidates,
                key=lambda provider: (
                    location_key(provider),
                    self._runtime[provider.descriptor.provider_id].latency_ewma_ms
                    is not None,
                    self._runtime[provider.descriptor.provider_id].latency_ewma_ms
                    or 0,
                    position[provider.descriptor.provider_id],
                ),
            )
        return sorted(
            candidates,
            key=lambda provider: (
                location_key(provider),
                position[provider.descriptor.provider_id],
            ),
        )

    def _provider_is_available(
        self,
        provider_id: str,
        *,
        now: float,
        claim: bool,
    ) -> bool:
        runtime = self._runtime[provider_id]
        if runtime.retry_after_until is not None:
            if runtime.retry_after_until > now:
                return False
            runtime.retry_after_until = None
        if runtime.circuit_state is ModelCircuitState.OPEN:
            if (
                runtime.circuit_open_until is not None
                and runtime.circuit_open_until > now
            ):
                return False
            if runtime.half_open_in_flight:
                return False
            if claim:
                runtime.circuit_state = ModelCircuitState.HALF_OPEN
                runtime.half_open_in_flight = True
            return True
        if runtime.circuit_state is ModelCircuitState.HALF_OPEN:
            if runtime.half_open_in_flight:
                return False
            if claim:
                runtime.half_open_in_flight = True
            return True
        return True

    def _route_temporarily_unavailable(
        self,
        providers: list[ModelProvider],
        *,
        now: float,
    ) -> ModelGatewayError:
        provider_ids = [provider.descriptor.provider_id for provider in providers]
        retry_delays: list[float] = []
        for candidate_id in provider_ids:
            retry_after_until = self._runtime[candidate_id].retry_after_until
            if retry_after_until is not None and retry_after_until > now:
                retry_delays.append(retry_after_until - now)
        provider_id = provider_ids[0] if len(provider_ids) == 1 else None
        if retry_delays:
            return ModelRateLimitError(
                "All eligible model routes are honoring Retry-After",
                provider_id=provider_id,
                retry_after_seconds=min(retry_delays),
            )
        circuit_delays: list[float] = []
        for candidate_id in provider_ids:
            circuit_open_until = self._runtime[candidate_id].circuit_open_until
            if circuit_open_until is not None and circuit_open_until > now:
                circuit_delays.append(circuit_open_until - now)
        return ModelProviderCircuitOpenError(
            "All eligible model Provider circuits are open",
            provider_id=provider_id,
            retry_after_seconds=min(circuit_delays) if circuit_delays else None,
        )

    def _has_candidate(
        self,
        request: ModelRequest,
        *,
        excluded: set[str],
        budget: _EffectiveBudget,
    ) -> bool:
        try:
            self._select_provider(
                request,
                excluded=excluded,
                claim_half_open=False,
                budget=budget,
            )
        except ModelGatewayError:
            return False
        return True

    def _next_route_availability_delay(self, request: ModelRequest) -> float:
        now = self._monotonic()
        with self._state_lock:
            delays: list[float] = []
            for provider in self._base_candidates(request):
                runtime = self._runtime[provider.descriptor.provider_id]
                for deadline in (
                    runtime.retry_after_until,
                    runtime.circuit_open_until,
                ):
                    if deadline is not None and deadline > now:
                        delays.append(deadline - now)
                if (
                    runtime.circuit_state is ModelCircuitState.HALF_OPEN
                    and runtime.half_open_in_flight
                ):
                    delays.append(0.05)
            return min(delays, default=0)

    def _retry_delay(
        self,
        *,
        attempt_number: int,
        error: ModelGatewayError,
        request: ModelRequest,
    ) -> float:
        del request
        exponential = min(
            self._policy.retry_max_delay_seconds,
            self._policy.retry_base_delay_seconds
            * (2 ** max(0, attempt_number - 1)),
        )
        retry_after = error.retry_after_seconds or 0.0
        return max(float(exponential), retry_after)

    def _reserve_cost(
        self,
        request: ModelRequest,
        provider_id: str,
        budget: _EffectiveBudget,
    ) -> _CostReservation:
        pricing = self._policy.pricing_for(provider_id)
        if budget.max_task_cost_micros is not None and pricing is None:
            self._release_half_open(provider_id)
            raise ModelProviderPricingRequiredError(
                "A finite model cost budget requires configured Provider pricing",
                provider_id=provider_id,
            )
        estimated_cost = self._estimated_upper_cost(request, provider_id)
        reservation = _CostReservation(
            task_id=request.task_id,
            provider_id=provider_id,
            reserved_micros=estimated_cost,
            limit_micros=budget.max_task_cost_micros,
        )
        if budget.max_task_cost_micros is None:
            return reservation
        with self._state_lock:
            task = self._task_costs.setdefault(request.task_id, _TaskCostRuntime())
            projected = task.spent_micros + task.reserved_micros + estimated_cost
            if projected > budget.max_task_cost_micros:
                self._release_half_open(provider_id)
                raise ModelCostBudgetExceededError(
                    "Model request would exceed the task cost budget",
                    provider_id=provider_id,
                )
            task.reserved_micros += estimated_cost
        return reservation

    def _estimated_upper_cost(self, request: ModelRequest, provider_id: str) -> int:
        pricing = self._policy.pricing_for(provider_id)
        if pricing is None:
            return 0
        return pricing.upper_bound_cost_micros(
            input_tokens=self._input_token_upper_bound(request),
            output_tokens=request.max_output_tokens,
        )

    def _release_cost(self, reservation: _CostReservation) -> None:
        if reservation.limit_micros is None:
            return
        with self._state_lock:
            task = self._task_costs.get(reservation.task_id)
            if task is not None:
                task.reserved_micros = max(
                    0,
                    task.reserved_micros - reservation.reserved_micros,
                )

    def _settle_cost(
        self,
        reservation: _CostReservation,
        response: ModelResponse,
    ) -> int:
        pricing = self._policy.pricing_for(reservation.provider_id)
        cost_micros = pricing.cost_micros(response.usage) if pricing is not None else 0
        if reservation.limit_micros is not None:
            with self._state_lock:
                task = self._task_costs[reservation.task_id]
                task.reserved_micros = max(
                    0,
                    task.reserved_micros - reservation.reserved_micros,
                )
                task.spent_micros += cost_micros
        return cost_micros

    def _record_success(
        self,
        provider_id: str,
        *,
        latency_ms: int,
        cost_micros: int,
    ) -> None:
        with self._state_lock:
            runtime = self._runtime[provider_id]
            runtime.request_count += 1
            runtime.total_cost_micros += cost_micros
            runtime.consecutive_failures = 0
            runtime.last_error_code = None
            runtime.retry_after_until = None
            runtime.circuit_state = ModelCircuitState.CLOSED
            runtime.circuit_open_until = None
            runtime.half_open_in_flight = False
            runtime.latency_ewma_ms = (
                float(latency_ms)
                if runtime.latency_ewma_ms is None
                else (
                    self._policy.latency_ewma_alpha * latency_ms
                    + (1 - self._policy.latency_ewma_alpha)
                    * runtime.latency_ewma_ms
                )
            )

    def _record_failure(
        self,
        provider_id: str,
        error: ModelGatewayError,
    ) -> None:
        now = self._monotonic()
        with self._state_lock:
            runtime = self._runtime[provider_id]
            runtime.request_count += 1
            runtime.failure_count += 1
            runtime.last_error_code = error.code
            if isinstance(error, ModelRateLimitError) or (
                error.retry_after_seconds is not None
            ):
                cooldown = (
                    error.retry_after_seconds
                    if error.retry_after_seconds is not None
                    else self._policy.retry_base_delay_seconds
                )
                runtime.retry_after_until = now + cooldown

            circuit_failure = isinstance(
                error,
                (
                    ModelProviderUnavailableError,
                    ModelTimeoutError,
                    ModelResponseInvalidError,
                    ModelStreamInvalidError,
                ),
            )
            if circuit_failure:
                runtime.consecutive_failures += 1
            if (
                runtime.circuit_state is ModelCircuitState.HALF_OPEN
                or runtime.consecutive_failures
                >= self._policy.circuit_failure_threshold
            ):
                runtime.circuit_state = ModelCircuitState.OPEN
                runtime.circuit_open_until = (
                    now + self._policy.circuit_recovery_timeout_seconds
                )
            runtime.half_open_in_flight = False

    def _record_retry(self, provider_id: str) -> None:
        with self._state_lock:
            self._runtime[provider_id].retry_count += 1

    def _release_half_open(self, provider_id: str) -> None:
        with self._state_lock:
            runtime = self._runtime.get(provider_id)
            if runtime is not None:
                runtime.half_open_in_flight = False

    @staticmethod
    def _input_token_upper_bound(request: ModelRequest) -> int:
        byte_count = 32
        for message in request.messages:
            byte_count += len(message.content.encode("utf-8")) + 16
            if message.name is not None:
                byte_count += len(message.name.encode("utf-8"))
        if request.output_schema is not None:
            byte_count += len(
                json.dumps(
                    request.output_schema.model_dump(mode="json"),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            )
        return max(1, byte_count)

    @staticmethod
    def _privacy_allows(
        descriptor: ModelProviderDescriptor,
        request: ModelRequest,
    ) -> bool:
        return not (
            (
                request.privacy_mode == "local_only"
                or (
                    request.privacy_mode == "local_preferred"
                    and not request.cloud_fallback_approved
                )
            )
            and descriptor.location is not ModelLocation.LOCAL
        )

    @staticmethod
    def _future_wall_time(
        deadline: float | None,
        *,
        now: float,
        wall: datetime,
    ) -> datetime | None:
        if deadline is None or deadline <= now:
            return None
        return wall + timedelta(seconds=deadline - now)

    async def health(self) -> tuple[ProviderHealth, ...]:
        provider_ids = tuple(sorted(self._providers))
        return tuple(
            [
                await self.check_health(provider_id)
                for provider_id in provider_ids
            ]
        )

    async def check_health(
        self,
        provider_id: str,
        *,
        timeout_seconds: float = 5,
    ) -> ProviderHealth:
        provider = self._resolve(provider_id)
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await provider.health()
            if result.provider_id != provider_id:
                raise ValueError("Provider health identity does not match")
            return result
        except asyncio.CancelledError:
            raise
        except Exception as error:
            return ProviderHealth(
                provider_id=provider_id,
                status=ProviderHealthStatus.UNAVAILABLE,
                detail=f"Provider health check failed: {type(error).__name__}",
            )

    def _resolve(self, provider_id: str) -> ModelProvider:
        try:
            return self._providers[provider_id]
        except KeyError as error:
            raise UnknownModelProviderError(
                f"Model provider is not registered: {provider_id}",
                provider_id=provider_id,
            ) from error

    @staticmethod
    def _validate_privacy(
        descriptor: ModelProviderDescriptor,
        request: ModelRequest,
    ) -> None:
        if (
            (
                request.privacy_mode == "local_only"
                or (
                    request.privacy_mode == "local_preferred"
                    and not request.cloud_fallback_approved
                )
            )
            and descriptor.location is not ModelLocation.LOCAL
        ):
            raise ModelPrivacyRouteError(
                "Request cannot use a cloud provider without an approved route",
                provider_id=descriptor.provider_id,
            )

    @classmethod
    def _validate_capabilities(
        cls,
        descriptor: ModelProviderDescriptor,
        requirements: ModelCapabilityRequirements,
    ) -> None:
        if not cls._supports(descriptor, requirements):
            raise ModelCapabilityUnavailableError(
                "Model provider does not satisfy the requested capabilities",
                provider_id=descriptor.provider_id,
            )

    @staticmethod
    def _supports(
        descriptor: ModelProviderDescriptor,
        requirements: ModelCapabilityRequirements,
    ) -> bool:
        capabilities = descriptor.capabilities
        return not (
            (requirements.streaming and not capabilities.streaming)
            or (requirements.structured_output and not capabilities.structured_output)
            or (requirements.strict_json_schema and not capabilities.strict_json_schema)
            or (
                requirements.tool_calling
                and capabilities.tool_calling is ToolCallingMode.NONE
            )
            or (
                requirements.parallel_tool_calls
                and not capabilities.parallel_tool_calls
            )
            or (requirements.vision and not capabilities.vision)
            or capabilities.max_context_tokens < requirements.min_context_tokens
        )

    @staticmethod
    def _validate_response(
        request: ModelRequest,
        descriptor: ModelProviderDescriptor,
        response: ModelResponse,
    ) -> None:
        if response.request_id != request.request_id:
            raise ModelResponseInvalidError(
                "Model response request_id does not match the request",
                provider_id=descriptor.provider_id,
            )
        if response.provider_id != descriptor.provider_id:
            raise ModelResponseInvalidError(
                "Model response provider_id does not match the selected provider",
                provider_id=descriptor.provider_id,
            )
        if response.model != descriptor.model:
            raise ModelResponseInvalidError(
                "Model response model does not match the provider descriptor",
                provider_id=descriptor.provider_id,
            )
        if response.usage.output_tokens > request.max_output_tokens:
            raise ModelResponseInvalidError(
                "Model response usage exceeded max_output_tokens",
                provider_id=descriptor.provider_id,
            )
        if request.output_schema is not None and response.structured_output is None:
            raise ModelResponseInvalidError(
                "Structured model request returned no structured output",
                provider_id=descriptor.provider_id,
            )
