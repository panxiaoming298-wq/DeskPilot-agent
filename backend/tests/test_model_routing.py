import asyncio
from collections.abc import AsyncIterator

import pytest
from fastapi.testclient import TestClient

from deskpilot.application.model_gateway import (
    ModelCostBudgetExceededError,
    ModelGateway,
    ModelProviderCircuitOpenError,
    ModelProviderPricingRequiredError,
    ModelProviderUnavailableError,
    ModelRateLimitError,
)
from deskpilot.domain.model_contracts import (
    ModelExecutionBudget,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamEvent,
    ModelUsage,
)
from deskpilot.domain.model_routing import (
    ModelCircuitState,
    ModelGatewayPolicy,
    ModelProviderPricing,
    ModelRoleRoute,
    ModelRouteStrategy,
)
from deskpilot.model_providers.fake import FakeModelProvider


class VirtualClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, delay: float) -> None:
        self.sleeps.append(delay)
        self.now += delay


class ScriptedProvider(FakeModelProvider):
    def __init__(
        self,
        provider_id: str,
        outcomes: list[Exception | int],
        *,
        output_tokens: int = 2,
    ) -> None:
        super().__init__(provider_id=provider_id)
        self.outcomes = outcomes
        self.output_tokens = output_tokens
        self.calls = 0

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if not self.outcomes:
            raise AssertionError("Scripted Provider has no remaining outcome")
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        response = await super().complete(request)
        usage = ModelUsage(
            input_tokens=1,
            output_tokens=self.output_tokens,
            total_tokens=1 + self.output_tokens,
        )
        return response.model_copy(
            update={"latency_ms": outcome, "usage": usage}
        )

    def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        return super().stream(request)


class HalfOpenProbeProvider(FakeModelProvider):
    def __init__(self) -> None:
        super().__init__(provider_id="half-open-local")
        self.calls = 0
        self.probe_started = asyncio.Event()
        self.release_probe = asyncio.Event()

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        if self.calls == 1:
            raise ModelProviderUnavailableError(
                "initial failure",
                provider_id=self.descriptor.provider_id,
            )
        self.probe_started.set()
        await self.release_probe.wait()
        response = await super().complete(request)
        return response.model_copy(
            update={
                "usage": ModelUsage(
                    input_tokens=1,
                    output_tokens=2,
                    total_tokens=3,
                )
            }
        )


def make_request(
    *,
    role: ModelRole = ModelRole.INTENT,
    task_id: str = "routing-task",
    execution_budget: ModelExecutionBudget | None = None,
) -> ModelRequest:
    return ModelRequest(
        request_id=f"request-{role.value}",
        task_id=task_id,
        role=role,
        messages=(ModelMessage(role="user", content="x"),),
        privacy_mode="balanced",
        max_output_tokens=5,
        timeout_seconds=30,
        execution_budget=execution_budget or ModelExecutionBudget(),
    )


def gateway_with(
    providers: tuple[ScriptedProvider, ...],
    policy: ModelGatewayPolicy,
    clock: VirtualClock | None = None,
) -> ModelGateway:
    virtual_clock = clock or VirtualClock()
    gateway = ModelGateway(
        default_provider_id=providers[0].descriptor.provider_id,
        policy=policy,
        monotonic=virtual_clock.monotonic,
        sleep=virtual_clock.sleep,
    )
    for provider in providers:
        gateway.register(provider)
    return gateway


def test_role_routes_select_independent_provider_allowlists() -> None:
    intent = ScriptedProvider("intent-local", [10])
    planner = ScriptedProvider("planner-local", [10])
    gateway = gateway_with(
        (intent, planner),
        ModelGatewayPolicy(
            role_routes=(
                ModelRoleRoute(
                    role=ModelRole.INTENT,
                    provider_ids=("intent-local",),
                ),
                ModelRoleRoute(
                    role=ModelRole.PLANNER,
                    provider_ids=("planner-local",),
                ),
            )
        ),
    )

    assert gateway.select_provider(make_request()).descriptor.provider_id == "intent-local"
    assert (
        gateway.select_provider(make_request(role=ModelRole.PLANNER))
        .descriptor.provider_id
        == "planner-local"
    )


