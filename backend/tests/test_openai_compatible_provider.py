import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.model_gateway import (
    ModelAuthenticationError,
    ModelContentFilteredError,
    ModelGateway,
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelResponseInvalidError,
    ModelStreamInvalidError,
    ModelTimeoutError,
)
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import (
    ModelCapabilityRequirements,
    ModelMessage,
    ModelRequest,
    ModelRole,
    ModelStreamEventType,
    StructuredOutputDefinition,
)
from deskpilot.domain.planning import (
    PlanStep,
    TaskClassification,
    TaskComplexity,
    TaskIntent,
    TaskPlan,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel
from deskpilot.main import create_app
from deskpilot.model_providers.openai_compatible_chat import (
    OpenAICompatibleChatProvider,
)

PROVIDER_ID = "openai-compatible-test"
MODEL = "test-chat-model"
BASE_URL = "https://models.example.test/v1"
API_KEY = "test-api-key-must-never-appear-in-errors"


def make_provider(
    transport: httpx.AsyncBaseTransport,
) -> OpenAICompatibleChatProvider:
    return OpenAICompatibleChatProvider(
        provider_id=PROVIDER_ID,
        display_name="Mock OpenAI-compatible",
        model=MODEL,
        base_url=BASE_URL,
        api_key=SecretStr(API_KEY),
        transport=transport,
    )


def gateway_with(provider: OpenAICompatibleChatProvider) -> ModelGateway:
    gateway = ModelGateway(default_provider_id=PROVIDER_ID)
    gateway.register(provider)
    return gateway


def structured_request() -> ModelRequest:
    return ModelRequest(
        request_id="openai-compatible-structured",
        task_id="task-private-id",
        role=ModelRole.INTENT,
        messages=(
            ModelMessage(role="system", content="Return the requested JSON."),
            ModelMessage(role="user", content="检查磁盘空间", name="deskpilot_user"),
        ),
        privacy_mode="quality_first",
        requirements=ModelCapabilityRequirements(
            structured_output=True,
            strict_json_schema=True,
        ),
        output_schema=StructuredOutputDefinition.from_model(
            name="task_classification",
            description="Classify a DeskPilot task",
            model=TaskClassification,
            strict=True,
        ),
        temperature=0,
        max_output_tokens=512,
        timeout_seconds=1,
        metadata={"internal_trace": "must-not-leave-the-gateway"},
    )


def streaming_request() -> ModelRequest:
    return ModelRequest(
        request_id="openai-compatible-stream",
        task_id="task-stream",
        role=ModelRole.SUMMARIZER,
        messages=(ModelMessage(role="user", content="总结结果"),),
        privacy_mode="quality_first",
        requirements=ModelCapabilityRequirements(streaming=True),
        timeout_seconds=1,
    )


def classification_payload() -> dict[str, object]:
    return TaskClassification(
        intent=TaskIntent.COMPUTER_INFO,
        complexity=TaskComplexity.SIMPLE,
        risk_level=ToolRiskLevel.R0,
        requires_planning=True,
        confidence=0.98,
        recommended_agent="computer",
        rationale="只需要读取磁盘容量元数据。",
    ).model_dump(mode="json")


@pytest.mark.asyncio
async def test_structured_completion_translates_request_and_normalizes_response() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-structured",
                "object": "chat.completion",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": json.dumps(
                                classification_payload(), ensure_ascii=False
                            ),
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 21,
                    "completion_tokens": 13,
                    "total_tokens": 34,
                    "prompt_tokens_details": {"cached_tokens": 5},
                },
            },
        )

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))
    parsed, response = await gateway.complete_structured(
        structured_request(), TaskClassification
    )

    assert parsed.intent is TaskIntent.COMPUTER_INFO
    assert response.native_response_id == "chatcmpl-structured"
    assert response.usage.cached_input_tokens == 5
    assert response.output_text is None
    assert captured["url"] == f"{BASE_URL}/chat/completions"
    assert captured["authorization"] == f"Bearer {API_KEY}"
    body = captured["body"]
    assert isinstance(body, dict)
    assert body["model"] == MODEL
    assert body["max_tokens"] == 512
    assert body["stream"] is False
    assert body["messages"][1]["name"] == "deskpilot_user"
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "task_id" not in body
    assert "request_id" not in body
    assert "metadata" not in body


