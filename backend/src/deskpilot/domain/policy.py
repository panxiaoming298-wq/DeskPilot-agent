"""Deterministic policy facts, decisions, and Runner authorization grants."""

from enum import StrEnum
from typing import Any, Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.tool_contracts import SEMVER_PATTERN, TOOL_NAME_PATTERN, ToolRiskLevel

SHA256_PATTERN = r"^[0-9a-f]{64}$"
POLICY_IDENTIFIER_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class PolicyEffect(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


class PolicyResource(BaseModel):
    """One canonical resource and the exact operations requested on it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str = Field(pattern=r"^[a-z][a-z0-9_]{0,63}$")
    identifier: str = Field(min_length=1, max_length=32_767)
    operations: tuple[str, ...] = ()
    version_digest: str | None = Field(default=None, pattern=SHA256_PATTERN)
    display_name: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("operations")
    @classmethod
    def normalize_operations(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 200 for item in value):
            raise ValueError("resource operations must be non-empty and at most 200 characters")
        return tuple(sorted(set(value)))

    @property
    def scope_key(self) -> tuple[str, str]:
        return (self.kind, self.identifier)


def policy_resource_scope_digest(resources: tuple[PolicyResource, ...]) -> str:
    return sha256_digest(
        {"resources": [resource.model_dump(mode="json") for resource in resources]}
    )


class ToolAuthorizationRequest(BaseModel):
    """Trusted, structured facts evaluated before a Tool Runner dispatch."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    task_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    actor: str = Field(min_length=1, max_length=200)
    origin: Literal["builtin", "plugin", "mcp"] = "builtin"
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=SHA256_PATTERN)
    arguments_digest: str = Field(pattern=SHA256_PATTERN)
    risk_level: ToolRiskLevel
    side_effects: tuple[str, ...] = ()
    reversible: bool = False
    capabilities: tuple[str, ...] = ()
    network_access: bool = False
    data_egress: bool = False
    resources: tuple[PolicyResource, ...] = Field(min_length=1)
    expected_resource_versions_digest: str = Field(pattern=SHA256_PATTERN)
    interactive: bool = True
    batch_count: int = Field(default=1, ge=1, le=1_000_000)

    @field_validator("side_effects", "capabilities")
    @classmethod
    def normalize_fact_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 200 for item in value):
            raise ValueError("policy facts must be non-empty and at most 200 characters")
        return tuple(sorted(set(value)))

    @field_validator("resources")
    @classmethod
    def normalize_resources(
        cls,
        value: tuple[PolicyResource, ...],
    ) -> tuple[PolicyResource, ...]:
        keys = [resource.scope_key for resource in value]
        if len(keys) != len(set(keys)):
            raise ValueError("policy resources must have unique kind/identifier pairs")
        return tuple(
            sorted(
                value,
                key=lambda resource: (
                    resource.kind,
                    resource.identifier,
                    resource.operations,
                    resource.version_digest or "",
                    resource.display_name or "",
                ),
            )
        )

    @property
    def resource_scope_digest(self) -> str:
        return policy_resource_scope_digest(self.resources)

    @property
    def request_digest(self) -> str:
        return sha256_digest(self)


