"""Strict, network-free readiness checks for bounded Provider probes."""

import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from pydantic import ValidationError
from yaml.tokens import AliasToken, AnchorToken

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.provider_config import OpenAICompatibleResponsesProviderConfig
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeOperatorBinding,
    ProviderProbePolicy,
    ProviderProbeProfilePolicy,
    ProviderProbeReadinessReport,
)

MAX_PROVIDER_PROBE_POLICY_BYTES = 65_536
MAX_PROVIDER_PROBE_BINDING_BYTES = 65_536
_BAILIAN_WORKSPACE_HOST = re.compile(
    r"^[a-z0-9][a-z0-9-]{1,62}\.cn-beijing\.maas\.aliyuncs\.com$"
)


class ProviderProbeAuthorizationError(RuntimeError):
    code = "PROVIDER_PROBE_READINESS_REJECTED"


@dataclass(frozen=True, slots=True)
class ProviderProbePolicyBundle:
    policy: ProviderProbePolicy
    policy_digest: str


def _strict_yaml(path: Path) -> object:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProviderProbeAuthorizationError(
                "Provider probe policy must be one regular file"
            )
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_PROVIDER_PROBE_POLICY_BYTES:
            raise ProviderProbeAuthorizationError(
                "Provider probe policy is empty or exceeds its size limit"
            )
        text = payload.decode("utf-8")
        if any(isinstance(token, AnchorToken | AliasToken) for token in yaml.scan(text)):
            raise ProviderProbeAuthorizationError(
                "Provider probe policy YAML aliases are not allowed"
            )
        return yaml.safe_load(text)
    except ProviderProbeAuthorizationError:
        raise
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        raise ProviderProbeAuthorizationError(
            "Provider probe policy failed strict loading"
        ) from error


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ProviderProbeAuthorizationError(
                "Provider probe binding contains a duplicate JSON key"
            )
        result[key] = value
    return result


class ProviderProbePolicyLoader:
    def __init__(self, policy_path: Path | None = None) -> None:
        self._policy_path = policy_path or (
            Path(__file__).parents[1]
            / "evaluations"
            / "phase115_provider_probe_policy_v2.yaml"
        )

    def load(self) -> ProviderProbePolicyBundle:
        try:
            policy = ProviderProbePolicy.model_validate(_strict_yaml(self._policy_path))
        except ProviderProbeAuthorizationError:
            raise
        except ValidationError as error:
            raise ProviderProbeAuthorizationError(
                "Provider probe policy failed strict validation"
            ) from error
        return ProviderProbePolicyBundle(
            policy=policy,
            policy_digest=sha256_digest(policy.model_dump(mode="json")),
        )


def load_provider_probe_binding(path: Path) -> ProviderProbeOperatorBinding:
    try:
        if path.is_symlink() or not path.is_file():
            raise ProviderProbeAuthorizationError(
                "Provider probe binding must be one regular file"
            )
        payload = path.read_bytes()
        if not payload or len(payload) > MAX_PROVIDER_PROBE_BINDING_BYTES:
            raise ProviderProbeAuthorizationError(
                "Provider probe binding is empty or exceeds its size limit"
            )
        json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        return ProviderProbeOperatorBinding.model_validate_json(payload, strict=True)
    except ProviderProbeAuthorizationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValidationError) as error:
        raise ProviderProbeAuthorizationError(
            "Provider probe binding failed strict loading"
        ) from error


