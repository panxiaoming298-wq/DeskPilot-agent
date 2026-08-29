"""Build short-lived Provider probe bindings without credentials or network access."""

from datetime import UTC, datetime, timedelta
from typing import Final

from deskpilot.application.provider_probe_authorization import (
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyBundle,
    ProviderProbePolicyLoader,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.provider_config import CredentialReference
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeOperatorBinding,
    ProviderProbeProfilePolicy,
)
from deskpilot.domain.provider_probe_preparation import (
    ProviderProbePreparationCommand,
    ProviderProbePreparationManifest,
    ProviderProbePreparationProfile,
    ProviderProbePreparationResult,
)

_LOCAL_OPERATOR: Final = "reviewer_local_owner"
_BINDING_VALIDITY: Final = timedelta(hours=24)
_PROFILE_PRESENTATION: Final = {
    "openai": (
        "openai-responses",
        "OpenAI",
        "https://api.openai.com/v1",
        False,
    ),
    "deepseek": (
        "deepseek-responses",
        "DeepSeek",
        "https://api.deepseek.com",
        False,
    ),
    "bailian": (
        "bailian-responses",
        "阿里云百炼",
        None,
        True,
    ),
}


class ProviderProbePreparationService:
    """Projects policy and runs the existing offline preflight only."""

    def __init__(self, bundle: ProviderProbePolicyBundle | None = None) -> None:
        self._bundle = bundle or ProviderProbePolicyLoader().load()

    def manifest(self) -> ProviderProbePreparationManifest:
        policy = self._bundle.policy
        return ProviderProbePreparationManifest(
            policy_id=policy.policy_id,
            policy_digest=self._bundle.policy_digest,
            profiles=tuple(
                self._manifest_profile(profile) for profile in policy.profiles
            ),
        )

    def prepare(
        self,
        command: ProviderProbePreparationCommand,
        *,
        now: datetime | None = None,
    ) -> ProviderProbePreparationResult:
        confirmed_at = now or datetime.now(UTC)
        if confirmed_at.tzinfo is None:
            raise ValueError("Provider probe preparation time must be timezone-aware")
        profile = self._profile(command.provider_family)
        material = {
            "schema_version": "deskpilot.provider-probe-operator-binding.v2",
            "policy_digest": self._bundle.policy_digest,
            "provider_family": command.provider_family,
            "provider_id": command.provider_id,
            "exact_model": command.exact_model,
            "base_url": command.base_url,
            "credential_ref": CredentialReference(
                backend="windows_credential_manager",
                identifier=command.credential_identifier,
            ).model_dump(mode="json"),
            "currency": profile.budget.currency,
            "maximum_total_microunits": profile.budget.maximum_total_microunits,
            "maximum_per_request_microunits": (
                profile.budget.maximum_per_request_microunits
            ),
            "maximum_requests": profile.budget.maximum_requests,
            "automatic_retries": 0,
            "exact_model_confirmed": command.exact_model_confirmed,
            "credential_presence_confirmed": command.credential_presence_confirmed,
            "base_url_key_pair_confirmed": command.base_url_key_pair_confirmed,
            "cost_control_mode": command.cost_control_mode,
            "provider_hard_limit_enforcing": command.provider_hard_limit_enforcing,
            "dedicated_probe_credential_confirmed": (
                command.dedicated_probe_credential_confirmed
            ),
            "application_budget_envelope_confirmed": (
                command.application_budget_envelope_confirmed
            ),
            "prepaid_balance_available_confirmed": (
                command.prepaid_balance_available_confirmed
            ),
            "prepaid_balance_checked_at": (
                confirmed_at if command.prepaid_balance_available_confirmed else None
            ),
            "billing_alert_confirmed": command.billing_alert_confirmed,
            "billing_delay_acknowledged": command.billing_delay_acknowledged,
            "free_quota_stop_enabled": command.free_quota_stop_enabled,
            "pricing_source_checked_at": confirmed_at,
            "confirmed_by": _LOCAL_OPERATOR,
            "confirmed_at": confirmed_at,
            "valid_until": confirmed_at + _BINDING_VALIDITY,
        }
        binding = ProviderProbeOperatorBinding.model_validate(
            {**material, "binding_digest": sha256_digest(material)}
        )
        readiness = ProviderProbeOfflinePreflight(self._bundle).run(
            binding,
            now=confirmed_at,
        )
        return ProviderProbePreparationResult(
            binding=binding,
            readiness=readiness,
            readiness_report_digest=sha256_digest(readiness),
        )

    def _profile(self, family: str) -> ProviderProbeProfilePolicy:
        for profile in self._bundle.policy.profiles:
            if profile.provider_family == family:
                return profile
        raise ValueError("Provider probe family is not frozen")

    def _manifest_profile(
        self,
        profile: ProviderProbeProfilePolicy,
    ) -> ProviderProbePreparationProfile:
        provider_id, display_name, base_url, editable = _PROFILE_PRESENTATION[
            profile.provider_family
        ]
        if profile.recommended_model is None:
            raise ValueError("Desktop Provider probe requires a frozen model")
        policy = self._bundle.policy
        return ProviderProbePreparationProfile(
            provider_family=profile.provider_family,
            provider_id=provider_id,
            display_name=display_name,
            exact_model=profile.recommended_model,
            suggested_base_url=base_url,
            base_url_editable=editable,
            credential_identifier=profile.credential_identifier,
            currency=profile.budget.currency,
            maximum_total_microunits=profile.budget.maximum_total_microunits,
            maximum_per_request_microunits=(
                profile.budget.maximum_per_request_microunits
            ),
            maximum_requests=profile.budget.maximum_requests,
            planned_budget_envelope_microunits=(
                policy.planned_requests_per_provider
                * profile.budget.maximum_per_request_microunits
            ),
            allowed_cost_control_modes=profile.budget.cost_control.allowed_modes,
            prepaid_balance_check_required=(
                profile.budget.cost_control.prepaid_balance_check_required
            ),
            billing_alert_required=profile.budget.cost_control.billing_alert_required,
            billing_delay_acknowledgement_required=(
                profile.budget.cost_control.billing_delay_acknowledgement_required
            ),
            free_quota_stop_recommended=(
                profile.budget.cost_control.free_quota_stop_if_available_recommended
            ),
        )