class FragmentedStream(httpx.AsyncByteStream):
    def __init__(self, content: bytes) -> None:
        self._content = content

    async def __aiter__(self) -> AsyncIterator[bytes]:
        boundaries = (17, 61, 109, 177, len(self._content))
        start = 0
        for end in boundaries:
            if end > start:
                yield self._content[start:end]
            start = end


@pytest.mark.asyncio
async def test_stream_parses_fragmented_sse_and_emits_normalized_events() -> None:
    frames = [
        {
            "id": "chatcmpl-stream",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"role": "assistant", "content": "磁盘"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream",
            "model": MODEL,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "空间充足"},
                    "finish_reason": None,
                }
            ],
        },
        {
            "id": "chatcmpl-stream",
            "model": MODEL,
            "choices": [
                {"index": 0, "delta": {}, "finish_reason": "stop"}
            ],
        },
        {
            "id": "chatcmpl-stream",
            "model": MODEL,
            "choices": [],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 4,
                "total_tokens": 12,
            },
        },
    ]
    sse = "".join(
        f"data: {json.dumps(frame, ensure_ascii=False)}\n\n" for frame in frames
    )
    sse += "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["stream"] is True
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream; charset=utf-8"},
            stream=FragmentedStream(sse.encode("utf-8")),
        )

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))
    events = [event async for event in gateway.stream(streaming_request())]

    assert [event.sequence for event in events] == list(range(len(events)))
    assert [event.type for event in events] == [
        ModelStreamEventType.RESPONSE_STARTED,
        ModelStreamEventType.OUTPUT_TEXT_DELTA,
        ModelStreamEventType.OUTPUT_TEXT_DELTA,
        ModelStreamEventType.USAGE,
        ModelStreamEventType.RESPONSE_COMPLETED,
    ]
    assert "".join(event.text_delta or "" for event in events) == "磁盘空间充足"
    assert events[-1].response is not None
    assert events[-1].response.output_text == "磁盘空间充足"
    assert events[-1].response.usage.total_tokens == 12


@pytest.mark.parametrize(
    ("status", "error_code", "expected", "retryable"),
    [
        (401, "invalid_api_key", ModelAuthenticationError, False),
        (429, "rate_limit_exceeded", ModelRateLimitError, True),
        (429, "insufficient_quota", ModelQuotaExceededError, False),
        (500, "server_error", ModelProviderUnavailableError, True),
        (503, "overloaded", ModelProviderUnavailableError, True),
        (400, "invalid_request_error", ModelRequestRejectedError, False),
    ],
)
@pytest.mark.asyncio
async def test_http_errors_map_to_stable_sanitized_gateway_errors(
    status: int,
    error_code: str,
    expected: type[ModelGatewayError],
    retryable: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            json={
                "error": {
                    "code": error_code,
                    "message": f"sensitive upstream body {API_KEY}",
                }
            },
        )

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))

    with pytest.raises(expected) as raised:
        await gateway.complete(structured_request())
    assert raised.value.retryable is retryable
    assert raised.value.provider_id == PROVIDER_ID
    assert API_KEY not in str(raised.value)
    assert "sensitive upstream body" not in str(raised.value)


@pytest.mark.asyncio
async def test_rate_limit_preserves_bounded_retry_after_hint() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            headers={"Retry-After": "7"},
            json={"error": {"code": "rate_limit_exceeded"}},
        )

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))

    with pytest.raises(ModelRateLimitError) as raised:
        await gateway.complete(structured_request())

    assert raised.value.retry_after_seconds == 7


@pytest.mark.asyncio
async def test_transport_timeout_maps_to_model_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("sensitive timeout", request=request)

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))

    with pytest.raises(ModelTimeoutError) as raised:
        await gateway.complete(structured_request())
    assert raised.value.retryable is True
    assert "sensitive timeout" not in str(raised.value)


@pytest.mark.asyncio
async def test_invalid_completion_identity_and_content_filter_are_rejected() -> None:
    def mismatched(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-wrong-model",
                "model": "unexpected-model",
                "choices": [
                    {
                        "index": 0,
                        "message": {"content": "{}"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            },
        )

    mismatch_gateway = gateway_with(
        make_provider(httpx.MockTransport(mismatched))
    )
    with pytest.raises(ModelResponseInvalidError):
        await mismatch_gateway.complete(structured_request())

    def filtered(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "id": "chatcmpl-filtered",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {"content": "filtered"},
                        "finish_reason": "content_filter",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 0,
                    "total_tokens": 1,
                },
            },
        )

    filtered_gateway = gateway_with(make_provider(httpx.MockTransport(filtered)))
    with pytest.raises(ModelContentFilteredError):
        await filtered_gateway.complete(structured_request())


