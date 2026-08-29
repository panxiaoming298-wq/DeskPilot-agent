"""Frozen authorization contracts for network-free Provider probe readiness."""

from datetime import datetime, timedelta
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN, ModelProtocol
from deskpilot.domain.phase107_calibrations import REVIEWER_PATTERN
from deskpilot.domain.provider_config import CredentialReference

ProviderProbeFamily = Literal["openai", "deepseek", "bailian"]
ProviderProbeCurrency = Literal["USD", "CNY"]
ProviderProbeBaseUrlPolicy = Literal[
    "openai_public_v1",
    "deepseek_public_responses",
    "bailian_beijing_workspace_responses",
]
ProviderProbeCostControlMode = Literal[
    "openai_project_hard_limit",
    "openai_application_envelope",
    "deepseek_prepaid_balance",
    "bailian_billing_alert",
]

MAX_PROVIDER_PROBE_BINDING_VALIDITY = timedelta(hours=24)


class ProviderProbeCasePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: Literal[
        "public-strict-json-nonstream-v1",
        "public-strict-json-stream-v1",
    ]
    transport: Literal["nonstream", "stream"]
    output_mode: Literal["strict_json_schema"] = "strict_json_schema"
    repeat_count: Literal[2] = 2
    maximum_input_characters: Literal[4096] = 4096
    maximum_output_tokens: Literal[256] = 256
    store_response: Literal[False] = False
    tools_enabled: Literal[False] = False
    external_retrieval_enabled: Literal[False] = False
    data_class: Literal["public_synthetic"] = "public_synthetic"
    contains_repository_content: Literal[False] = False


class ProviderProbeCostControlPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_modes: tuple[ProviderProbeCostControlMode, ...] = Field(
        min_length=1,
        max_length=2,
    )
    provider_hard_limit_preferred: bool
    prepaid_balance_check_required: bool
    billing_alert_required: bool
    billing_delay_acknowledgement_required: bool
    free_quota_stop_if_available_recommended: bool
    dedicated_probe_credential_required: Literal[True] = True
    application_budget_envelope_required: Literal[True] = True


class ProviderProbeBudgetPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    currency: ProviderProbeCurrency
    maximum_total_microunits: int = Field(ge=1)
    maximum_per_request_microunits: int = Field(ge=1)
    maximum_requests: int = Field(ge=1, le=100)
    automatic_retries: Literal[0] = 0
    hidden_retries: Literal[False] = False
    cost_control: ProviderProbeCostControlPolicy


class ProviderProbeProfilePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_family: ProviderProbeFamily
    protocol: Literal[ModelProtocol.OPENAI_RESPONSES] = ModelProtocol.OPENAI_RESPONSES
    recommended_model: str | None = Field(default=None, min_length=1, max_length=200)
    exact_model_confirmation_required: Literal[True] = True
    base_url_policy: ProviderProbeBaseUrlPolicy
    credential_backend: Literal["windows_credential_manager"] = (
        "windows_credential_manager"
    )
    credential_identifier: str
    budget: ProviderProbeBudgetPolicy


