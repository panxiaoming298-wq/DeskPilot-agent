import json
from datetime import UTC, datetime, timedelta
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
POLICY_PATH = (
    BACKEND_ROOT
    / "src"
    / "deskpilot"
    / "evaluations"
    / "phase115_provider_probe_policy_v1.yaml"
)
FIXED_NOW = datetime(2026, 8, 29, 8, tzinfo=UTC)

_BINDINGS = {
    "openai": (
        "openai-probe",
        "operator-confirmed-openai-model",
        "https://api.openai.com/v1",
        "DESKPILOT_CREDENTIAL_OPENAI_RESPONSES",
    ),
    "deepseek": (
        "deepseek-v4-flash",
        "deepseek-v4-flash",
        "https://api.deepseek.com",
        "DESKPILOT_CREDENTIAL_DEEPSEEK",
    ),
    "bailian": (
        "bailian-qwen38-max",
        "qwen3.8-max",
        "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
        "DESKPILOT_CREDENTIAL_BAILIAN",
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
    provider_id, model, base_url, credential_identifier = _BINDINGS[family]
    material: dict[str, Any] = {
        "schema_version": "deskpilot.provider-probe-operator-binding.v1",
        "policy_digest": bundle.policy_digest,
        "provider_family": family,
        "provider_id": provider_id,
        "exact_model": model,
        "base_url": base_url,
        "credential_ref": {
            "backend": "environment",
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
        "dashboard_hard_limit_confirmed": True,
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
        == "51b9b24743508f6546f37e3274e0a8f748b2424369c6c2e93b3449ab1472bb47"
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
    serialized = report.model_dump_json()
    assert binding.base_url not in serialized
    assert binding.credential_ref.identifier not in serialized


@pytest.mark.parametrize(
    ("family", "overrides", "violation"),
    (
        (
            "openai",
            {"base_url": "https://proxy.example.test/v1"},
            "BASE_URL_NOT_ALLOWED",
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
            "deepseek",
            {"dashboard_hard_limit_confirmed": False},
            "DASHBOARD_HARD_LIMIT_NOT_CONFIRMED",
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
            '"schema_version":"deskpilot.provider-probe-operator-binding.v1",'
            '"schema_version":',
            1,
        ),
        encoding="utf-8",
    )
    with pytest.raises(ProviderProbeAuthorizationError, match="duplicate JSON key"):
        load_provider_probe_binding(binding_path)


def test_cli_manifest_and_preflight_never_offer_a_live_run_command(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert provider_probe_gate_main(("manifest",)) == 0
    manifest = json.loads(capsys.readouterr().out)
    assert manifest["planned_aggregate_requests"] == 12
    assert manifest["maximum_aggregate_requests"] == 36
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
