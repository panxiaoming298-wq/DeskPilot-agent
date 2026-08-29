import json
from collections.abc import AsyncIterator

import httpx
import pytest
from pydantic import SecretStr

from deskpilot.application.model_gateway import (
    ModelContentFilteredError,
    ModelGateway,
    ModelRequestRejectedError,
    ModelResponseInvalidError,
    ModelStreamInvalidError,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelStreamEventType,
    StructuredOutputDefinition,
)
from deskpilot.domain.planning import (
    TaskClassification,
    TaskComplexity,
    TaskIntent,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.model_providers.openai_compatible_responses import (
    OpenAICompatibleResponsesProvider,
)

API_KEY = "responses-api-key-must-never-appear"


def _classification_payload() -> dict[str, object]:
    return TaskClassification(
        intent=TaskIntent.COMPUTER_INFO,
        complexity=TaskComplexity.SIMPLE,
        risk_level=ToolRiskLevel.R0,
        requires_planning=True,
        confidence=0.98,
        recommended_agent="computer",
        rationale="Only read bounded system metadata.",
    ).model_dump(mode="json")


def _structured_request(provider_id: str) -> ModelRequest:
    return ModelRequest(
        request_id=f"{provider_id}-structured",
        task_id="synthetic-provider-contract",
        role=ModelRole.INTENT,
        messages=(
            ModelMessage(role="system", content="Return the requested JSON."),
            ModelMessage(role="user", content="Classify the synthetic request."),
        ),
        privacy_mode="quality_first",
        requirements=ModelCapabilityRequirements(
            structured_output=True,
            strict_json_schema=True,
        ),
        output_schema=StructuredOutputDefinition.from_model(
            name="task_classification",
            description="Classify a synthetic DeskPilot task",
            model=TaskClassification,
            strict=True,
        ),
        provider_hint=provider_id,
        temperature=0,
        max_output_tokens=512,
        timeout_seconds=1,
        metadata={"must_not_leave": "provider-neutral metadata"},
    )


def _provider(
    *,
    provider_id: str,
    model: str,
    base_url: str,
    transport: httpx.AsyncBaseTransport,
) -> OpenAICompatibleResponsesProvider:
    return OpenAICompatibleResponsesProvider(
        provider_id=provider_id,
        display_name=f"Synthetic {provider_id}",
        model=model,
        base_url=base_url,
        api_key=SecretStr(API_KEY),
        transport=transport,
    )


@pytest.mark.parametrize(
    ("provider_id", "model", "base_url"),
    [
        ("openai-responses", "openai-exact-model", "https://api.openai.test/v1"),
        ("deepseek-responses", "deepseek-v4-flash", "https://api.deepseek.test"),
        (
            "bailian-responses",
            "qwen3.8-max",
            "https://workspace.cn-beijing.maas.aliyuncs.test/compatible-mode/v1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_portable_responses_contract_for_three_provider_profiles(
    provider_id: str,
    model: str,
    base_url: str,
) -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": f"resp-{provider_id}",
                "object": "response",
                "model": model,
                "status": "completed",
                "output": [
                    {"type": "reasoning", "summary": []},
                    {
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": json.dumps(_classification_payload()),
                                "annotations": [],
                            }
                        ],
                    },
                ],
                "usage": {
                    "input_tokens": 23,
                    "output_tokens": 17,
                    "total_tokens": 40,
                    "input_tokens_details": {"cached_tokens": 5},
                },
            },
        )

    provider = _provider(
        provider_id=provider_id,
        model=model,
        base_url=base_url,
        transport=httpx.MockTransport(handler),
    )
    gateway = ModelGateway(default_provider_id=provider_id)
    gateway.register(provider)

    parsed, response = await gateway.complete_structured(
        _structured_request(provider_id),
        TaskClassification,
    )

    assert parsed.intent is TaskIntent.COMPUTER_INFO
    assert response.native_response_id == f"resp-{provider_id}"
    assert response.usage.cached_input_tokens == 5
    assert provider.descriptor.protocol.value == "openai_responses"
    assert captured["url"] == f"{base_url}/responses"
    assert captured["authorization"] == f"Bearer {API_KEY}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == model
    assert body["max_output_tokens"] == 512
    assert body["store"] is False
    assert body["stream"] is False
    assert body["text"]["format"]["type"] == "json_schema"
    assert body["text"]["format"]["strict"] is True
    assert body["input"][0]["type"] == "message"
    assert "metadata" not in body
    assert "task_id" not in body
    assert "request_id" not in body


class _FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._content), 29):
            yield self._content[start : start + 29]


def _sse(event_type: str, sequence: int, **payload: object) -> str:
    body = {"type": event_type, "sequence_number": sequence, **payload}
    return f"event: {event_type}\ndata: {json.dumps(body)}\n\n"