class ProviderProbeFutureRunnerGuards(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    serial_execution: Literal[True] = True
    stop_on_first_error: Literal[True] = True
    usage_required: Literal[True] = True
    request_and_response_bodies_logged: Literal[False] = False
    headers_logged: Literal[False] = False


class ProviderProbeExecutionBoundary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    network_access: Literal[False] = False
    resolves_credentials: Literal[False] = False
    real_model_capture: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    repository_content: Literal[False] = False
    full_116c_b: Literal[False] = False


class ProviderProbePolicy(BaseModel):
    """User-approved caps that do not themselves authorize a network request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-policy.v2"]
    policy_id: Literal["phase115-three-provider-public-probe"]
    version: Literal[2] = 2
    data_class: Literal["public_synthetic"] = "public_synthetic"
    cases: tuple[ProviderProbeCasePolicy, ...] = Field(min_length=2, max_length=2)
    profiles: tuple[ProviderProbeProfilePolicy, ...] = Field(min_length=3, max_length=3)
    planned_requests_per_provider: Literal[4] = 4
    planned_aggregate_requests: Literal[12] = 12
    maximum_aggregate_requests: Literal[36] = 36
    future_runner_guards: ProviderProbeFutureRunnerGuards
    execution_boundary: ProviderProbeExecutionBoundary

    @model_validator(mode="after")
    def exact_policy_matrix(self) -> Self:
        if tuple(item.case_id for item in self.cases) != (
            "public-strict-json-nonstream-v1",
            "public-strict-json-stream-v1",
        ):
            raise ValueError("Provider probe cases changed")
        if tuple(item.transport for item in self.cases) != ("nonstream", "stream"):
            raise ValueError("Provider probe transports changed")
        planned = sum(int(item.repeat_count) for item in self.cases)
        if planned != self.planned_requests_per_provider:
            raise ValueError("Provider probe planned request count changed")
        if tuple(item.provider_family for item in self.profiles) != (
            "openai",
            "deepseek",
            "bailian",
        ):
            raise ValueError("Provider probe profile order changed")
        expected = {
            "openai": (
                "gpt-5.6-luna",
                "openai_public_v1",
                "OPENAI_RESPONSES",
                "USD",
                5_000_000,
                250_000,
                16,
                (
                    "openai_project_hard_limit",
                    "openai_application_envelope",
                ),
                True,
                False,
                False,
                False,
                False,
            ),
            "deepseek": (
                "deepseek-v4-flash",
                "deepseek_public_responses",
                "DEEPSEEK",
                "USD",
                2_000_000,
                100_000,
                10,
                ("deepseek_prepaid_balance",),
                False,
                True,
                False,
                False,
                False,
            ),
            "bailian": (
                "qwen3.8-max",
                "bailian_beijing_workspace_responses",
                "BAILIAN",
                "CNY",
                20_000_000,
                2_000_000,
                10,
                ("bailian_billing_alert",),
                False,
                False,
                True,
                True,
                True,
            ),
        }
        for profile in self.profiles:
            control = profile.budget.cost_control
            actual = (
                profile.recommended_model,
                profile.base_url_policy,
                profile.credential_identifier,
                profile.budget.currency,
                profile.budget.maximum_total_microunits,
                profile.budget.maximum_per_request_microunits,
                profile.budget.maximum_requests,
                control.allowed_modes,
                control.provider_hard_limit_preferred,
                control.prepaid_balance_check_required,
                control.billing_alert_required,
                control.billing_delay_acknowledgement_required,
                control.free_quota_stop_if_available_recommended,
            )
            if actual != expected[profile.provider_family]:
                raise ValueError("Provider probe profile policy changed")
            if (
                self.planned_requests_per_provider > profile.budget.maximum_requests
                or self.planned_requests_per_provider
                * profile.budget.maximum_per_request_microunits
                > profile.budget.maximum_total_microunits
            ):
                raise ValueError("Provider probe plan exceeds its approved budget")
        if self.planned_aggregate_requests != (
            self.planned_requests_per_provider * len(self.profiles)
        ) or self.maximum_aggregate_requests != sum(
            item.budget.maximum_requests for item in self.profiles
        ):
            raise ValueError("Provider probe aggregate request count changed")
        return self


class ProviderProbeOperatorBinding(BaseModel):
    """Short-lived public configuration attestation; it contains no API secret."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-operator-binding.v2"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    exact_model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_048)
    credential_ref: CredentialReference
    currency: ProviderProbeCurrency
    maximum_total_microunits: int = Field(ge=1)
    maximum_per_request_microunits: int = Field(ge=1)
    maximum_requests: int = Field(ge=1, le=100)
    automatic_retries: Literal[0] = 0
    exact_model_confirmed: bool
    credential_presence_confirmed: bool
    base_url_key_pair_confirmed: bool
    cost_control_mode: ProviderProbeCostControlMode
    provider_hard_limit_enforcing: bool
    dedicated_probe_credential_confirmed: bool
    application_budget_envelope_confirmed: bool
    prepaid_balance_available_confirmed: bool
    prepaid_balance_checked_at: datetime | None = None
    billing_alert_confirmed: bool
    billing_delay_acknowledged: bool
    free_quota_stop_enabled: bool
    pricing_source_checked_at: datetime
    confirmed_by: str = Field(pattern=REVIEWER_PATTERN)
    confirmed_at: datetime
    valid_until: datetime
    binding_digest: str = Field(pattern=DIGEST_PATTERN)

    @model_validator(mode="after")
    def short_lived_and_digested(self) -> Self:
        timestamps = (
            self.pricing_source_checked_at,
            self.confirmed_at,
            self.valid_until,
        )
        if any(item.tzinfo is None for item in timestamps) or (
            self.prepaid_balance_checked_at is not None
            and self.prepaid_balance_checked_at.tzinfo is None
        ):
            raise ValueError("Provider probe binding timestamps must be timezone-aware")
        if (
            self.pricing_source_checked_at > self.confirmed_at
            or self.confirmed_at - self.pricing_source_checked_at
            > MAX_PROVIDER_PROBE_BINDING_VALIDITY
            or self.valid_until <= self.confirmed_at
            or self.valid_until - self.confirmed_at
            > MAX_PROVIDER_PROBE_BINDING_VALIDITY
        ):
            raise ValueError("Provider probe binding validity must stay within 24 hours")
        if self.binding_digest != sha256_digest(
            self.model_dump(mode="json", exclude={"binding_digest"})
        ):
            raise ValueError("Provider probe operator binding digest changed")
        return self


class ProviderProbeReadinessReport(BaseModel):
    """Sanitized offline result that deliberately grants no execution authority."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-readiness.v2"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    binding_digest: str = Field(pattern=DIGEST_PATTERN)
    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    model: str = Field(min_length=1, max_length=200)
    public_config_digest: str = Field(pattern=DIGEST_PATTERN)
    credential_reference_digest: str = Field(pattern=DIGEST_PATTERN)
    planned_request_count: Literal[4] = 4
    maximum_requests: int = Field(ge=1, le=100)
    currency: ProviderProbeCurrency
    maximum_total_microunits: int = Field(ge=1)
    maximum_per_request_microunits: int = Field(ge=1)
    planned_budget_envelope_microunits: int = Field(ge=1)
    cost_control_mode: ProviderProbeCostControlMode
    provider_hard_limit_enforcing: bool
    dedicated_probe_credential_confirmed: bool
    application_budget_envelope_confirmed: bool
    ready: bool
    violations: tuple[str, ...] = Field(max_length=20)
    checked_at: datetime
    network_access: Literal[False] = False
    credentials_resolved: Literal[False] = False
    real_model_capture: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False

    @model_validator(mode="after")
    def readiness_matches_violations(self) -> Self:
        if self.checked_at.tzinfo is None:
            raise ValueError("Provider probe readiness timestamp must be timezone-aware")
        if self.ready == bool(self.violations):
            raise ValueError("Provider probe readiness does not match its violations")
        if len(self.violations) != len(set(self.violations)):
            raise ValueError("Provider probe readiness violations must be unique")
        return self
