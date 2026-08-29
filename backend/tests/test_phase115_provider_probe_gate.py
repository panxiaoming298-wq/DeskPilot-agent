import json
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest

from deskpilot.application.provider_probe_authorization import (
    ProviderProbeAuthorizationError,
    ProviderProbeOfflinePreflight,
    ProviderProbePolicyLoader,
    load_provider_probe_binding,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.provider_probe_authorizations import (
    ProviderProbeOperatorBinding,
)
from deskpilot.phase115_provider_probe_gate import main as provider_probe_gate_main

BACKEND_ROOT = Path(__file__).parents[1]
POLICY_V1_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "phase115_provider_probe_policy_v1.yaml"
)
POLICY_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "phase115_provider_probe_policy_v2.yaml"
)
FIXED_NOW = datetime(2026, 8, 29, 8, tzinfo=UTC)

_BINDINGS = {
    "openai": (
        "openai-gpt56-luna",
        "gpt-5.6-luna",
        "https://api.openai.com/v1",
        "OPENAI_RESPONSES",
        "openai_application_envelope",
    ),
    "deepseek": (
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "https://api.deepseek.com",
        "DEEPSEEK",
        "deepseek_prepaid_balance",
    ),
    "bailian": (
        "bailian-qwen38-max",
        "qwen3.8-max",
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "BAILIAN",
        "bailian_billing_alert",
    ),
}


def _binding(
    family: str,
    *,
    overrides: dict[str, Any] | None = None,
) -> ProviderProbeOperatorBinding:
    bundle = ProviderProbePolicyLoader().load()
    profile = next(
        item for item in bundle.policy.profiles if item.provider_family == family
    )
    provider_id, model, base_url, credential_identifier, cost_control_mode = _BINDINGS[
        family
    ]
    material: dict[str, Any] = {
        "schema_version": "deskpilot.provider-probe-operator-binding.v2",
        "policy_digest": bundle.policy_digest,
        "provider_family": family,
        "provider_id": provider_id,
        "exact_model": model,
        "base_url": base_url,
        "credential_ref": {
            "backend": "windows_credential_manager",
            "identifier": credential_identifier,
        },
        "currency": profile.budget.currency,
        "maximum_total_microunits": profile.budget.maximum_total_microunits,
        "maximum_per_request_microunits": (
            profile.budget.maximum_per_request_microunits
        ),
        "maximum_requests": profile.budget.maximum_requests,
        "automatic_retries": 0,
        "exact_model_confirmed": True,
        "credential_presence_confirmed": True,
        "base_url_key_pair_confirmed": True,
        "cost_control_mode": cost_control_mode,
        "provider_hard_limit_enforcing": False,
        "dedicated_probe_credential_confirmed": True,
        "application_budget_envelope_confirmed": True,
        "prepaid_balance_available_confirmed": family == "deepseek",
        "prepaid_balance_checked_at": FIXED_NOW if family == "deepseek" else None,
        "billing_alert_confirmed": family == "bailian",
        "billing_delay_acknowledged": family == "bailian",
        "free_quota_stop_enabled": False,
        "pricing_source_checked_at": FIXED_NOW,
        "confirmed_by": "reviewer_operator_owner",
        "confirmed_at": FIXED_NOW,
        "valid_until": FIXED_NOW + timedelta(hours=12),
    }
    material.update(overrides or {})
    return ProviderProbeOperatorBinding.model_validate(
        {**material, "binding_digest": sha256_digest(material)}
    )