class ProviderProbeOfflinePreflight:
    """Validate public configuration and attestations without resolving a credential."""

    def __init__(self, bundle: ProviderProbePolicyBundle) -> None:
        self._bundle = bundle

    def run(
        self,
        binding: ProviderProbeOperatorBinding,
        *,
        now: datetime | None = None,
    ) -> ProviderProbeReadinessReport:
        checked_at = now or datetime.now(UTC)
        if checked_at.tzinfo is None:
            raise ProviderProbeAuthorizationError(
                "Provider probe readiness time must be timezone-aware"
            )
        profile = self._profile(binding.provider_family)
        violations: list[str] = []
        if binding.policy_digest != self._bundle.policy_digest:
            violations.append("POLICY_DIGEST_MISMATCH")
        if checked_at < binding.confirmed_at or checked_at > binding.valid_until:
            violations.append("BINDING_NOT_CURRENT")
        if checked_at - binding.pricing_source_checked_at > timedelta(hours=24):
            violations.append("PRICING_CONFIRMATION_STALE")
        if not binding.exact_model_confirmed:
            violations.append("EXACT_MODEL_NOT_CONFIRMED")
        if not binding.credential_presence_confirmed:
            violations.append("CREDENTIAL_PRESENCE_NOT_CONFIRMED")
        if not binding.base_url_key_pair_confirmed:
            violations.append("BASE_URL_KEY_PAIR_NOT_CONFIRMED")
        self._check_cost_control(profile, binding, checked_at, violations)
        if not self._model_is_allowed(profile, binding.exact_model):
            violations.append("MODEL_NOT_ALLOWED")
        if not self._base_url_is_allowed(profile, binding.base_url):
            violations.append("BASE_URL_NOT_ALLOWED")
        if not self._credential_reference_is_allowed(profile, binding):
            violations.append("CREDENTIAL_REFERENCE_NOT_ALLOWED")
        if (
            binding.currency != profile.budget.currency
            or binding.maximum_total_microunits
            != profile.budget.maximum_total_microunits
            or binding.maximum_per_request_microunits
            != profile.budget.maximum_per_request_microunits
            or binding.maximum_requests != profile.budget.maximum_requests
            or binding.automatic_retries != 0
        ):
            violations.append("BUDGET_POLICY_MISMATCH")

        try:
            public_config = OpenAICompatibleResponsesProviderConfig(
                enabled=False,
                provider_id=binding.provider_id,
                display_name=f"{binding.provider_family.title()} probe candidate",
                model=binding.exact_model,
                base_url=binding.base_url,
                location=ModelLocation.CLOUD,
                credential_ref=binding.credential_ref,
            )
            public_config_digest = sha256_digest(public_config.model_dump(mode="json"))
        except ValidationError:
            violations.append("PUBLIC_PROVIDER_CONFIG_INVALID")
            public_config_digest = "0" * 64

        return ProviderProbeReadinessReport(
            schema_version="deskpilot.provider-probe-readiness.v2",
            policy_digest=self._bundle.policy_digest,
            binding_digest=binding.binding_digest,
            provider_family=binding.provider_family,
            provider_id=binding.provider_id,
            model=binding.exact_model,
            public_config_digest=public_config_digest,
            credential_reference_digest=sha256_digest(binding.credential_ref),
            planned_request_count=self._bundle.policy.planned_requests_per_provider,
            maximum_requests=profile.budget.maximum_requests,
            currency=profile.budget.currency,
            maximum_total_microunits=profile.budget.maximum_total_microunits,
            maximum_per_request_microunits=(
                profile.budget.maximum_per_request_microunits
            ),
            planned_budget_envelope_microunits=(
                self._bundle.policy.planned_requests_per_provider
                * profile.budget.maximum_per_request_microunits
            ),
            cost_control_mode=binding.cost_control_mode,
            provider_hard_limit_enforcing=binding.provider_hard_limit_enforcing,
            dedicated_probe_credential_confirmed=(
                binding.dedicated_probe_credential_confirmed
            ),
            application_budget_envelope_confirmed=(
                binding.application_budget_envelope_confirmed
            ),
            ready=not violations,
            violations=tuple(violations),
            checked_at=checked_at,
        )

    def _profile(self, family: str) -> ProviderProbeProfilePolicy:
        for profile in self._bundle.policy.profiles:
            if profile.provider_family == family:
                return profile
        raise ProviderProbeAuthorizationError("Provider probe family is not frozen")

    @staticmethod
    def _check_cost_control(
        profile: ProviderProbeProfilePolicy,
        binding: ProviderProbeOperatorBinding,
        checked_at: datetime,
        violations: list[str],
    ) -> None:
        control = profile.budget.cost_control
        if binding.cost_control_mode not in control.allowed_modes:
            violations.append("COST_CONTROL_MODE_NOT_ALLOWED")
        if not binding.dedicated_probe_credential_confirmed:
            violations.append("DEDICATED_PROBE_CREDENTIAL_NOT_CONFIRMED")
        if not binding.application_budget_envelope_confirmed:
            violations.append("APPLICATION_BUDGET_ENVELOPE_NOT_CONFIRMED")

        if binding.cost_control_mode == "openai_project_hard_limit":
            if not binding.provider_hard_limit_enforcing:
                violations.append("PROVIDER_HARD_LIMIT_NOT_ENFORCING")
        elif binding.provider_hard_limit_enforcing:
            violations.append("PROVIDER_HARD_LIMIT_EVIDENCE_UNEXPECTED")

        if control.prepaid_balance_check_required:
            balance_checked_at = binding.prepaid_balance_checked_at
            if (
                not binding.prepaid_balance_available_confirmed
                or balance_checked_at is None
                or balance_checked_at > binding.confirmed_at
                or checked_at - balance_checked_at > timedelta(hours=24)
            ):
                violations.append("PREPAID_BALANCE_NOT_CURRENT")
        elif (
            binding.prepaid_balance_available_confirmed
            or binding.prepaid_balance_checked_at is not None
        ):
            violations.append("PREPAID_BALANCE_EVIDENCE_UNEXPECTED")

        if binding.billing_alert_confirmed != control.billing_alert_required:
            violations.append("BILLING_ALERT_EVIDENCE_MISMATCH")
        if (
            binding.billing_delay_acknowledged
            != control.billing_delay_acknowledgement_required
        ):
            violations.append("BILLING_DELAY_EVIDENCE_MISMATCH")
        if (
            binding.free_quota_stop_enabled
            and not control.free_quota_stop_if_available_recommended
        ):
            violations.append("FREE_QUOTA_STOP_EVIDENCE_UNEXPECTED")

    @staticmethod
    def _model_is_allowed(
        profile: ProviderProbeProfilePolicy,
        exact_model: str,
    ) -> bool:
        if profile.recommended_model is None:
            return exact_model.strip() == exact_model and "placeholder" not in exact_model.lower()
        return exact_model == profile.recommended_model

    @staticmethod
    def _base_url_is_allowed(
        profile: ProviderProbeProfilePolicy,
        base_url: str,
    ) -> bool:
        parsed = urlsplit(base_url)
        if parsed.username or parsed.password or parsed.port or parsed.query or parsed.fragment:
            return False
        normalized = base_url.rstrip("/")
        if profile.base_url_policy == "openai_public_v1":
            return normalized == "https://api.openai.com/v1"
        if profile.base_url_policy == "deepseek_public_responses":
            return normalized == "https://api.deepseek.com"
        workspace_id = parsed.hostname.split(".", maxsplit=1)[0] if parsed.hostname else ""
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and workspace_id not in {"coding-plan", "token-plan", "trial"}
            and _BAILIAN_WORKSPACE_HOST.fullmatch(parsed.hostname.lower()) is not None
            and parsed.path.rstrip("/") == "/compatible-mode/v1"
        )

    @staticmethod
    def _credential_reference_is_allowed(
        profile: ProviderProbeProfilePolicy,
        binding: ProviderProbeOperatorBinding,
    ) -> bool:
        reference = binding.credential_ref
        return (
            reference.backend == profile.credential_backend
            and reference.identifier == profile.credential_identifier
        )