class PolicyDecision(BaseModel):
    """Auditable deterministic result of one policy evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(pattern=r"^pdec_[0-9a-f]{64}$")
    effect: PolicyEffect
    effective_risk: ToolRiskLevel
    policy_revision: str = Field(pattern=POLICY_IDENTIFIER_PATTERN)
    rule_id: str = Field(pattern=POLICY_IDENTIFIER_PATTERN)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    request_digest: str = Field(pattern=SHA256_PATTERN)
    resource_scope_digest: str = Field(pattern=SHA256_PATTERN)
    approval_ttl_seconds: int | None = Field(default=None, ge=1, le=86_400)

    @model_validator(mode="after")
    def validate_approval_ttl(self) -> Self:
        requires_approval = self.effect is PolicyEffect.REQUIRE_APPROVAL
        if requires_approval != (self.approval_ttl_seconds is not None):
            raise ValueError(
                "approval_ttl_seconds must be present exactly when approval is required"
            )
        return self


class _ToolAuthorizationGrantBinding(BaseModel):
    """Every policy and call fact covered by a Runner authorization ID."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    decision_id: str = Field(pattern=r"^pdec_[0-9a-f]{64}$")
    request_digest: str = Field(pattern=SHA256_PATTERN)
    task_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    call_id: str = Field(min_length=1, max_length=128)
    actor_id: str = Field(min_length=1, max_length=200)
    origin: Literal["builtin", "plugin", "mcp"]
    tool_name: str = Field(pattern=TOOL_NAME_PATTERN)
    tool_version: str = Field(pattern=SEMVER_PATTERN)
    contract_digest: str = Field(pattern=SHA256_PATTERN)
    policy_revision: str = Field(pattern=POLICY_IDENTIFIER_PATTERN)
    rule_id: str = Field(pattern=POLICY_IDENTIFIER_PATTERN)
    reason_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,99}$")
    effect: Literal[PolicyEffect.ALLOW] = PolicyEffect.ALLOW
    effective_risk: ToolRiskLevel
    arguments_digest: str = Field(pattern=SHA256_PATTERN)
    resource_scope_digest: str = Field(pattern=SHA256_PATTERN)
    expected_resource_versions_digest: str = Field(pattern=SHA256_PATTERN)
    capabilities: tuple[str, ...] = ()
    network_access: bool = False
    data_egress: bool = False
    side_effects: tuple[str, ...] = ()
    reversible: bool = False
    resources: tuple[PolicyResource, ...] = Field(min_length=1)
    interactive: bool = True
    batch_count: int = Field(default=1, ge=1, le=1_000_000)
    approval_id: str | None = Field(default=None, min_length=1, max_length=128)
    preview_hash: str | None = Field(default=None, pattern=SHA256_PATTERN)
    approved_at: AwareDatetime | None = None
    grant_expires_at: AwareDatetime | None = None

    @field_validator("capabilities", "side_effects")
    @classmethod
    def normalize_policy_fact_sets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item or len(item) > 200 for item in value):
            raise ValueError("authorization facts must be non-empty and at most 200 characters")
        return tuple(sorted(set(value)))

    @field_validator("resources")
    @classmethod
    def normalize_bound_resources(
        cls,
        value: tuple[PolicyResource, ...],
    ) -> tuple[PolicyResource, ...]:
        keys = [resource.scope_key for resource in value]
        if len(keys) != len(set(keys)):
            raise ValueError("authorization resources must have unique kind/identifier pairs")
        return tuple(
            sorted(
                value,
                key=lambda resource: (
                    resource.kind,
                    resource.identifier,
                    resource.operations,
                    resource.version_digest or "",
                    resource.display_name or "",
                ),
            )
        )

    @model_validator(mode="after")
    def validate_approval_binding(self) -> Self:
        approval_fields = (
            self.approval_id,
            self.preview_hash,
            self.approved_at,
            self.grant_expires_at,
        )
        if any(value is not None for value in approval_fields) and not all(
            value is not None for value in approval_fields
        ):
            raise ValueError(
                "approval_id, preview_hash, approved_at, and grant_expires_at "
                "must be provided together"
            )
        if (
            self.approved_at is not None
            and self.grant_expires_at is not None
            and self.approved_at > self.grant_expires_at
        ):
            raise ValueError("approval time must not be later than grant expiry")
        if self.resource_scope_digest != policy_resource_scope_digest(self.resources):
            raise ValueError("resource_scope_digest does not match authorization resources")
        return self


class ToolAuthorizationGrant(_ToolAuthorizationGrantBinding):
    """Signed, exact-call authorization proof embedded in Runner IPC."""

    authorization_id: str = Field(pattern=r"^auth_[0-9a-f]{64}$")

    @property
    def expected_authorization_id(self) -> str:
        return f"auth_{sha256_digest(self.model_dump(mode='json', exclude={'authorization_id'}))}"

    @classmethod
    def issue(cls, **data: Any) -> Self:
        binding = _ToolAuthorizationGrantBinding.model_validate(data)
        return cls(
            authorization_id=f"auth_{sha256_digest(binding)}",
            **binding.model_dump(mode="python"),
        )

    @model_validator(mode="after")
    def validate_authorization_id(self) -> Self:
        if self.authorization_id != self.expected_authorization_id:
            raise ValueError("authorization_id does not cover the complete grant binding")
        return self
