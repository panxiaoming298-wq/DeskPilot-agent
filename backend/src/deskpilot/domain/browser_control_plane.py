"""Read-only contracts for the local Edge Browser control plane."""

from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.browser_automation import (
    BrowserActionKind,
    BrowserOriginAllowlistSnapshot,
    BrowserVerificationKind,
)
from deskpilot.domain.tool_contracts import ToolRiskLevel


class BrowserActionContractRead(BaseModel):
    """Public action metadata; this object cannot authorize an execution."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: BrowserActionKind
    capability: str = Field(pattern=r"^browser\.[a-z_]{2,40}$")
    risk_level: ToolRiskLevel
    requires_origin_allowlist: Literal[True] = True
    requires_fresh_approval: bool
    automatic_retries: Literal[0] = 0
    postcondition_verification: BrowserVerificationKind


class BrowserControlPlaneSnapshot(BaseModel):
    """Authenticated projection of durable configuration, never an operator."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-control-plane.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    configuration_id: Literal["edge-deskpilot-v1"]
    revision: int = Field(ge=1)
    browser_product: Literal["microsoft_edge"]
    profile_name: Literal["DeskPilot"]
    profile_mode: Literal["application_managed_dedicated"]
    visible_window_required: Literal[True] = True
    manual_login_only: Literal[True] = True
    acceptance_loopback_only: Literal[True] = True
    semantic_dom_targeting_only: Literal[True] = True
    profile_created: Literal[False] = False
    browser_launched: Literal[False] = False
    operator_enabled: Literal[False] = False
    origin_allowlist: BrowserOriginAllowlistSnapshot
    actions: tuple[BrowserActionContractRead, ...] = Field(min_length=8, max_length=8)
    browser_operator_available: Literal[False] = False
    network_execution_available: Literal[False] = False
    action_execution_available: Literal[False] = False
    updated_at: AwareDatetime
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def projection_is_bound_and_digested(self) -> Self:
        if self.origin_allowlist.policy_digest != self.policy_digest:
            raise ValueError("Browser allowlist is not bound to the active policy")
        if self.origin_allowlist.revision != self.revision:
            raise ValueError("Browser allowlist revision is not active")
        expected_actions = tuple(action for action in BrowserActionKind)
        if tuple(item.action for item in self.actions) != expected_actions:
            raise ValueError("Browser control-plane action matrix changed")
        expected_digest = sha256_digest(
            self.model_dump(mode="json", exclude={"snapshot_digest"})
        )
        if self.snapshot_digest != expected_digest:
            raise ValueError("Browser control-plane digest changed")
        return self
