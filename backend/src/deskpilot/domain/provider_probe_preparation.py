"""Secret-free contracts for desktop Provider probe preparation."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from deskpilot.domain.agent_contracts import DIGEST_PATTERN
from deskpilot.domain.model_contracts import PROVIDER_ID_PATTERN
from deskpilot.domain.provider_config import WINDOWS_CREDENTIAL_ID_PATTERN
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeCostControlMode,
    ProviderProbeCurrency,
    ProviderProbeFamily,
    ProviderProbeOperatorBinding,
    ProviderProbeReadinessReport,
)


class ProviderProbePreparationProfile(BaseModel):
    """Frozen public policy projected for the desktop workflow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=100)
    exact_model: str = Field(min_length=1, max_length=200)
    suggested_base_url: str | None = Field(default=None, max_length=2_048)
    base_url_editable: bool
    credential_identifier: str = Field(pattern=WINDOWS_CREDENTIAL_ID_PATTERN)
    currency: ProviderProbeCurrency
    maximum_total_microunits: int = Field(ge=1)
    maximum_per_request_microunits: int = Field(ge=1)
    maximum_requests: int = Field(ge=1, le=100)
    planned_budget_envelope_microunits: int = Field(ge=1)
    allowed_cost_control_modes: tuple[ProviderProbeCostControlMode, ...] = Field(
        min_length=1,
        max_length=2,
    )
    prepaid_balance_check_required: bool
    billing_alert_required: bool
    billing_delay_acknowledgement_required: bool
    free_quota_stop_recommended: bool


class ProviderProbePreparationManifest(BaseModel):
    """Read-only desktop manifest; every execution authority remains false."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-preparation-manifest.v1"] = (
        "deskpilot.provider-probe-preparation-manifest.v1"
    )
    policy_id: Literal["phase115-three-provider-public-probe"]
    policy_digest: str = Field(pattern=DIGEST_PATTERN)
    data_class: Literal["public_synthetic"] = "public_synthetic"
    planned_requests_per_provider: Literal[4] = 4
    planned_aggregate_requests: Literal[12] = 12
    profiles: tuple[ProviderProbePreparationProfile, ...] = Field(
        min_length=3,
        max_length=3,
    )
    network_access: Literal[False] = False
    credentials_resolved: Literal[False] = False
    real_model_capture: Literal[False] = False
    production_admission: Literal[False] = False
    cloud_activation: Literal[False] = False
    full_116c_b: Literal[False] = False


class ProviderProbePreparationCommand(BaseModel):
    """Explicit local-user attestations used to mint one short-lived binding."""

    model_config = ConfigDict(extra="forbid")

    provider_family: ProviderProbeFamily
    provider_id: str = Field(pattern=PROVIDER_ID_PATTERN)
    exact_model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_048)
    credential_identifier: str = Field(pattern=WINDOWS_CREDENTIAL_ID_PATTERN)
    cost_control_mode: ProviderProbeCostControlMode
    exact_model_confirmed: bool
    credential_presence_confirmed: bool
    base_url_key_pair_confirmed: bool
    provider_hard_limit_enforcing: bool = False
    dedicated_probe_credential_confirmed: bool
    application_budget_envelope_confirmed: bool
    prepaid_balance_available_confirmed: bool = False
    billing_alert_confirmed: bool = False
    billing_delay_acknowledged: bool = False
    free_quota_stop_enabled: bool = False
    pricing_source_confirmed: Literal[True]


class ProviderProbePreparationResult(BaseModel):
    """Downloadable binding plus a sanitized network-free readiness report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["deskpilot.provider-probe-preparation-result.v1"] = (
        "deskpilot.provider-probe-preparation-result.v1"
    )
    binding: ProviderProbeOperatorBinding
    readiness: ProviderProbeReadinessReport
    readiness_report_digest: str = Field(pattern=DIGEST_PATTERN)
    live_permit_created: Literal[False] = False
    network_access: Literal[False] = False