@pytest.mark.asyncio
async def test_latency_aware_route_samples_unknown_then_uses_lowest_ewma() -> None:
    slow = ScriptedProvider("slow-local", [100])
    fast = ScriptedProvider("fast-local", [20, 30])
    gateway = gateway_with(
        (slow, fast),
        ModelGatewayPolicy(
            latency_ewma_alpha=0.5,
            role_routes=(
                ModelRoleRoute(
                    role=ModelRole.INTENT,
                    provider_ids=("slow-local", "fast-local"),
                    strategy=ModelRouteStrategy.LATENCY_AWARE,
                ),
            ),
        ),
    )

    responses = [await gateway.complete(make_request()) for _ in range(3)]
    snapshot = gateway.routing_snapshot()
    by_id = {provider.provider_id: provider for provider in snapshot.providers}

    assert [response.provider_id for response in responses] == [
        "slow-local",
        "fast-local",
        "fast-local",
    ]
    assert by_id["slow-local"].latency_ewma_ms == 100
    assert by_id["fast-local"].latency_ewma_ms == 25


@pytest.mark.asyncio
async def test_retry_after_cools_down_primary_and_immediately_uses_fallback() -> None:
    clock = VirtualClock()
    primary = ScriptedProvider(
        "primary-local",
        [
            ModelRateLimitError(
                "rate limited",
                provider_id="primary-local",
                retry_after_seconds=5,
            )
        ],
    )
    fallback = ScriptedProvider("fallback-local", [12])
    gateway = gateway_with(
        (primary, fallback),
        ModelGatewayPolicy(
            default_max_attempts=2,
            default_retry_delay_budget_seconds=5,
            role_routes=(
                ModelRoleRoute(
                    role=ModelRole.INTENT,
                    provider_ids=("primary-local", "fallback-local"),
                ),
            ),
        ),
        clock,
    )

    response = await gateway.complete(make_request())
    runtime = {
        provider.provider_id: provider
        for provider in gateway.routing_snapshot().providers
    }

    assert response.provider_id == "fallback-local"
    assert clock.sleeps == []
    assert runtime["primary-local"].retry_count == 1
    assert runtime["primary-local"].retry_after_until is not None


@pytest.mark.asyncio
async def test_retry_after_waits_within_budget_when_no_fallback_exists() -> None:
    clock = VirtualClock()
    provider = ScriptedProvider(
        "only-local",
        [
            ModelRateLimitError(
                "rate limited",
                provider_id="only-local",
                retry_after_seconds=2,
            ),
            8,
        ],
    )
    gateway = gateway_with(
        (provider,),
        ModelGatewayPolicy(
            default_max_attempts=2,
            default_retry_delay_budget_seconds=2,
        ),
        clock,
    )

    response = await gateway.complete(make_request())

    assert response.provider_id == "only-local"
    assert clock.sleeps == [2]
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_cost_budget_reserves_upper_bound_and_accumulates_actual_usage() -> None:
    provider = ScriptedProvider("priced-local", [1, 1, 1], output_tokens=2)
    gateway = gateway_with(
        (provider,),
        ModelGatewayPolicy(
            provider_pricing=(
                ModelProviderPricing(
                    provider_id="priced-local",
                    output_micros_per_million_tokens=1_000_000,
                ),
            )
        ),
    )
    budget = ModelExecutionBudget(max_task_cost_micros=7)

    await gateway.complete(make_request(execution_budget=budget))
    await gateway.complete(make_request(execution_budget=budget))
    with pytest.raises(ModelCostBudgetExceededError):
        await gateway.complete(make_request(execution_budget=budget))

    runtime = gateway.routing_snapshot().providers[0]
    assert runtime.total_cost_micros == 4
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_cost_budget_routes_to_affordable_fallback_before_calling() -> None:
    expensive = ScriptedProvider("expensive-local", [1])
    affordable = ScriptedProvider("affordable-local", [1])
    gateway = gateway_with(
        (expensive, affordable),
        ModelGatewayPolicy(
            role_routes=(
                ModelRoleRoute(
                    role=ModelRole.INTENT,
                    provider_ids=("expensive-local", "affordable-local"),
                ),
            ),
            provider_pricing=(
                ModelProviderPricing(
                    provider_id="expensive-local",
                    output_micros_per_million_tokens=10_000_000,
                ),
                ModelProviderPricing(
                    provider_id="affordable-local",
                    output_micros_per_million_tokens=1_000_000,
                ),
            ),
        ),
    )

    response = await gateway.complete(
        make_request(
            execution_budget=ModelExecutionBudget(max_task_cost_micros=7)
        )
    )

    assert response.provider_id == "affordable-local"
    assert expensive.calls == 0
    assert affordable.calls == 1


