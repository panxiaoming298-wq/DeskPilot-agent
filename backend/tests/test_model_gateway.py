from collections.abc import AsyncIterator

import pytest

from deskpilot.application.model_gateway import (
    DuplicateModelProviderError,
    ModelCapabilityUnavailableError,
    ModelGateway,
    ModelPrivacyRouteError,
    ModelProvider,
    ModelProviderUnavailableError,
    ModelResponseInvalidError,
    ModelTimeoutError,
    UnknownModelProviderError,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelLocation,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelRole,
    ModelStreamEvent,
    ModelStreamEventType,
    ProviderHealth,
    StructuredOutputDefinition,
)
from deskpilot.domain.planning import TaskClassification, TaskIntent, TaskPlan
from deskpilot.model_providers.fake import (
    TASK_CLASSIFICATION_SCHEMA,
    TASK_PLAN_SCHEMA,
    FakeModelProvider,
)


def make_request(
    *,
    schema_name: str = TASK_CLASSIFICATION_SCHEMA,
    output_model: type[TaskClassification] | type[TaskPlan] = TaskClassification,
    privacy_mode: str = "local_only",
    provider_hint: str | None = None,
    timeout_seconds: float = 1,
    streaming: bool = False,
    tool_calling: bool = False,
    cloud_fallback_approved: bool = False,
) -> ModelRequest:
    return ModelRequest(
        request_id=f"request-{schema_name}",
        task_id="task-model-gateway",
        role=(
            ModelRole.INTENT
            if output_model is TaskClassification
            else ModelRole.PLANNER
        ),
        messages=(ModelMessage(role="user", content="检查磁盘空间"),),
        privacy_mode=privacy_mode,  # type: ignore[arg-type]
        provider_hint=provider_hint,
        cloud_fallback_approved=cloud_fallback_approved,
        requirements=ModelCapabilityRequirements(
            streaming=streaming,
            structured_output=True,
            tool_calling=tool_calling,
        ),
        output_schema=StructuredOutputDefinition.from_model(
            name=schema_name,
            description="Test structured output",
            model=output_model,
        ),
        timeout_seconds=timeout_seconds,
    )


def gateway_with(provider: ModelProvider) -> ModelGateway:
    gateway = ModelGateway(default_provider_id=provider.descriptor.provider_id)
    gateway.register(provider)
    return gateway


@pytest.mark.asyncio
async def test_fake_provider_returns_valid_classification_and_plan() -> None:
    provider = FakeModelProvider()
    gateway = gateway_with(provider)

    classification, classification_response = await gateway.complete_structured(
        make_request(), TaskClassification
    )
    plan, plan_response = await gateway.complete_structured(
        make_request(
            schema_name=TASK_PLAN_SCHEMA,
            output_model=TaskPlan,
        ),
        TaskPlan,
    )

    assert classification.intent is TaskIntent.COMPUTER_INFO
    assert classification_response.provider_id == "fake-local"
    assert classification_response.usage.total_tokens > 0
    assert plan.steps[1].tool_name == "computer.disk_usage"
    assert plan.steps[1].depends_on == ("s1",)
    assert plan_response.model == "deskpilot-fake-v1"


@pytest.mark.asyncio
async def test_fake_provider_stream_is_contiguous_and_completed() -> None:
    gateway = gateway_with(FakeModelProvider())
    request = make_request(streaming=True)

    events = [event async for event in gateway.stream(request)]

    assert [event.sequence for event in events] == list(range(len(events)))
    assert events[0].type is ModelStreamEventType.RESPONSE_STARTED
    assert events[-1].type is ModelStreamEventType.RESPONSE_COMPLETED
    assert events[-1].response is not None
    assert events[-1].response.request_id == request.request_id


@pytest.mark.asyncio
async def test_gateway_enforces_privacy_capabilities_timeout_and_failure_mapping() -> None:
    cloud = FakeModelProvider(
        provider_id="fake-cloud",
        location=ModelLocation.CLOUD,
    )
    cloud_gateway = gateway_with(cloud)

    with pytest.raises(ModelPrivacyRouteError) as privacy:
        await cloud_gateway.complete(
            make_request(provider_hint="fake-cloud", privacy_mode="local_only")
        )
    assert privacy.value.code == "MODEL_PRIVACY_ROUTE_UNAVAILABLE"

    with pytest.raises(ModelPrivacyRouteError):
        await cloud_gateway.complete(
            make_request(
                provider_hint="fake-cloud",
                privacy_mode="local_preferred",
            )
        )
    approved = await cloud_gateway.complete(
        make_request(
            provider_hint="fake-cloud",
            privacy_mode="local_preferred",
            cloud_fallback_approved=True,
        )
    )
    assert approved.provider_id == "fake-cloud"

    local_gateway = gateway_with(FakeModelProvider())
    with pytest.raises(ModelCapabilityUnavailableError) as capability:
        await local_gateway.complete(make_request(tool_calling=True))
    assert capability.value.code == "MODEL_CAPABILITY_UNAVAILABLE"

    delayed = gateway_with(FakeModelProvider(delay_seconds=0.05))
    with pytest.raises(ModelTimeoutError) as timeout:
        await delayed.complete(make_request(timeout_seconds=0.01))
    assert timeout.value.code == "MODEL_TIMEOUT"
    assert timeout.value.retryable is True

    failing = gateway_with(FakeModelProvider(failure_message="injected"))
    with pytest.raises(ModelProviderUnavailableError) as unavailable:
        await failing.complete(make_request())
    assert unavailable.value.code == "MODEL_PROVIDER_UNAVAILABLE"
    assert "injected" not in str(unavailable.value)


@pytest.mark.asyncio
async def test_gateway_rejects_mismatched_provider_response() -> None:
    class InvalidResponseProvider(FakeModelProvider):
        async def complete(self, request: ModelRequest) -> ModelResponse:
            response = await super().complete(request)
            return response.model_copy(update={"request_id": "another-request"})

    gateway = gateway_with(InvalidResponseProvider())

    with pytest.raises(ModelResponseInvalidError) as invalid:
        await gateway.complete(make_request())
    assert invalid.value.code == "MODEL_RESPONSE_INVALID"


def test_gateway_rejects_duplicate_provider_registration() -> None:
    provider = FakeModelProvider()
    gateway = gateway_with(provider)

    with pytest.raises(DuplicateModelProviderError) as duplicate:
        gateway.register(provider)
    assert duplicate.value.code == "MODEL_PROVIDER_ALREADY_REGISTERED"

    missing_default = ModelGateway(default_provider_id="missing-provider")
    missing_default.register(provider)
    with pytest.raises(UnknownModelProviderError):
        missing_default.validate_configuration()


@pytest.mark.asyncio
async def test_gateway_health_is_normalized() -> None:
    gateway = gateway_with(FakeModelProvider())

    health = await gateway.health()

    assert len(health) == 1
    assert health[0].provider_id == "fake-local"
    assert health[0].status == "ready"


def test_model_provider_protocol_shape_is_documented_for_type_checkers() -> None:
    class MinimalProvider:
        def __init__(self) -> None:
            self._delegate = FakeModelProvider()

        @property
        def descriptor(self):  # type: ignore[no-untyped-def]
            return self._delegate.descriptor

        async def complete(self, request: ModelRequest) -> ModelResponse:
            return await self._delegate.complete(request)

        def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
            return self._delegate.stream(request)

        async def health(self) -> ProviderHealth:
            return await self._delegate.health()

    gateway = gateway_with(MinimalProvider())
    assert gateway.descriptors()[0].provider_id == "fake-local"
