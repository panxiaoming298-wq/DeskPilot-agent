"""Serializable and versioned tool contracts shared by control plane and Runner."""

from enum import StrEnum
from typing import Any, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest

TOOL_NAME_PATTERN = r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$"
SEMVER_PATTERN = r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)$"


class ToolRiskLevel(StrEnum):
    R0 = "R0"
    R1 = "R1"
    R2 = "R2"
    R3 = "R3"
    R4 = "R4"


class ToolIdempotency(StrEnum):
    IDEMPOTENT = "idempotent"
    KEY_REQUIRED = "key_required"
    NON_IDEMPOTENT = "non_idempotent"


class ToolCommitProtocol(StrEnum):
    READ_ONLY = "read_only"
    BROKERED = "brokered"


class ToolExecutionContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    timeout_seconds: int = Field(ge=1, le=3_600)
    idempotency: ToolIdempotency
    max_output_bytes: int = Field(default=262_144, ge=1_024, le=16_777_216)
    resource_locks: tuple[str, ...] = ()
    commit_protocol: ToolCommitProtocol = ToolCommitProtocol.READ_ONLY


class ToolSecurityContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    capabilities: tuple[str, ...] = ()
    network_access: bool = False
    supports_dry_run: bool = False


class ToolContract(BaseModel):
    """Data-only contract. It never contains an import path or executable callable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    version: str = Field(pattern=SEMVER_PATTERN)
    description: str = Field(min_length=1, max_length=500)
    risk_level: ToolRiskLevel
    side_effects: tuple[str, ...] = ()
    reversible: bool = False
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    execution: ToolExecutionContract
    security: ToolSecurityContract

    @model_validator(mode="after")
    def validate_commit_protocol(self) -> Self:
        if self.side_effects and self.execution.commit_protocol is not ToolCommitProtocol.BROKERED:
            raise ValueError("Tool side effects require the brokered commit protocol")
        return self

    @classmethod
    def from_models(
        cls,
        *,
        name: str,
        version: str,
        description: str,
        input_model: type[BaseModel],
        output_model: type[BaseModel],
        risk_level: ToolRiskLevel,
        execution: ToolExecutionContract,
        security: ToolSecurityContract,
        side_effects: tuple[str, ...] = (),
        reversible: bool = False,
    ) -> "ToolContract":
        return cls(
            name=name,
            version=version,
            description=description,
            risk_level=risk_level,
            side_effects=side_effects,
            reversible=reversible,
            input_schema=input_model.model_json_schema(),
            output_schema=output_model.model_json_schema(),
            execution=execution,
            security=security,
        )

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.version)

    @property
    def digest(self) -> str:
        return sha256_digest(self)
