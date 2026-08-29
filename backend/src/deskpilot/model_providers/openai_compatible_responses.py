"""HTTP adapter for OpenAI-compatible Responses endpoints."""

import json
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

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


class _InputTokenDetails(_WireModel):
    cached_tokens: int = Field(default=0, ge=0)


class _ResponsesUsage(_WireModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    input_tokens_details: _InputTokenDetails | None = None


class _ResponseError(_WireModel):
    code: str | None = None
    message: str | None = None


class _IncompleteDetails(_WireModel):
    reason: str | None = None


class _ResponseContent(_WireModel):
    type: str
    text: str | None = None
    refusal: str | None = None


class _ResponseOutputItem(_WireModel):
    type: str
    role: str | None = None
    status: str | None = None
    content: tuple[_ResponseContent, ...] = ()


class _ResponseWire(_WireModel):
    id: str = Field(min_length=1, max_length=200)
    model: str = Field(min_length=1, max_length=200)
    status: str
    output: tuple[_ResponseOutputItem, ...] = ()
    usage: _ResponsesUsage | None = None
    error: _ResponseError | None = None
    incomplete_details: _IncompleteDetails | None = None


class OpenAICompatibleResponsesProvider:
    """Translate provider-neutral requests to the OpenAI Responses HTTP shape.

    This adapter intentionally uses only the portable text/structured-output subset.
    Provider-specific hosted tools, server-side conversation state, and implicit storage
    remain disabled so OpenAI, DeepSeek, and Alibaba Cloud Model Studio can share one
    fail-closed contract.
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
            protocol=ModelProtocol.OPENAI_RESPONSES,
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
                    self._url("responses"),
                    json=self._request_body(request, stream=False),
                )
        except httpx.TimeoutException as error:
            raise ModelTimeoutError(
                "OpenAI-compatible Responses request timed out",
                provider_id=self._descriptor.provider_id,
            ) from error
        except httpx.RequestError as error:
            raise ModelProviderUnavailableError(
                "OpenAI-compatible Responses endpoint is unreachable",
                provider_id=self._descriptor.provider_id,
            ) from error

        self._raise_for_http_status(response)
        if len(response.content) > self._max_response_bytes:
            raise ModelResponseInvalidError(
                "OpenAI-compatible Responses payload exceeded the configured size limit",
                provider_id=self._descriptor.provider_id,
            )
        try:
            wire = _ResponseWire.model_validate(response.json())
            return self._model_response(request, wire, started=started)
        except ModelGatewayError:
            raise
        except (TypeError, ValueError, ValidationError) as error:
            raise ModelResponseInvalidError(
                "OpenAI-compatible Responses payload failed Schema validation",
                provider_id=self._descriptor.provider_id,
            ) from error

    async def stream(self, request: ModelRequest) -> AsyncIterator[ModelStreamEvent]:
        started = time.monotonic()
        sequence = 0
        native_response_id: str | None = None
        output_parts: list[str] = []
        saw_started = False
        saw_terminal = False
        last_native_sequence = -1

        try:
            async with self._client(request.timeout_seconds) as client:
                async with client.stream(
                    "POST",
                    self._url("responses"),
                    json=self._request_body(request, stream=True),
                ) as response:
                    self._raise_for_http_status(response)
                    content_type = response.headers.get("content-type", "")
                    if content_type and "text/event-stream" not in content_type.lower():
                        raise ModelStreamInvalidError(
                            "OpenAI-compatible Responses stream returned an unexpected "
                            "content type",
                            provider_id=self._descriptor.provider_id,
                        )
                    async for event_name, payload_text in self._sse_events(response):
                        payload = json.loads(payload_text)
                        if not isinstance(payload, dict):
                            raise ValueError("Responses stream event payload must be an object")
                        payload_type = payload.get("type")
                        if event_name is None:
                            event_name = payload_type if isinstance(payload_type, str) else None
                        elif isinstance(payload_type, str) and payload_type != event_name:
                            raise ValueError("Responses stream event type changed")
                        native_sequence = payload.get("sequence_number")
                        if native_sequence is not None:
                            if (
                                not isinstance(native_sequence, int)
                                or native_sequence <= last_native_sequence
                            ):
                                raise ValueError("Responses stream sequence is not monotonic")
                            last_native_sequence = native_sequence

                        if event_name == "response.created":
                            if saw_started:
                                raise ValueError("Responses stream repeated its start event")
                            created = self._event_response(payload)
                            self._validate_wire_identity(created.model)
                            native_response_id = created.id
                            saw_started = True
                            yield ModelStreamEvent(
                                request_id=request.request_id,
                                provider_id=self._descriptor.provider_id,
                                sequence=sequence,
                                type=ModelStreamEventType.RESPONSE_STARTED,
                            )
                            sequence += 1
                            continue

                        if event_name == "response.output_text.delta":
                            if not saw_started:
                                raise ValueError("Responses stream emitted text before creation")
                            delta = payload.get("delta")
                            if not isinstance(delta, str):
                                raise ValueError("Responses text delta is missing")
                            output_parts.append(delta)
                            yield ModelStreamEvent(
                                request_id=request.request_id,
                                provider_id=self._descriptor.provider_id,
                                sequence=sequence,
                                type=ModelStreamEventType.OUTPUT_TEXT_DELTA,
                                text_delta=delta,
                            )
                            sequence += 1
                            continue

                        if event_name in {"response.completed", "response.incomplete"}:
                            if not saw_started or saw_terminal:
                                raise ValueError("Responses stream terminal event is out of order")
                            wire = self._event_response(payload)
                            if wire.id != native_response_id:
                                raise ValueError("Responses stream response id changed")
                            expected_status = (
                                "completed"
                                if event_name == "response.completed"
                                else "incomplete"
                            )
                            if wire.status != expected_status:
                                raise ValueError("Responses stream terminal status changed")
                            completed = self._model_response(request, wire, started=started)
                            final_content = self._response_text(wire)
                            streamed_content = "".join(output_parts)
                            if output_parts and streamed_content != final_content:
                                raise ValueError(
                                    "Responses stream text does not match final output"
                                )
                            yield ModelStreamEvent(
                                request_id=request.request_id,
                                provider_id=self._descriptor.provider_id,
                                sequence=sequence,
                                type=ModelStreamEventType.USAGE,
                                usage=completed.usage,
                            )
                            sequence += 1
                            yield ModelStreamEvent(
                                request_id=request.request_id,
                                provider_id=self._descriptor.provider_id,
                                sequence=sequence,
                                type=ModelStreamEventType.RESPONSE_COMPLETED,
                                response=completed,
                            )
                            saw_terminal = True
                            break

                        if event_name == "response.failed":
                            raise ModelStreamInvalidError(
                                "OpenAI-compatible Responses stream reported failure",
                                provider_id=self._descriptor.provider_id,
                            )
        except httpx.TimeoutException as error:
            raise ModelTimeoutError(
                "OpenAI-compatible Responses stream timed out",
                provider_id=self._descriptor.provider_id,
            ) from error
        except httpx.RequestError as error:
            raise ModelProviderUnavailableError(
                "OpenAI-compatible Responses stream endpoint is unreachable",
                provider_id=self._descriptor.provider_id,
            ) from error
        except ModelGatewayError:
            raise
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as error:
            raise ModelStreamInvalidError(
                "OpenAI-compatible Responses stream failed Schema validation",
                provider_id=self._descriptor.provider_id,
            ) from error

        if not saw_terminal:
            raise ModelStreamInvalidError(
                "OpenAI-compatible Responses stream ended without a terminal event",
                provider_id=self._descriptor.provider_id,
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

    def _request_body(self, request: ModelRequest, *, stream: bool) -> dict[str, Any]:
        input_items: list[dict[str, Any]] = []
        for message in request.messages:
            if message.role == "tool" or message.name is not None:
                raise ModelRequestRejectedError(
                    "Responses portable subset does not accept tool or named messages",
                    provider_id=self._descriptor.provider_id,
                )
            item: dict[str, Any] = {
                "type": "message",
                "role": message.role,
                "content": message.content,
            }
            input_items.append(item)
        body: dict[str, Any] = {
            "model": self._descriptor.model,
            "input": input_items,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "stream": stream,
            "store": False,
        }
        if request.output_schema is not None:
            body["text"] = {
                "format": {
                    "type": "json_schema",
                    "name": request.output_schema.name,
                    "description": request.output_schema.description,
                    "schema": request.output_schema.json_schema,
                    "strict": request.output_schema.strict,
                }
            }
        return body

    def _model_response(
        self,
        request: ModelRequest,
        wire: _ResponseWire,
        *,
        started: float,
    ) -> ModelResponse:
        self._validate_wire_identity(wire.model)
        if wire.status == "failed" or wire.error is not None:
            raise ModelRequestRejectedError(
                "OpenAI-compatible Responses request failed",
                provider_id=self._descriptor.provider_id,
            )
        if wire.status == "incomplete":
            if (
                wire.incomplete_details is not None
                and wire.incomplete_details.reason == "content_filter"
            ):
                raise ModelContentFilteredError(
                    "OpenAI-compatible Responses request was content filtered",
                    provider_id=self._descriptor.provider_id,
                )
            raise ModelResponseInvalidError(
                "OpenAI-compatible Responses request was incomplete",
                provider_id=self._descriptor.provider_id,
            )
        if wire.status != "completed":
            raise ValueError("Responses request did not reach a terminal state")
        content = self._response_text(wire)
        structured_output = self._structured_output(request, content)
        usage = self._usage(wire.usage)
        return ModelResponse(
            request_id=request.request_id,
            provider_id=self._descriptor.provider_id,
            model=self._descriptor.model,
            native_response_id=wire.id,
            output_text=content if request.output_schema is None else None,
            structured_output=structured_output,
            finish_reason=ModelFinishReason.STOP,
            usage=usage,
            latency_ms=max(0, round((time.monotonic() - started) * 1_000)),
        )

    def _response_text(self, wire: _ResponseWire) -> str:
        parts: list[str] = []
        for item in wire.output:
            if item.type != "message":
                continue
            for content in item.content:
                if content.type == "refusal" or content.refusal is not None:
                    raise ModelContentFilteredError(
                        "Model refused the request",
                        provider_id=self._descriptor.provider_id,
                    )
                if content.type == "output_text" and content.text is not None:
                    parts.append(content.text)
        if not parts:
            raise ValueError("Responses payload has no assistant output text")
        return "".join(parts)

    @staticmethod
    def _event_response(payload: dict[str, Any]) -> _ResponseWire:
        response = payload.get("response")
        if not isinstance(response, dict):
            raise ValueError("Responses stream event has no response object")
        return _ResponseWire.model_validate(response)

    def _structured_output(
        self,
        request: ModelRequest,
        content: str,
    ) -> dict[str, JsonValue] | None:
        if request.output_schema is None:
            return None
        return _JSON_OBJECT.validate_python(json.loads(content))

    @staticmethod
    def _usage(wire: _ResponsesUsage | None) -> ModelUsage:
        if wire is None:
            raise ValueError("Responses payload has no token usage")
        if wire.total_tokens != wire.input_tokens + wire.output_tokens:
            raise ValueError("Responses token usage total is inconsistent")
        cached_tokens = (
            wire.input_tokens_details.cached_tokens
            if wire.input_tokens_details is not None
            else 0
        )
        return ModelUsage(
            input_tokens=wire.input_tokens,
            output_tokens=wire.output_tokens,
            total_tokens=wire.total_tokens,
            cached_input_tokens=cached_tokens,
        )

    def _client(self, timeout_seconds: float) -> httpx.AsyncClient:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if self._api_key is not None:
            headers["Authorization"] = f"Bearer {self._api_key.get_secret_value()}"
        return httpx.AsyncClient(
            headers=headers,
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=self._trust_env,
            transport=self._transport,
        )

    def _url(self, path: str) -> str:
        return f"{self._base_url}/{path.lstrip('/')}"

    async def _sse_events(
        self,
        response: httpx.Response,
    ) -> AsyncIterator[tuple[str | None, str]]:
        event_name: str | None = None
        data_lines: list[str] = []
        async for line in self._utf8_lines(response):
            if not line:
                if data_lines:
                    yield event_name, "\n".join(data_lines)
                event_name = None
                data_lines.clear()
                continue
            if line.startswith(":"):
                continue
            if line.startswith("event:"):
                value = line[6:]
                event_name = value[1:] if value.startswith(" ") else value
                continue
            if line.startswith("data:"):
                value = line[5:]
                data_lines.append(value[1:] if value.startswith(" ") else value)
        if data_lines:
            yield event_name, "\n".join(data_lines)

    async def _utf8_lines(self, response: httpx.Response) -> AsyncIterator[str]:
        buffer = bytearray()
        received = 0
        async for chunk in response.aiter_bytes():
            received += len(chunk)
            if received > self._max_response_bytes:
                raise ModelStreamInvalidError(
                    "OpenAI-compatible Responses stream exceeded the configured size limit",
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
            raise ValueError("Responses payload model does not match the request")

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
