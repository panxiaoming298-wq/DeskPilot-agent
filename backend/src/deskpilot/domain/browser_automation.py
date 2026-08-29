"""Frozen contracts for the phase 117A local-only Browser Agent boundary."""

from datetime import timedelta
from enum import StrEnum
from typing import Literal, Self
from urllib.parse import urlsplit

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.tool_contracts import ToolRiskLevel

MAX_BROWSER_APPROVAL_VALIDITY = timedelta(minutes=5)


class BrowserActionKind(StrEnum):
    NAVIGATE = "navigate"
    DOM_READ = "dom_read"
    SCREENSHOT = "screenshot"
    FORM_PREFILL = "form_prefill"
    SUBMIT = "submit"
    UPLOAD = "upload"
    DOWNLOAD = "download"
    PUBLISH = "publish"


BrowserVerificationKind = Literal[
    "location_and_document_identity",
    "dom_snapshot_digest",
    "image_digest_and_window_identity",
    "dom_value_readback",
    "resulting_document_or_receipt",
    "selected_file_and_dom_state",
    "destination_file_digest",
    "published_state_and_origin",
]
BrowserSensitiveDataKind = Literal[
    "cookie",
    "password",
    "one_time_code",
    "two_factor_secret",
    "captcha_solution",
]


def normalize_browser_origin(value: str) -> str:
    """Accept one credential-free HTTP(S) origin, never a URL path or query."""

    parsed = urlsplit(value)
    try:
        hostname = parsed.hostname
        if hostname is not None:
            hostname.encode("ascii")
        port = parsed.port
    except (UnicodeEncodeError, ValueError) as error:
        raise ValueError("Browser origin contains an invalid host or port") from error
    if (
        parsed.scheme not in {"http", "https"}
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Browser origin must be one credential-free HTTP(S) origin")
    host = hostname.lower()
    loopback = host in {"127.0.0.1", "localhost", "::1"}
    if not loopback and (parsed.scheme != "https" or port is not None):
        raise ValueError("Public Browser origins require HTTPS on the default port")
    rendered_host = f"[{host}]" if ":" in host else host
    normalized = f"{parsed.scheme}://{rendered_host}"
    if port is not None:
        normalized = f"{normalized}:{port}"
    if value != normalized:
        raise ValueError("Browser origin must be normalized")
    return value


def browser_origin_is_loopback(origin: str) -> bool:
    parsed = urlsplit(origin)
    return parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def browser_action_approval_preview_hash(
    proposal: "BrowserActionProposal",
) -> str:
    return sha256_digest(
        {
            "schema_version": "deskpilot.browser-action-approval-preview.v1",
            "proposal_digest": proposal.proposal_digest,
            "action": proposal.action.value,
            "origin": proposal.origin,
            "target_digest": proposal.target_digest,
            "content_digest": proposal.content_digest,
        }
    )


class BrowserProfilePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    browser_product: Literal["microsoft_edge"] = "microsoft_edge"
    profile_name: Literal["DeskPilot"] = "DeskPilot"
    profile_mode: Literal["application_managed_dedicated"] = (
        "application_managed_dedicated"
    )
    visible_window_required: Literal[True] = True
    manual_login_only: Literal[True] = True
    automated_login: Literal[False] = False
    existing_personal_profile_reuse: Literal[False] = False
    default_allowed_origins: tuple[str, ...] = ()
    acceptance_loopback_only: Literal[True] = True
    semantic_dom_targeting_only: Literal[True] = True
    arbitrary_coordinate_input: Literal[False] = False

    @model_validator(mode="after")
    def default_allowlist_stays_empty(self) -> Self:
        if self.default_allowed_origins:
            raise ValueError("Browser default origin allowlist must stay empty")
        return self


class BrowserSensitiveDataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cookie_values_readable: Literal[False] = False
    passwords_readable: Literal[False] = False
    one_time_codes_readable: Literal[False] = False
    two_factor_secrets_readable: Literal[False] = False
    captcha_bypass_allowed: Literal[False] = False
    permission_dialogs_require_user: Literal[True] = True
    authentication_challenges_require_user: Literal[True] = True
    screenshot_sensitive_region_redaction_required: Literal[True] = True
    dom_and_ui_text_trust: Literal["untrusted_external_input"] = (
        "untrusted_external_input"
    )
    dom_or_ui_text_can_grant_authority: Literal[False] = False


class BrowserActionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    action: BrowserActionKind
    capability: str = Field(pattern=r"^browser\.[a-z_]{2,40}$")
    risk_level: ToolRiskLevel
    requires_origin_allowlist: Literal[True] = True
    requires_fresh_approval: bool
    approval_binds_origin: bool
    approval_binds_target_digest: bool
    approval_binds_content_digest: bool
    automatic_retries: Literal[0] = 0
    postcondition_verification: BrowserVerificationKind

    @model_validator(mode="after")
    def approval_scope_is_complete_or_absent(self) -> Self:
        bindings = (
            self.approval_binds_origin,
            self.approval_binds_target_digest,
            self.approval_binds_content_digest,
        )
        if self.requires_fresh_approval != all(bindings) or (
            not self.requires_fresh_approval and any(bindings)
        ):
            raise ValueError("Browser approval bindings must be exact and all-or-none")
        return self


class BrowserOfflineExecutionBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    browser_profile_created: Literal[False] = False
    browser_launched: Literal[False] = False
    desktop_application_control: Literal[False] = False
    network_access: Literal[False] = False
    action_executed: Literal[False] = False
    model_called: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False


class BrowserAutomationPolicy(BaseModel):
    """117A-A policy asset; loading it grants no browser or network authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-automation-policy.v1"]
    policy_id: Literal["phase117-edge-browser-local"]
    version: Literal[1] = 1
    approval_validity_seconds: Literal[300] = 300
    profile: BrowserProfilePolicy
    sensitive_data: BrowserSensitiveDataPolicy
    actions: tuple[BrowserActionPolicy, ...] = Field(min_length=8, max_length=8)
    offline_execution_boundary: BrowserOfflineExecutionBoundary

    @model_validator(mode="after")
    def exact_action_matrix(self) -> Self:
        actual = tuple(
            (
                item.action.value,
                item.capability,
                item.risk_level.value,
                item.requires_fresh_approval,
                item.postcondition_verification,
            )
            for item in self.actions
        )
        expected = (
            (
                "navigate",
                "browser.navigate",
                "R1",
                False,
                "location_and_document_identity",
            ),
            ("dom_read", "browser.dom_read", "R0", False, "dom_snapshot_digest"),
            (
                "screenshot",
                "browser.screenshot",
                "R0",
                False,
                "image_digest_and_window_identity",
            ),
            (
                "form_prefill",
                "browser.form_prefill",
                "R1",
                False,
                "dom_value_readback",
            ),
            (
                "submit",
                "browser.submit",
                "R2",
                True,
                "resulting_document_or_receipt",
            ),
            (
                "upload",
                "browser.upload",
                "R2",
                True,
                "selected_file_and_dom_state",
            ),
            (
                "download",
                "browser.download",
                "R2",
                True,
                "destination_file_digest",
            ),
            (
                "publish",
                "browser.publish",
                "R2",
                True,
                "published_state_and_origin",
            ),
        )
        if actual != expected:
            raise ValueError("Browser action policy matrix changed")
        return self


class BrowserOriginAllowlistSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-origin-allowlist.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    revision: int = Field(ge=1)
    origins: tuple[str, ...] = Field(max_length=32)
    updated_by: Literal["local_user"] = "local_user"
    updated_at: AwareDatetime
    snapshot_digest: str = Field(pattern=DIGEST_PATTERN)

    @field_validator("origins")
    @classmethod
    def origins_are_normalized_unique_and_sorted(
        cls, value: tuple[str, ...]
    ) -> tuple[str, ...]:
        normalized = tuple(normalize_browser_origin(item) for item in value)
        if normalized != tuple(sorted(set(normalized))):
            raise ValueError("Browser origins must be unique and sorted")
        return normalized

    @model_validator(mode="after")
    def snapshot_digest_matches(self) -> Self:
        expected = sha256_digest(
            self.model_dump(mode="json", exclude={"snapshot_digest"})
        )
        if self.snapshot_digest != expected:
            raise ValueError("Browser origin allowlist digest changed")
        return self


class BrowserActionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-action-proposal.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    task_id: str = Field(min_length=1, max_length=128)
    step_id: str = Field(min_length=1, max_length=128)
    action: BrowserActionKind
    origin: str = Field(min_length=1, max_length=500)
    target_digest: str = Field(pattern=DIGEST_PATTERN)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    acceptance_mode: bool = False
    user_visible_window: Literal[True] = True
    semantic_dom_targeting: Literal[True] = True
    automated_login_requested: Literal[False] = False
    coordinate_targeting_requested: Literal[False] = False
    sensitive_data_kinds: tuple[BrowserSensitiveDataKind, ...] = Field(max_length=5)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)

    _origin_is_normalized = field_validator("origin")(normalize_browser_origin)

    @field_validator("sensitive_data_kinds")
    @classmethod
    def sensitive_data_kinds_are_unique(
        cls, value: tuple[BrowserSensitiveDataKind, ...]
    ) -> tuple[BrowserSensitiveDataKind, ...]:
        if len(value) != len(set(value)):
            raise ValueError("Browser sensitive data kinds must be unique")
        return value

    @model_validator(mode="after")
    def proposal_digest_matches(self) -> Self:
        expected = sha256_digest(self.model_dump(mode="json", exclude={"proposal_digest"}))
        if self.proposal_digest != expected:
            raise ValueError("Browser action proposal digest changed")
        return self


class BrowserActionApprovalBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-action-approval-binding.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    approval_id: str = Field(pattern=r"^apr_[0-9a-f]{32}$")
    preview_hash: str = Field(pattern=DIGEST_PATTERN)
    action: Literal[
        BrowserActionKind.SUBMIT,
        BrowserActionKind.UPLOAD,
        BrowserActionKind.DOWNLOAD,
        BrowserActionKind.PUBLISH,
    ]
    origin: str = Field(min_length=1, max_length=500)
    target_digest: str = Field(pattern=DIGEST_PATTERN)
    content_digest: str = Field(pattern=DIGEST_PATTERN)
    approved_by: Literal["local_user"] = "local_user"
    approved_at: AwareDatetime
    valid_until: AwareDatetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    _origin_is_normalized = field_validator("origin")(normalize_browser_origin)

    @model_validator(mode="after")
    def short_lived_and_digested(self) -> Self:
        if (
            self.valid_until <= self.approved_at
            or self.valid_until - self.approved_at > MAX_BROWSER_APPROVAL_VALIDITY
        ):
            raise ValueError("Browser action approval must expire within five minutes")
        preview_material = {
            "schema_version": "deskpilot.browser-action-approval-preview.v1",
            "proposal_digest": self.proposal_digest,
            "action": self.action.value,
            "origin": self.origin,
            "target_digest": self.target_digest,
            "content_digest": self.content_digest,
        }
        if self.preview_hash != sha256_digest(preview_material):
            raise ValueError("Browser action approval preview digest changed")
        expected = sha256_digest(self.model_dump(mode="json", exclude={"binding_digest"}))
        if self.binding_digest != expected:
            raise ValueError("Browser action approval binding digest changed")
        return self


class BrowserActionReadinessReport(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.browser-action-readiness.v1"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    proposal_digest: str = Field(pattern=DIGEST_PATTERN)
    allowlist_snapshot_digest: str = Field(pattern=DIGEST_PATTERN)
    approval_binding_digest: str | None = Field(default=None, pattern=DIGEST_PATTERN)
    action: BrowserActionKind
    origin_digest: str = Field(pattern=DIGEST_PATTERN)
    ready: bool
    violations: tuple[str, ...] = Field(max_length=20)
    checked_at: AwareDatetime
    browser_profile_created: Literal[False] = False
    browser_launched: Literal[False] = False
    desktop_application_control: Literal[False] = False
    network_access: Literal[False] = False
    action_executed: Literal[False] = False
    execution_authorized: Literal[False] = False

    @model_validator(mode="after")
    def readiness_matches_violations(self) -> Self:
        if self.ready == bool(self.violations):
            raise ValueError("Browser action readiness does not match its violations")
        if len(self.violations) != len(set(self.violations)):
            raise ValueError("Browser action readiness violations must be unique")
        return self
