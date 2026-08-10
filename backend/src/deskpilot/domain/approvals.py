"""Public approval contracts for exact, one-shot tool authorization."""

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from deskpilot.domain.schemas import TaskRead
from deskpilot.domain.tool_contracts import (
    SEMVER_PATTERN,
    TOOL_NAME_PATTERN,
    ToolRiskLevel,
)

SHA256_PATTERN = r"^[0-9a-f]{64}$"
STABLE_CODE_PATTERN = r"^[A-Z][A-Z0-9_]{1,99}$"
RESOURCE_KIND_PATTERN = r"^[a-z][a-z0-9_.-]{0,63}$"


def _ensure_timezone_aware(value: datetime) -> datetime:
    if value.utcoffset() is None:
        raise ValueError("Approval timestamps must be timezone-aware")
    return value


AwareDateTime = Annotated[datetime, AfterValidator(_ensure_timezone_aware)]


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self is not self.PENDING


class ApprovalDecision(StrEnum):
    """Immutable user decision, independent from later grant invalidation."""

    APPROVED = "approved"
    REJECTED = "rejected"


class ApprovalResourceRead(BaseModel):
    """One normalized resource value shown to the user before approval."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    kind: str = Field(pattern=RESOURCE_KIND_PATTERN)
    label: str = Field(min_length=1, max_length=32_767)
    operations: tuple[str, ...] = Field(min_length=1, max_length=20)
    version: str | None = Field(default=None, min_length=1, max_length=256)


class DataEgress(BaseModel):
    """Minimal disclosure of whether and where data leaves the local device."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    enabled: bool = False
    destination: str | None = Field(default=None, min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_egress_details(self) -> Self:
        if self.enabled and self.destination is None:
            raise ValueError("data egress requires a destination")
        if not self.enabled and self.destination is not None:
            raise ValueError("local-only data must not declare an egress destination")
        return self


class ApprovalRead(BaseModel):
    """Authenticated public projection of a durable approval request."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        from_attributes=True,
        str_strip_whitespace=True,
    )

    approval_id: str = Field(min_length=1, max_length=40)
    decision_id: str = Field(min_length=1, max_length=80)
    task_id: str = Field(min_length=1, max_length=40)
    call_id: str = Field(min_length=1, max_length=128)
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    status: ApprovalStatus
    decision: ApprovalDecision | None = None
    risk_level: ToolRiskLevel
    title: str = Field(min_length=1, max_length=200)
    purpose: str = Field(min_length=1, max_length=500)
    policy_rule_id: str = Field(min_length=1, max_length=100)
    policy_revision: str = Field(min_length=1, max_length=64)
    reason_code: str = Field(pattern=STABLE_CODE_PATTERN)
    reversible: bool
    capabilities: tuple[str, ...] = Field(max_length=50)
    resource_scope: tuple[ApprovalResourceRead, ...] = Field(max_length=100)
    consequences: tuple[str, ...] = Field(max_length=50)
    data_egress: DataEgress
    preview_hash: str = Field(pattern=SHA256_PATTERN)
    requested_at: AwareDateTime
    expires_at: AwareDateTime
    resolved_at: AwareDateTime | None = None
    resolution_reason: str | None = Field(default=None, min_length=1, max_length=500)
    consumed_at: AwareDateTime | None = None
    updated_at: AwareDateTime

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.expires_at <= self.requested_at:
            raise ValueError("approval expiry must be later than request time")
        if self.updated_at < self.requested_at:
            raise ValueError("approval update time must not precede request time")
        if self.status is ApprovalStatus.PENDING:
            if any(
                value is not None
                for value in (
                    self.decision,
                    self.resolved_at,
                    self.resolution_reason,
                    self.consumed_at,
                )
            ):
                raise ValueError("a pending approval must not contain resolution fields")
            return self

        if self.resolved_at is None:
            raise ValueError("a resolved approval requires resolved_at")
        if self.resolved_at < self.requested_at:
            raise ValueError("approval resolution must not precede request time")
        if (
            self.status is ApprovalStatus.APPROVED
            and self.decision is not ApprovalDecision.APPROVED
        ):
            raise ValueError("an approved authorization requires an approved decision")
        if (
            self.status is ApprovalStatus.REJECTED
            and self.decision is not ApprovalDecision.REJECTED
        ):
            raise ValueError("a rejected authorization requires a rejected decision")
        if self.status in {ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED} and (
            self.decision not in {None, ApprovalDecision.APPROVED}
        ):
            raise ValueError("an invalidated authorization may only preserve an approved decision")
        if self.status is not ApprovalStatus.APPROVED and self.consumed_at is not None:
            raise ValueError("only an approved request may be consumed")
        if self.consumed_at is not None and self.consumed_at < self.resolved_at:
            raise ValueError("approval consumption must not precede resolution")
        return self


class ResolveCommand(BaseModel):
    """Replay-safe user decision bound to the exact server preview."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    preview_hash: str = Field(pattern=SHA256_PATTERN)
    reason: str | None = Field(default=None, min_length=1, max_length=500)
    scope: Literal["once"] = "once"


class ApprovalResolutionRead(BaseModel):
    """Approval decision together with the atomically updated task projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval: ApprovalRead
    task: TaskRead
    replayed: bool = False


__all__ = [
    "ApprovalDecision",
    "ApprovalRead",
    "ApprovalResolutionRead",
    "ApprovalResourceRead",
    "ApprovalStatus",
    "DataEgress",
    "ResolveCommand",
]