@pytest.mark.asyncio
async def test_finite_cost_budget_rejects_provider_without_pricing() -> None:
    provider = ScriptedProvider("unpriced-local", [1])
    gateway = gateway_with((provider,), ModelGatewayPolicy())

    with pytest.raises(ModelProviderPricingRequiredError):
        await gateway.complete(
            make_request(
                execution_budget=ModelExecutionBudget(max_task_cost_micros=100)
            )
        )

    assert provider.calls == 0


@pytest.mark.asyncio
async def test_circuit_opens_then_half_open_probe_closes_it() -> None:
    clock = VirtualClock()
    provider = ScriptedProvider(
        "fragile-local",
        [
            ModelProviderUnavailableError(
                "failure one",
                provider_id="fragile-local",
            ),
            ModelProviderUnavailableError(
                "failure two",
                provider_id="fragile-local",
            ),
            15,
        ],
    )
    gateway = gateway_with(
        (provider,),
        ModelGatewayPolicy(
            circuit_failure_threshold=2,
            circuit_recovery_timeout_seconds=5,
        ),
        clock,
    )

    with pytest.raises(ModelProviderUnavailableError):
        await gateway.complete(make_request())
    with pytest.raises(ModelProviderUnavailableError):
        await gateway.complete(make_request())
    assert gateway.routing_snapshot().providers[0].circuit_state is ModelCircuitState.OPEN

    with pytest.raises(ModelProviderCircuitOpenError):
        await gateway.complete(make_request())
    clock.now += 5
    response = await gateway.complete(make_request())

    assert response.provider_id == "fragile-local"
    assert gateway.routing_snapshot().providers[0].circuit_state is ModelCircuitState.CLOSED


@pytest.mark.asyncio
async def test_half_open_circuit_allows_only_one_concurrent_probe() -> None:
    clock = VirtualClock()
    provider = HalfOpenProbeProvider()
    gateway = ModelGateway(
        default_provider_id=provider.descriptor.provider_id,
        policy=ModelGatewayPolicy(
            circuit_failure_threshold=1,
            circuit_recovery_timeout_seconds=5,
        ),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )
    gateway.register(provider)

    with pytest.raises(ModelProviderUnavailableError):
        await gateway.complete(make_request())
    clock.now += 5
    probe = asyncio.create_task(gateway.complete(make_request()))
    await provider.probe_started.wait()

    with pytest.raises(ModelProviderCircuitOpenError):
        await gateway.complete(make_request())

    provider.release_probe.set()
    response = await probe
    assert response.provider_id == "half-open-local"
    assert provider.calls == 2


def test_routing_api_exposes_only_safe_policy_and_runtime_state(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/model-providers/routing")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert {route["role"] for route in body["routes"]} == {
        "intent",
        "planner",
        "tool_agent",
        "summarizer",
        "verifier",
    }
    assert body["providers"][0]["provider_id"] == "fake-local"
    assert body["providers"][0]["circuit_state"] == "closed"
    assert "credential" not in str(body).lower()