@pytest.mark.asyncio
async def test_responses_stream_uses_semantic_terminal_event_without_done_marker() -> None:
    provider_id = "responses-stream"
    model = "stream-model"
    final = {
        "id": "resp-stream",
        "model": model,
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "hello"}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }
    created = {**final, "status": "in_progress", "output": [], "usage": None}
    content = (
        _sse("response.created", 0, response=created)
        + _sse("response.output_text.delta", 1, delta="hel")
        + _sse("response.output_text.delta", 2, delta="lo")
        + _sse("response.completed", 3, response=final)
    ).encode()

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            stream=_FragmentedStream(content),
        )

    provider = _provider(
        provider_id=provider_id,
        model=model,
        base_url="https://responses.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(
        request_id="responses-stream-request",
        task_id="synthetic-stream-task",
        role=ModelRole.SUMMARIZER,
        messages=(ModelMessage(role="user", content="Say hello."),),
        privacy_mode="quality_first",
        requirements=ModelCapabilityRequirements(streaming=True),
        provider_hint=provider_id,
        timeout_seconds=1,
    )

    events = [event async for event in provider.stream(request)]

    assert [event.type for event in events] == [
        ModelStreamEventType.RESPONSE_STARTED,
        ModelStreamEventType.OUTPUT_TEXT_DELTA,
        ModelStreamEventType.OUTPUT_TEXT_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.RESPONSE_COMPLETED,
    ]
    assert events[-1].response is not None
    assert events[-1].response.output_text == "hello"


@pytest.mark.asyncio
async def test_responses_rejects_identity_drift_refusal_and_incomplete_stream() -> None:
    base_url = "https://responses.example.test/v1"

    def mismatch(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-wrong",
                "model": "substituted-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "{}"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
            },
        )

    provider = _provider(
        provider_id="responses-invalid",
        model="expected-model",
        base_url=base_url,
        transport=httpx.MockTransport(mismatch),
    )
    with pytest.raises(ModelResponseInvalidError):
        await provider.complete(_structured_request("responses-invalid"))

    def refused(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "resp-refused",
                "model": "expected-model",
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "refusal", "refusal": "blocked"}],
                    }
                ],
                "usage": {"input_tokens": 1, "output_tokens": 0, "total_tokens": 1},
            },
        )

    refused_provider = _provider(
        provider_id="responses-invalid",
        model="expected-model",
        base_url=base_url,
        transport=httpx.MockTransport(refused),
    )
    with pytest.raises(ModelContentFilteredError):
        await refused_provider.complete(_structured_request("responses-invalid"))

    def unfinished(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_sse(
                "response.created",
                0,
                response={
                    "id": "resp-incomplete",
                    "model": "expected-model",
                    "status": "in_progress",
                },
            ).encode(),
        )

    unfinished_provider = _provider(
        provider_id="responses-invalid",
        model="expected-model",
        base_url=base_url,
        transport=httpx.MockTransport(unfinished),
    )
    request = ModelRequest(
        request_id="responses-incomplete-stream",
        task_id="synthetic-stream-task",
        role=ModelRole.SUMMARIZER,
        messages=(ModelMessage(role="user", content="Say hello."),),
        privacy_mode="quality_first",
        requirements=ModelCapabilityRequirements(streaming=True),
        provider_hint="responses-invalid",
        timeout_seconds=1,
    )
    with pytest.raises(ModelStreamInvalidError, match="terminal event"):
        _ = [event async for event in unfinished_provider.stream(request)]

    incomplete = {
        "id": "resp-incomplete",
        "model": "expected-model",
        "status": "incomplete",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial"}],
            }
        ],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    content = (
        _sse(
            "response.created",
            0,
            response={
                **incomplete,
                "status": "in_progress",
                "output": [],
                "usage": None,
                "incomplete_details": None,
            },
        )
        + _sse("response.output_text.delta", 1, delta="partial")
        + _sse("response.incomplete", 2, response=incomplete)
    ).encode()

    def incomplete_terminal(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )

    incomplete_provider = _provider(
        provider_id="responses-invalid",
        model="expected-model",
        base_url=base_url,
        transport=httpx.MockTransport(incomplete_terminal),
    )
    with pytest.raises(ModelResponseInvalidError, match="incomplete"):
        _ = [event async for event in incomplete_provider.stream(request)]


@pytest.mark.asyncio
async def test_responses_portable_subset_rejects_named_messages_before_network() -> None:
    called = False

    def handler(_: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(500)

    provider = _provider(
        provider_id="responses-portable",
        model="expected-model",
        base_url="https://responses.example.test/v1",
        transport=httpx.MockTransport(handler),
    )
    request = ModelRequest(
        request_id="responses-named-message",
        task_id="synthetic-named-message",
        role=ModelRole.SUMMARIZER,
        messages=(ModelMessage(role="user", content="hello", name="named-user"),),
        privacy_mode="quality_first",
        provider_hint="responses-portable",
    )

    with pytest.raises(ModelRequestRejectedError, match="tool or named messages"):
        await provider.complete(request)
    assert called is False
