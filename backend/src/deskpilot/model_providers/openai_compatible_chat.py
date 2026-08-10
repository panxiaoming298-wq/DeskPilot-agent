"""HTTP adapter for OpenAI-compatible Chat Completions endpoints."""

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any, Literal

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    SecretStr,
    TypeAdapter,
    ValidationError,
)

from deskpilot.application.model_gateway import (
    ModelAuthenticationError,
    ModelContentFilteredError,
    ModelGatewayError,
    ModelProviderUnavailableError,
    ModelQuotaExceededError,
    ModelRateLimitError,
    ModelRequestRejectedError,
    ModelResponseInvalidError,
    ModelStreamInvalidError,
    ModelTimeoutError,
)
from deskpilot.domain.model_contracts import (
    ModelCapabilities,
    ModelFinishReason,
    ModelLocation,
    ModelProtocol,
    ModelProviderDescriptor,
    ModelRequest,
    ModelResponse,
    ModelStreamEvent,
    ModelStreamEventType,
    ModelUsage,
    ProviderHealth,
    ProviderHealthStatus,
    ToolCallingMode,
)

_JSON_OBJECT = TypeAdapter(dict[str, JsonValue])
_QUOTA_ERROR_CODES = frozenset(
    {
        "credit_balance_exhausted",
        "insufficient_quota",
        "organization_spend_limit_exceeded",
        "organization_usage_limit_exceeded",
        "project_spend_limit_exceeded",
    }
)


class _WireModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class _PromptTokenDetails(_WireModel):
    cached_tokens: int = Field(default=0, ge=0)


class _CompletionUsage(_WireModel):
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    prompt_tokens_details: _PromptTokenDetails | None = None


class _CompletionMessage(_WireModel):
    content: str | None = None
    refusal: str | None = None


class _CompletionChoice(_WireModel):
    index: int = Field(ge=0)
    finish_reason: str
    message: _CompletionMessage


class _ChatCompletion(_WireModel):
    id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    choices: tuple[_CompletionChoice, ...] = Field(min_length=1, max_length=1)
    usage: _CompletionUsage


class _CompletionDelta(_WireModel):
    content: str | None = None
    refusal: str | None = None


class _StreamChoice(_WireModel):
    index: int = Field(ge=0)
    finish_reason: str | None = None
    delta: _CompletionDelta


class _ChatCompletionChunk(_WireModel):
    id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    choices: tuple[_StreamChoice, ...] = Field(max_length=1)
    usage: _CompletionUsage | None = None


