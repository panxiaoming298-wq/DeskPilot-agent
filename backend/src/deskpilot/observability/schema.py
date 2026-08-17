"""Public safe telemetry projections."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.observability.attributes import AttributeValue


class TelemetrySpanRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    trace_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    span_id: str = Field(pattern=r"^[0-9a-f]{16}$")
    parent_span_id: str | None = Field(default=None, pattern=r"^[0-9a-f]{16}$")
    name: str
    kind: str
    status: Literal["unset", "error"]
    attributes: dict[str, AttributeValue]
    started_at: datetime
    completed_at: datetime
    duration_ms: float = Field(ge=0)


class TelemetryTracePage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.telemetry-query.v1"] = "deskpilot.telemetry-query.v1"
    export_policy_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    spans: tuple[TelemetrySpanRead, ...]


class TelemetryMetricPoint(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    category: Literal["task", "model", "tool", "mcp", "evaluation", "other"]
    outcome: Literal["accepted", "succeeded", "failed", "denied", "cancelled", "unknown", "other"]
    operation_count: int = Field(ge=0)
    duration_count: int = Field(ge=0)
    duration_sum_ms: float = Field(ge=0)
    duration_max_ms: float = Field(ge=0)


class TelemetryMetricsRead(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["deskpilot.telemetry-metrics.v1"] = "deskpilot.telemetry-metrics.v1"
    points: tuple[TelemetryMetricPoint, ...]