def test_policy_freezes_three_profiles_budget_and_non_execution_boundary() -> None:
    bundle = ProviderProbePolicyLoader().load()
    policy = bundle.policy

    assert (
        bundle.policy_digest
        == "0b221968240375def2ee886c4f73e937bf399db3f7009d86330892ed7c58a141"
    )
    normalized_v1 = POLICY_V1_PATH.read_bytes().replace(b"\r\n", b"\n")
    assert sha256(normalized_v1).hexdigest() == (
        "03b8fe6035d25d8bb9b8dd9c830412f7dcaf138714f1e76417ca938ba6183c78"
    )
    assert [item.provider_family for item in policy.profiles] == [
        "openai",
        "deepseek",
        "bailian",
    ]
    assert policy.planned_requests_per_provider == 4
    assert policy.planned_aggregate_requests == 12
    assert policy.maximum_aggregate_requests == 36
    assert [item.budget.maximum_requests for item in policy.profiles] == [16, 10, 10]
    assert [item.budget.automatic_retries for item in policy.profiles] == [0, 0, 0]
    assert [item.recommended_model for item in policy.profiles] == [
        "gpt-5.6-luna",
        "deepseek-v4-flash",
        "qwen3.8-max",
    ]
    assert [item.credential_backend for item in policy.profiles] == [
        "windows_credential_manager",
        "windows_credential_manager",
        "windows_credential_manager",
    ]
    assert [item.budget.cost_control.allowed_modes for item in policy.profiles] == [
        ("openai_project_hard_limit", "openai_application_envelope"),
        ("deepseek_prepaid_balance",),
        ("bailian_billing_alert",),
    ]
    assert policy.future_runner_guards.serial_execution is True
    assert policy.future_runner_guards.stop_on_first_error is True
    assert policy.future_runner_guards.usage_required is True
    assert not any(policy.execution_boundary.model_dump().values())


@pytest.mark.parametrize("family", ("openai", "deepseek", "bailian"))
def test_offline_preflight_accepts_exact_public_binding_without_resolving_secret(
    family: str,
) -> None:
    bundle = ProviderProbePolicyLoader().load()
    binding = _binding(family)

    report = ProviderProbeOfflinePreflight(bundle).run(binding, now=FIXED_NOW)

    assert report.ready is True
    assert report.violations == ()
    assert report.credentials_resolved is False
    assert report.network_access is False
    assert report.real_model_capture is False
    assert report.production_admission is False
    assert report.cloud_activation is False
    assert report.planned_budget_envelope_microunits in {400_000, 1_000_000, 8_000_000}
    assert report.dedicated_probe_credential_confirmed is True
    assert report.application_budget_envelope_confirmed is True
    serialized = report.model_dump_json()
    assert binding.base_url not in serialized
    assert binding.credential_ref.identifier not in serialized


def test_openai_project_hard_limit_mode_requires_and_accepts_enforcing_evidence() -> None:
    bundle = ProviderProbePolicyLoader().load()
    report = ProviderProbeOfflinePreflight(bundle).run(
        _binding(
            "openai",
            overrides={
                "cost_control_mode": "openai_project_hard_limit",
                "provider_hard_limit_enforcing": True,
            },
        ),
        now=FIXED_NOW,
    )

    assert report.ready is True
    assert report.provider_hard_limit_enforcing is True


@pytest.mark.parametrize(
    ("family", "overrides", "violation"),
    (
        (
            "openai",
            {"base_url": "https://proxy.example.test/v1"},
            "BASE_URL_NOT_ALLOWED",
        ),
        (
            "openai",
            {"exact_model": "gpt-5.6-terra"},
            "MODEL_NOT_ALLOWED",
        ),
        (
            "deepseek",
            {"exact_model": "deepseek-v4-pro"},
            "MODEL_NOT_ALLOWED",
        ),
        (
            "bailian",
            {
                "base_url": "https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"
            },
            "BASE_URL_NOT_ALLOWED",
        ),
        (
            "bailian",
            {
                "base_url": "https://workspace-123.ap-southeast-1.maas.aliyuncs.com/compatible-mode/v1"
            },
            "BASE_URL_NOT_ALLOWED",
        ),
        (
            "deepseek",
            {"dedicated_probe_credential_confirmed": False},
            "DEDICATED_PROBE_CREDENTIAL_NOT_CONFIRMED",
        ),
        (
            "openai",
            {"application_budget_envelope_confirmed": False},
            "APPLICATION_BUDGET_ENVELOPE_NOT_CONFIRMED",
        ),
        (
            "deepseek",
            {
                "prepaid_balance_available_confirmed": False,
                "prepaid_balance_checked_at": None,
            },
            "PREPAID_BALANCE_NOT_CURRENT",
        ),
        (
            "deepseek",
            {"prepaid_balance_checked_at": FIXED_NOW - timedelta(hours=25)},
            "PREPAID_BALANCE_NOT_CURRENT",
        ),
        (
            "bailian",
            {"billing_alert_confirmed": False},
            "BILLING_ALERT_EVIDENCE_MISMATCH",
        ),
        (
            "bailian",
            {"billing_delay_acknowledged": False},
            "BILLING_DELAY_EVIDENCE_MISMATCH",
        ),
        (
            "openai",
            {
                "cost_control_mode": "openai_project_hard_limit",
                "provider_hard_limit_enforcing": False,
            },
            "PROVIDER_HARD_LIMIT_NOT_ENFORCING",
        ),
        (
            "bailian",
            {
                "credential_ref": {
                    "backend": "environment",
                    "identifier": "DESKPILOT_CREDENTIAL_DEEPSEEK",
                }
            },
            "CREDENTIAL_REFERENCE_NOT_ALLOWED",
        ),
    ),
)
def test_offline_preflight_rejects_endpoint_model_budget_or_credential_drift(
    family: str,
    overrides: dict[str, Any],
    violation: str,
) -> None:
    bundle = ProviderProbePolicyLoader().load()
    report = ProviderProbeOfflinePreflight(bundle).run(
        _binding(family, overrides=overrides),
        now=FIXED_NOW,
    )

    assert report.ready is False
    assert violation in report.violations