class OpenAICompatibleChatProvider:
    """Translate provider-neutral requests to the Chat Completions HTTP shape.

    The endpoint and credential are application configuration, never model output.
    Redirects and environment proxy inheritance are disabled by default so bearer
    credentials cannot silently move to a different host.
    """

    def __init__(
        self,
        *,
        provider_id: str,
        display_name: str,
        model: str,
        base_url: str,
        api_key: SecretStr | None = None,
        location: ModelLocation = ModelLocation.CLOUD,
        supports_streaming: bool = True,
        supports_structured_output: bool = True,
        supports_strict_json_schema: bool = True,
        max_context_tokens: int = 128_000,
        max_tokens_field: Literal["max_tokens", "max_completion_tokens"] = "max_tokens",
        max_response_bytes: int = 4 * 1024 * 1024,
        health_timeout_seconds: float = 5,
        trust_env: bool = False,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = self._validate_base_url(base_url)
        if api_key is not None and not api_key.get_secret_value().strip():
            raise ValueError("OpenAI-compatible API key cannot be blank")
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        if health_timeout_seconds <= 0:
            raise ValueError("health_timeout_seconds must be positive")

        self._descriptor = ModelProviderDescriptor(
            provider_id=provider_id,
            display_name=display_name,
            model=model,
            protocol=ModelProtocol.OPENAI_COMPATIBLE_CHAT,
            location=location,
            capabilities=ModelCapabilities(
                streaming=supports_streaming,
                structured_output=supports_structured_output,
                strict_json_schema=supports_strict_json_schema,
                tool_calling=ToolCallingMode.NONE,
                parallel_tool_calls=False,
                vision=False,
                embeddings=False,
                max_context_tokens=max_context_tokens,
            ),
        )
        self._api_key = api_key
        self._max_tokens_field = max_tokens_field
        self._max_response_bytes = max_response_bytes
        self._health_timeout_seconds = health_timeout_seconds
        self._trust_env = trust_env
        self._transport = transport

    @property
    def descriptor(self) -> ModelProviderDescriptor:
        return self._descriptor

    async def complete(self, request: ModelRequest) -> ModelResponse:
        started = time.monotonic()
        try:
            async with self._client(request.timeout_seconds) as client:
                response = await client.post(
                    self._url("chat/completions"),
                    json=self._request_body(request, stream=False),
                )
        except httpx.TimeoutException as error:
            raise ModelTimeoutError(
                "OpenAI-compatible request timed out",
                provider_id=self._descriptor.provider_id,
            ) from error
        except httpx.RequestError as error:
            raise ModelProviderUnavailableError(
                "OpenAI-compatible endpoint is unreachable",
                provider_id=self._descriptor.provider_id,
            ) from error

        self._raise_for_http_status(response)
        if len(response.content) > self._max_response_bytes:
            raise ModelResponseInvalidError(
                "OpenAI-compatible response exceeded the configured size limit",
                provider_id=self._descriptor.provider_id,
            )
        try:
            wire = _ChatCompletion.model_validate(response.json())
            self._validate_wire_identity(wire.model)
            choice = wire.choices[0]
            if choice.index != 0:
                raise ValueError("Chat completion choice index must be zero")
            if choice.message.refusal:
                raise ModelContentFilteredError(
                    "Model refused the request",
                    provider_id=self._descriptor.provider_id,
                )
            finish_reason = self._finish_reason(choice.finish_reason)
            content = choice.message.content
            if content is None:
                raise ValueError("Chat completion message has no text content")
            structured_output = self._structured_output(request, content)
            usage = self._usage(wire.usage)
        except ModelGatewayError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelResponseInvalidError(
                "OpenAI-compatible response failed Schema validation",
                provider_id=self._descriptor.provider_id,
            ) from error

        return ModelResponse(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            model=self._descriptor.model,
            native_response_id=wire.id,
            output_text=content if request.output_schema is None else None,
            structured_output=structured_output,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=max(0, round((time.monotonic() - started) * 1_000)),
        )

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        sequence = 0
        native_response_id: str | None = None
        finish_reason: ModelFinishReason | None = None
        usage: ModelUsage | None = None
        output_parts: list[str] = []
        saw_done = False

        try:
            async with self._client(request.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    self._url("chat/completions"),
                    json=self._request_body(request, stream=True),
                ) as response:
                    self._raise_for_http_status(response)
                    content_type = response.headers.get("content-type", "")
                    if content_type and "text/event-stream" not in content_type.lower():
                        raise ModelStreamInvalidError(
                            "OpenAI-compatible stream returned an unexpected content type",
                            provider_id=self._descriptor.provider_id,
                        )

                    yield ModelStreamEvent(
                        request_id=request.request_id,
                        provider_id=self._descriptor.provider_id,
                        sequence=sequence,
                        type=ModelStreamEventType.RESPONSE_STARTED,
                    )
                    sequence += 1

                    async for payload in self._sse_payloads(response):
                        if payload == "[DONE]":
                            saw_done = True
                            break
                        chunk = _ChatCompletionChunk.model_validate(json.loads(payload))
                        self._validate_wire_identity(chunk.model)
                        if native_response_id is None:
                            native_response_id = chunk.id
                        elif chunk.id != native_response_id:
                            raise ValueError("Chat completion stream id changed")

                        if chunk.choices:
                            choice = chunk.choices[0]
                            if choice.index != 0:
                                raise ValueError("Chat completion choice index must be zero")
                            if choice.delta.refusal:
                                raise ModelContentFilteredError(
                                    "Model refused the request",
                                    provider_id=self._descriptor.provider_id,
                                )
                            if choice.delta.content is not None:
                                output_parts.append(choice.delta.content)
                                yield ModelStreamEvent(
                                    request_id=request.request_id,
                                    provider_id=self._descriptor.provider_id,
                                    sequence=sequence,
                                    type=ModelStreamEventType.OUTPUT_TEXT_DELTA,
                                    text_delta=choice.delta.content,
                                )
                                sequence += 1
                            if choice.finish_reason is not None:
                                parsed_finish = self._finish_reason(choice.finish_reason)
                                if finish_reason is not None and parsed_finish is not finish_reason:
                                    raise ValueError("Chat completion finish reason changed")
                                finish_reason = parsed_finish

                        if chunk.usage is not None:
                            if usage is not None:
                                raise ValueError("Chat completion stream repeated usage")
                            usage = self._usage(chunk.usage)
                            yield ModelStreamEvent(
                                request_id=request.request_id,
                                provider_id=self._descriptor.provider_id,
                                sequence=sequence,
                                type=ModelStreamEventType.USAGE,
                                usage=usage,
                            )
                            sequence += 1
        except httpx.TimeoutException as error:
            raise ModelTimeoutError(
                "OpenAI-compatible stream timed out",
                provider_id=self._descriptor.provider_id,
            ) from error
        except httpx.RequestError as error:
            raise ModelProviderUnavailableError(
                "OpenAI-compatible stream endpoint is unreachable",
                provider_id=self._descriptor.provider_id,
            ) from error
        except ModelGatewayError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelStreamInvalidError(
                "OpenAI-compatible stream failed Schema validation",
                provider_id=self._descriptor.provider_id,
            ) from error

        if not saw_done:
            raise ModelStreamInvalidError(
                "OpenAI-compatible stream ended without [DONE]",
                provider_id=self._descriptor.provider_id,
            )
        if native_response_id is None or finish_reason is None or usage is None:
            raise ModelStreamInvalidError(
                "OpenAI-compatible stream ended without required completion data",
                provider_id=self._descriptor.provider_id,
            )

        content = "".join(output_parts)
        try:
            structured_output = self._structured_output(request, content)
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelStreamInvalidError(
                "OpenAI-compatible stream output failed Schema validation",
                provider_id=self._descriptor.provider_id,
            ) from error
        completed = ModelResponse(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            model=self._descriptor.model,
            native_response_id=native_response_id,
            output_text=content if request.output_schema is None else None,
            structured_output=structured_output,
            finish_reason=finish_reason,
            usage=usage,
            latency_ms=max(0, round((time.monotonic() - started) * 1_000)),
        )
        yield ModelStreamEvent(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            sequence=sequence,
            type=ModelStreamEventType.RESPONSE_COMPLETED,
            response=completed,
        )

    async def health(self) -> ProviderHealth:
        started = time.monotonic()
        status = ProviderHealthStatus.UNAVAILABLE
        detail = "Endpoint is unavailable"
        try:
            async with self._client(self._health_timeout_seconds) as client:
                response = await client.get(self._url("models"))
            if 200 <= response.status_code < 300:
                status = ProviderHealthStatus.READY
                detail = "Endpoint accepted an authenticated models probe"
            elif response.status_code == 429:
                status = ProviderHealthStatus.DEGRADED
                detail = "Endpoint rate limited the models probe"
            elif response.status_code in {401, 403}:
                detail = "Endpoint rejected provider credentials"
            elif response.status_code < 500:
                status = ProviderHealthStatus.DEGRADED
                detail = "Endpoint does not support the models probe"
        except httpx.TimeoutException:
            detail = "Endpoint models probe timed out"
        except httpx.RequestError:
            detail = "Endpoint models probe could not connect"

        return ProviderHealth(
            provider_id=self._descriptor.provider_id,
            status=status,
            latency_ms=max(0, round((time.monotonic() - started) * 1_000)),
            detail=detail,
        )

    def _client(self, timeout_seconds: float) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = (
                f"Bearer {self._api_key.get_secret_value()}"
            )
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=self._trust_env,
            transport=self._transport,
        )

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    def _request_body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        messages: list[dict[str, str]] = []
        for message in request.messages:
            item = {"role": message.role, "content": message.content}
            if message.name is not None:
                item["name"] = message.name
            messages.append(item)

        body: dict[str, Any] = {
            "model": self._descriptor.model,
            "messages": messages,
            "temperature": request.temperature,
            self._max_tokens_field: request.max_output_tokens,
            "stream": stream,
        }
        if stream:
            body["stream_options"] = {"include_usage": True}
        if request.output_schema is not None:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.output_schema.name,
                    "description": request.output_schema.description,
                    "schema": request.output_schema.json_schema,
                    "strict": request.output_schema.strict,
                },
            }
        return body

    async def _sse_payloads(self, response: httpx.Response) -> AsyncIterator[str]:
        data_lines: list[str] = []
        async for line in self._utf8_lines(response):
            if not line:
                if data_lines:
                    yield "\n".join(data_lines)
                    data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if data_lines:
            yield "\n".join(data_lines)

    async def _utf8_lines(self, response: httpx.Response) -> AsyncIterator[str]:
        buffer = bytearray()
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self._max_response_bytes:
                raise ModelStreamInvalidError(
                    "OpenAI-compatible stream exceeded the configured size limit",
                    provider_id=self._descriptor.provider_id,
                )
            buffer.extend(chunk)
            while True:
                newline = buffer.find(b"\n")
                if newline < 0:
                    break
                raw_line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                yield raw_line.rstrip(b"\r").decode("utf-8", errors="strict")
        if buffer:
            yield bytes(buffer).rstrip(b"\r").decode("utf-8", errors="strict")

    def _raise_for_http_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if 200 <= status < 300:
            return
        provider_id = self._descriptor.provider_id
        if status in {401, 403}:
            raise ModelAuthenticationError(
                "OpenAI-compatible endpoint rejected provider credentials",
                provider_id=provider_id,
            )
        if status == 429:
            if self._error_code(response) in _QUOTA_ERROR_CODES:
                raise ModelQuotaExceededError(
                    "OpenAI-compatible provider quota is unavailable",
                    provider_id=provider_id,
                )
            raise ModelRateLimitError(
                "OpenAI-compatible endpoint rate limited the request",
                provider_id=provider_id,
                retry_after_seconds=self._retry_after_seconds(response),
            )
        if status in {408, 504}:
            raise ModelTimeoutError(
                "OpenAI-compatible endpoint timed out",
                provider_id=provider_id,
            )
        if status >= 500:
            raise ModelProviderUnavailableError(
                "OpenAI-compatible endpoint returned a server error",
                provider_id=provider_id,
                retry_after_seconds=self._retry_after_seconds(response),
            )
        raise ModelRequestRejectedError(
            "OpenAI-compatible endpoint rejected the request",
            provider_id=provider_id,
        )

    @staticmethod
    def _error_code(response: httpx.Response) -> str | None:
        try:
            payload: object = response.json()
        except ValueError:
            return None
        if not isinstance(payload, dict):
            return None
        error = payload.get("error")
        if not isinstance(error, dict):
            return None
        for field in ("code", "type"):
            value = error.get(field)
            if isinstance(value, str):
                return value
        return None

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value is None:
            return None
        value = value.strip()
        if value.isdigit():
            return min(3_600.0, float(value))
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        return min(
            3_600.0,
            max(0.0, (retry_at - datetime.now(UTC)).total_seconds()),
        )

    def _validate_wire_identity(self, model: str) -> None:
        if model != self._descriptor.model:
            raise ValueError("Chat completion response model does not match the request")

    def _structured_output(
        self,
        request: ModelRequest,
        content: str,
    ) -> dict[str, JsonValue] | None:
        if request.output_schema is None:
            return None
        return _JSON_OBJECT.validate_python(json.loads(content))

    @staticmethod
    def _usage(wire: _CompletionUsage) -> ModelUsage:
        if wire.total_tokens != wire.prompt_tokens + wire.completion_tokens:
            raise ValueError("Chat completion usage total is inconsistent")
        cached_tokens = (
            wire.prompt_tokens_details.cached_tokens
            if wire.prompt_tokens_details is not None
            else 0
        )
        return ModelUsage(
            input_tokens=wire.prompt_tokens,
            output_tokens=wire.completion_tokens,
            total_tokens=wire.total_tokens,
            cached_input_tokens=cached_tokens,
        )

    def _finish_reason(self, value: str) -> ModelFinishReason:
        if value == "content_filter":
            raise ModelContentFilteredError(
                "Model output was filtered",
                provider_id=self._descriptor.provider_id,
            )
        return {
            "stop": ModelFinishReason.STOP,
            "length": ModelFinishReason.LENGTH,
            "tool_calls": ModelFinishReason.TOOL_CALL,
            "function_call": ModelFinishReason.TOOL_CALL,
        }.get(value, ModelFinishReason.UNKNOWN)

    @staticmethod
    def _validate_base_url(value: str) -> str:
        try:
            url = httpx.URL(value)
        except Exception as error:
            raise ValueError("OpenAI-compatible base URL is invalid") from error
        if url.scheme not in {"http", "https"} or not url.host:
            raise ValueError("OpenAI-compatible base URL must use HTTP(S)")
        if url.username or url.password or url.query or url.fragment:
            raise ValueError(
                "OpenAI-compatible base URL cannot contain credentials, query, or fragment"
            )
        return str(url).rstrip("/")
