"""Public contracts for the controlled local MCP control plane."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class McpToolRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    name: str
    title: str
    description: str
    risk_floor: Literal["R0", "R1", "R2", "R3", "R4"]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    schema_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class McpServerRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: str
    title: str
    transport: Literal["stdio"] = "stdio"
    protocol_version: str
    command_preview: tuple[str, ...]
    enabled: bool
    revision: int = Field(ge=0)
    network_access: bool
    filesystem_roots: tuple[str, ...]
    client_capabilities: tuple[str, ...]
    tools: tuple[McpToolRead, ...]
    bundle_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    updated_at: datetime | None


class McpServerMutationRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server: McpServerRead
    audit_event_id: str | None


class McpToolCallRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool_name: str = Field(min_length=1, max_length=128)
    arguments: dict[str, Any] = Field(default_factory=dict)


class McpToolCallRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    server_id: str
    tool_name: str
    protocol_version: str
    structured_content: dict[str, Any]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    audit_event_id: str


class McpAuditEventRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    event_id: str
    sequence: int = Field(ge=1)
    server_id: str
    action: Literal["enabled", "disabled", "tool_called", "tool_failed"]
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    result_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_event_digest: str | None
    event_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    details: dict[str, Any]
    occurred_at: datetime


class McpAuditPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    events: tuple[McpAuditEventRead, ...]
    next_after_sequence: int = Field(ge=0)


class McpTextMetricsInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str = Field(min_length=1, max_length=4096)


class McpTextMetricsOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    character_count: int = Field(ge=1)
    line_count: int = Field(ge=1)
    word_count: int = Field(ge=0)
    text_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