def test_binding_expiry_and_stale_pricing_fail_closed() -> None:
    bundle = ProviderProbePolicyLoader().load()
    binding = _binding("openai")

    report = ProviderProbeOfflinePreflight(bundle).run(
        binding,
        now=FIXED_NOW + timedelta(hours=25),
    )

    assert report.ready is False
    assert report.violations == (
        "BINDING_NOT_CURRENT",
        "PRICING_CONFIRMATION_STALE",
    )


def test_strict_policy_and_binding_loaders_reject_alias_unknown_and_duplicate_keys(
    tmp_path: Path,
) -> None:
    policy_text = POLICY_PATH.read_text(encoding="utf-8")
    aliased = tmp_path / "aliased.yaml"
    aliased.write_text(
        policy_text.replace(
            "data_class: public_synthetic",
            "data_class: &data_class public_synthetic",
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeAuthorizationError, match="aliases"):
        ProviderProbePolicyLoader(aliased).load()

    unknown = tmp_path / "unknown.yaml"
    unknown.write_text(policy_text + "unexpected: true\n", encoding="utf-8")
    with pytest.raises(ProviderProbeAuthorizationError, match="strict validation"):
        ProviderProbePolicyLoader(unknown).load()

    binding = _binding("deepseek")
    binding_path = tmp_path / "duplicate.json"
    payload = binding.model_dump_json()
    binding_path.write_text(
        payload.replace(
            '"schema_version":',
            '"schema_version":"deskpilot.provider-probe-operator-binding.v2",'
            '"schema_version":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeAuthorizationError, match="duplicate JSON key"):
        load_provider_probe_binding(binding_path)

    v1_material = binding.model_dump(mode="json", exclude={"binding_digest"})
    v1_material["schema_version"] = "deskpilot.provider-probe-operator-binding.v1"
    v1_path = tmp_path / "obsolete-v1.json"
    v1_path.write_text(
        json.dumps(
            {**v1_material, "binding_digest": sha256_digest(v1_material)},
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeAuthorizationError, match="strict loading"):
        load_provider_probe_binding(v1_path)


def test_cli_manifest_and_preflight_never_offer_a_live_run_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert provider_probe_gate_main(("manifest",)) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["planned_aggregate_requests"] == 12
    assert manifest["maximum_aggregate_requests"] == 36
    assert manifest["profiles"][0]["recommended_model"] == "gpt-5.6-luna"
    assert manifest["profiles"][0]["credential_backend"] == (
        "windows_credential_manager"
    )
    assert manifest["future_runner_guards"]["serial_execution"] is True
    assert not any(manifest["execution_boundary"].values())

    binding = _binding("deepseek")
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(binding.model_dump_json(), encoding="utf-8")
    assert (
        provider_probe_gate_main(
            (
                "preflight",
                "--binding",
                str(binding_path),
                "--now",
                FIXED_NOW.isoformat(),
            )
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["ready"] is True
    assert report["network_access"] is False

    with pytest.raises(SystemExit):
        provider_probe_gate_main(("run",))
