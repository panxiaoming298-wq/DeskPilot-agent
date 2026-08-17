"""Versioned, default-deny telemetry attribute registry."""

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Literal

type AttributeValue = str | bool | int | float
Classification = Literal["operational", "bounded_identity", "correlation"]

TELEMETRY_SCHEMA_VERSION = "deskpilot.telemetry-schema.v1"
_STABLE_VALUE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


@dataclass(frozen=True, slots=True)
class TelemetryAttributeDefinition:
    key: str
    value_type: type[str] | type[bool] | type[int] | type[float]
    classification: Classification
    local_export: bool = True
    remote_export: bool = True
    metric_dimension: bool = False
    enum_values: frozenset[str] | None = None
    max_length: int = 128


_DEFINITIONS = (
    TelemetryAttributeDefinition(
        "deskpilot.telemetry.schema.version", str, "operational", max_length=64
    ),
    TelemetryAttributeDefinition(
        "deskpilot.operation.category",
        str,
        "bounded_identity",
        metric_dimension=True,
        enum_values=frozenset({"task", "model", "tool", "mcp", "evaluation", "other"}),
    ),
    TelemetryAttributeDefinition(
        "deskpilot.outcome",
        str,
        "operational",
        metric_dimension=True,
        enum_values=frozenset(
            {"accepted", "succeeded", "failed", "denied", "cancelled", "unknown", "other"}
        ),
    ),
    TelemetryAttributeDefinition("deskpilot.error.code", str, "operational"),
    TelemetryAttributeDefinition(
        "deskpilot.task.correlation_id", str, "correlation", remote_export=False
    ),
    TelemetryAttributeDefinition("deskpilot.subject.type", str, "bounded_identity"),
    TelemetryAttributeDefinition("deskpilot.subject.id", str, "correlation", remote_export=False),
    TelemetryAttributeDefinition(
        "deskpilot.evaluation.run_id", str, "correlation", remote_export=False
    ),
    TelemetryAttributeDefinition(
        "deskpilot.evaluation.case_id", str, "correlation", remote_export=False
    ),
    TelemetryAttributeDefinition("deskpilot.evaluation.suite_version", int, "operational"),
    TelemetryAttributeDefinition("deskpilot.evaluation.scenario", str, "bounded_identity"),
    TelemetryAttributeDefinition("deskpilot.model.provider_class", str, "bounded_identity"),
    TelemetryAttributeDefinition("deskpilot.model.protocol", str, "bounded_identity"),
    TelemetryAttributeDefinition("deskpilot.attempt.ordinal", int, "operational"),
    TelemetryAttributeDefinition("deskpilot.tool.class", str, "bounded_identity"),
    TelemetryAttributeDefinition(
        "deskpilot.tool.risk",
        str,
        "bounded_identity",
        enum_values=frozenset({"R0", "R1", "R2", "R3", "other"}),
    ),
    TelemetryAttributeDefinition(
        "deskpilot.mcp.operation",
        str,
        "bounded_identity",
        enum_values=frozenset({"initialize", "list_tools", "call_tool", "other"}),
    ),
)


class TelemetryAttributeRegistry:
    """Allows only reviewed keys and values; unknown input is dropped."""

    def __init__(self) -> None:
        self._definitions: Mapping[str, TelemetryAttributeDefinition] = MappingProxyType(
            {item.key: item for item in _DEFINITIONS}
        )
        material = [
            {
                "key": item.key,
                "type": item.value_type.__name__,
                "classification": item.classification,
                "local_export": item.local_export,
                "remote_export": item.remote_export,
                "metric_dimension": item.metric_dimension,
                "enum_values": sorted(item.enum_values) if item.enum_values else None,
                "max_length": item.max_length,
            }
            for item in _DEFINITIONS
        ]
        self.policy_digest = sha256(
            json.dumps(material, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def sanitize(
        self,
        attributes: Mapping[str, object] | None,
        *,
        local: bool,
    ) -> dict[str, AttributeValue]:
        result: dict[str, AttributeValue] = {}
        for key, raw_value in (attributes or {}).items():
            definition = self._definitions.get(key)
            if definition is None:
                continue
            if local and not definition.local_export:
                continue
            if not local and not definition.remote_export:
                continue
            value = self._sanitize_value(definition, raw_value)
            if value is not None:
                result[key] = value
        return result

    @staticmethod
    def _sanitize_value(
        definition: TelemetryAttributeDefinition,
        raw_value: object,
    ) -> AttributeValue | None:
        if definition.value_type is bool:
            return raw_value if isinstance(raw_value, bool) else None
        if definition.value_type is int:
            return (
                raw_value
                if isinstance(raw_value, int) and not isinstance(raw_value, bool)
                else None
            )
        if definition.value_type is float:
            return float(raw_value) if isinstance(raw_value, int | float) else None
        if not isinstance(raw_value, str) or not raw_value:
            return None
        if definition.enum_values is not None:
            return raw_value if raw_value in definition.enum_values else "other"
        if len(raw_value) > definition.max_length or _STABLE_VALUE.fullmatch(raw_value) is None:
            return None
        return raw_value
