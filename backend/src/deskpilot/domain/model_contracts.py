"""Provider-neutral model request, response, capability, and stream contracts."""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

PROVIDER_ID_PATTERN = r"^[a-z][a-z0-9_-]{1,63}$"
SCHEMA_NAME_PATTERN = r"^[a-z][a-z0-9_]{1,63}$"
REQUEST_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"

PrivacyMode = Literal[
    "local_only",
    "local_preferred",
    "balanced",
    "quality_first",
]


class ModelProtocol(StrEnum):
    FAKE = "fake"
    OPENAI_COMPATIBLE_CHAT = "openai_compatible_chat"
    OPENAI_RESPONSES = "openai_responses"
    OLLAMA = "ollama"


class ModelLocation(StrEnum):
    LOCAL = "local"
    CLOUD = "cloud"


class ModelRole(StrEnum):
    INTENT = "intent"
    PLANNER = "planner"
    TOOL_AGENT = "tool_agent"
    SUMMARIZER = "summarizer"
    VERIFIER = "verifier"


class ToolCallingMode(StrEnum):
    NONE = "none"
    PROMPTED = "prompted"
    NATIVE = "native"


class ModelCapabilities(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    streaming: bool = False
    structured_output: bool = False
    strict_json_schema: bool = False
    tool_calling: ToolCallingMode = ToolCallingMode.NONE
    parallel_tool_calls: bool = False
    vision: bool = False
    embeddings: bool = False
    max_context_tokens: int = Field(default=8_192, ge=1)


class ModelCapabilityRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    streaming: bool = False
    structured_output: bool = False
    strict_json_schema: bool = False
    tool_calling: bool = False
    parallel_tool_calls: bool = False
    vision: bool = False
    min_context_tokens: int = Field(default=1, ge=1)


class ModelProviderDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)
    protocol: ModelProtocol
    location: ModelLocation
    capabilities: ModelCapabilities


class ModelMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    role: Literal["system", "user", "assistant", "tool"]
    content: str = Field(min_length=1, max_length=200_000)
    name: str | None = Field(default=None, min_length=1, max_length=100)


class StructuredOutputDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=SCHEMA_NAME_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    json_schema: dict[str, Any]
    strict: bool = False

    @classmethod
    def from_model(
        cls,
        *,
        name: str,
        description: str,
        model: type[BaseModel],
        strict: bool = False,
    ) -> "StructuredOutputDefinition":
        return cls(
            name=name,
            description=description,
            json_schema=model.model_json_schema(),
            strict=strict,
        )


class ModelExecutionBudget(BaseModel):
    """Per-request overrides for Gateway retry and task cost budgets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    max_attempts: int | None = Field(default=None, ge=1, le=8)
    max_retry_delay_seconds: float | None = Field(default=None, ge=0, le=300)
    max_task_cost_micros: int | None = Field(
        default=None,
        ge=0,
        le=1_000_000_000_000,
    )


class ModelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    task_id: str = Field(pattern=REQUEST_ID_PATTERN)
    role: ModelRole
    messages: tuple[ModelMessage, ...] = Field(min_length=1, max_length=200)
    privacy_mode: PrivacyMode
    requirements: ModelCapabilityRequirements = Field(
        default_factory=ModelCapabilityRequirements
    )
    output_schema: StructuredOutputDefinition | None = None
    provider_hint: str | None = Field(default=None, pattern=PROVIDER_ID_PATTERN)
    cloud_fallback_approved: bool = False
    temperature: float = Field(default=0, ge=0, le=2)
    max_output_tokens: int = Field(default=1_024, ge=1, le=1_000_000)
    timeout_seconds: float = Field(default=30, gt=0, le=600)
    execution_budget: ModelExecutionBudget = Field(
        default_factory=ModelExecutionBudget
    )
    metadata: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_structured_output_requirements(self) -> Self:
        if self.output_schema is not None and not self.requirements.structured_output:
            raise ValueError("output_schema requires structured_output capability")
        if self.output_schema is not None and self.output_schema.strict:
            if not self.requirements.strict_json_schema:
                raise ValueError("strict output schema requires strict_json_schema capability")
        return self


class ModelUsage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def validate_total(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens must equal input_tokens + output_tokens")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")
        return self


class ModelFinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALL = "tool_call"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"
    UNKNOWN = "unknown"


class ModelResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    model: str = Field(min_length=1, max_length=200)
    native_response_id: str | None = Field(default=None, max_length=200)
    output_text: str | None = None
    structured_output: dict[str, JsonValue] | None = None
    finish_reason: ModelFinishReason
    usage: ModelUsage
    latency_ms: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_output(self) -> Self:
        if self.output_text is None and self.structured_output is None:
            raise ValueError("model response requires text or structured output")
        return self


class ModelStreamEventType(StrEnum):
    RESPONSE_STARTED = "response.started"
    OUTPUT_TEXT_DELTA = "output_text.delta"
    USAGE = "response.usage"
    RESPONSE_COMPLETED = "response.completed"


class ModelStreamEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request_id: str = Field(pattern=REQUEST_ID_PATTERN)
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    sequence: int = Field(ge=0)
    type: ModelStreamEventType
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    text_delta: str | None = None
    usage: ModelUsage | None = None
    response: ModelResponse | None = None

    @model_validator(mode="after")
    def validate_event_payload(self) -> Self:
        if self.timestamp.utcoffset() is None:
            raise ValueError("model stream timestamp must be timezone-aware")
        if self.type is ModelStreamEventType.OUTPUT_TEXT_DELTA and self.text_delta is None:
            raise ValueError("output_text.delta requires text_delta")
        if self.type is ModelStreamEventType.USAGE and self.usage is None:
            raise ValueError("response.usage requires usage")
        if self.type is ModelStreamEventType.RESPONSE_COMPLETED and self.response is None:
            raise ValueError("response.completed requires response")
        return self


class ProviderHealthStatus(StrEnum):
    READY = "ready"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class ProviderHealth(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    status: ProviderHealthStatus
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    latency_ms: int | None = Field(default=None, ge=0)
    detail: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_checked_at(self) -> Self:
        if self.checked_at.utcoffset() is None:
            raise ValueError("Provider health timestamp must be timezone-aware")
        return self