@pytest.mark.asyncio
async def test_stream_requires_done_and_usage() -> None:
    frame = {
        "id": "chatcmpl-incomplete",
        "model": MODEL,
        "choices": [
            {"index": 0, "delta": {"content": "partial"}, "finish_reason": "stop"}
        ],
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(frame)}\n\n".encode(),
        )

    gateway = gateway_with(make_provider(httpx.MockTransport(handler)))

    with pytest.raises(ModelStreamInvalidError) as raised:
        _ = [event async for event in gateway.stream(streaming_request())]
    assert "[DONE]" in str(raised.value)


@pytest.mark.asyncio
async def test_health_probe_is_normalized_without_exposing_credentials() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == f"{BASE_URL}/models"
        assert request.headers["authorization"] == f"Bearer {API_KEY}"
        return httpx.Response(200, json={"object": "list", "data": []})

    provider = make_provider(httpx.MockTransport(handler))
    health = await provider.health()

    assert health.status == "ready"
    assert health.provider_id == PROVIDER_ID
    assert API_KEY not in (health.detail or "")
    assert API_KEY not in provider.descriptor.model_dump_json()


@pytest.mark.parametrize(
    "base_url",
    [
        "file:///tmp/model",
        "https://user:password@models.example.test/v1",
        "https://models.example.test/v1?token=secret",
        "https://models.example.test/v1#fragment",
    ],
)
def test_base_url_rejects_unsafe_shapes(base_url: str) -> None:
    with pytest.raises(ValueError):
        OpenAICompatibleChatProvider(
            provider_id=PROVIDER_ID,
            display_name="Invalid endpoint",
            model=MODEL,
            base_url=base_url,
        )


def test_task_processor_runs_end_to_end_with_injected_chat_provider(
    tmp_path: Path,
) -> None:
    requested_schemas: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        schema_name = body["response_format"]["json_schema"]["name"]
        requested_schemas.append(schema_name)
        if schema_name == "task_classification":
            result: dict[str, object] = classification_payload()
        else:
            result = TaskPlan(
                summary="通过兼容模型生成磁盘只读检查计划",
                steps=(
                    PlanStep(
                        step_id="s1",
                        agent="supervisor",
                        title="确认任务目标",
                    ),
                    PlanStep(
                        step_id="s2",
                        agent="computer",
                        title="读取磁盘容量元数据",
                        tool_name="computer.disk_usage",
                        tool_version="1.0.0",
                        depends_on=("s1",),
                    ),
                    PlanStep(
                        step_id="s3",
                        agent="verifier",
                        title="验证只读结果",
                        depends_on=("s2",),
                    ),
                ),
            ).model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "id": f"chatcmpl-{schema_name}",
                "model": MODEL,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "content": json.dumps(result, ensure_ascii=False)
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 10,
                    "total_tokens": 20,
                },
            },
        )

    provider = make_provider(httpx.MockTransport(handler))
    session_token = "openai-compatible-test-session-token"
    origin = "http://127.0.0.1:5173"
    settings = Settings(
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'chat-provider.db').as_posix()}",
        fake_step_delay_seconds=0.001,
        session_token=SecretStr(session_token),
        cors_origins=[origin],
        model_default_provider_id=PROVIDER_ID,
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(
        create_app(settings, model_provider=provider), headers=headers
    ) as client:
        created = client.post(
            "/api/v1/tasks",
            json={
                "goal": "使用兼容模型规划并检查磁盘空间",
                "privacy_mode": "quality_first",
                "constraints": ["read_only"],
            },
        )
        assert created.status_code == 201
        task_id = created.json()["task_id"]
        task = created.json()
        for _ in range(100):
            task = client.get(f"/api/v1/tasks/{task_id}").json()
            if task["status"] in {"succeeded", "failed"}:
                break
            time.sleep(0.01)

        assert task["status"] == "succeeded"
        events = client.get(
            f"/api/v1/tasks/{task_id}/events?after_seq=0"
        ).json()

    assert requested_schemas == ["task_classification", "task_plan"]
    model_usage = [event for event in events if event["type"] == "model.usage"]
    assert len(model_usage) == 2
    assert all(
        event["payload"]["provider_id"] == PROVIDER_ID for event in model_usage
    )
    assert any(event["type"] == "tool.completed" for event in events)
    assert events[-1]["type"] == "task.completed"
