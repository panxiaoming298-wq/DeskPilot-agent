from typing import Any

from fastapi.testclient import TestClient


def _command(
    family: str = "openai",
    *,
    overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    values: dict[str, tuple[str, str, str, str]] = {
        "openai": (
            "openai-responses",
            "gpt-5.6-luna",
            "https://api.openai.com/v1",
            "OPENAI_RESPONSES",
        ),
        "deepseek": (
            "deepseek-responses",
            "deepseek-v4-flash",
            "https://api.deepseek.com",
            "DEEPSEEK",
        ),
        "bailian": (
            "bailian-responses",
            "qwen3.8-max",
            "https://workspace-123.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "BAILIAN",
        ),
    }
    provider_id, model, base_url, credential_identifier = values[family]
    command: dict[str, Any] = {
        "provider_family": family,
        "provider_id": provider_id,
        "exact_model": model,
        "base_url": base_url,
        "credential_identifier": credential_identifier,
        "cost_control_mode": {
            "openai": "openai_application_envelope",
            "deepseek": "deepseek_prepaid_balance",
            "bailian": "bailian_billing_alert",
        }[family],
        "exact_model_confirmed": True,
        "credential_presence_confirmed": True,
        "base_url_key_pair_confirmed": True,
        "provider_hard_limit_enforcing": False,
        "dedicated_probe_credential_confirmed": True,
        "application_budget_envelope_confirmed": True,
        "prepaid_balance_available_confirmed": family == "deepseek",
        "billing_alert_confirmed": family == "bailian",
        "billing_delay_acknowledged": family == "bailian",
        "free_quota_stop_enabled": False,
        "pricing_source_confirmed": True,
    }
    command.update(overrides or {})
    return command


def test_manifest_projects_three_profiles_without_execution_authority(
    client: TestClient,
) -> None:
    response = client.get("/api/v1/model-providers/probe-preparation")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["planned_requests_per_provider"] == 4
    assert payload["planned_aggregate_requests"] == 12
    assert [item["provider_family"] for item in payload["profiles"]] == [
        "openai",
        "deepseek",
        "bailian",
    ]
    assert payload["profiles"][0]["credential_identifier"] == "OPENAI_RESPONSES"
    assert payload["profiles"][2]["suggested_base_url"] is None
    assert payload["profiles"][2]["base_url_editable"] is True
    for boundary in (
        "network_access",
        "credentials_resolved",
        "real_model_capture",
        "production_admission",
        "cloud_activation",
        "full_116c_b",
    ):
        assert payload[boundary] is False


def test_preflight_mints_downloadable_binding_without_live_permit(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/model-providers/probe-preparation:preflight",
        json=_command(),
    )

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["readiness"]["ready"] is True
    assert payload["readiness"]["violations"] == []
    assert payload["binding"]["confirmed_by"] == "reviewer_local_owner"
    assert payload["binding"]["automatic_retries"] == 0
    assert payload["binding"]["credential_ref"] == {
        "backend": "windows_credential_manager",
        "identifier": "OPENAI_RESPONSES",
    }
    assert payload["live_permit_created"] is False
    assert payload["network_access"] is False
    serialized = response.text.lower()
    assert "api key" not in serialized
    assert "secret" not in serialized
    assert "authorization" not in serialized


def test_preflight_reports_public_configuration_drift_without_network(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/model-providers/probe-preparation:preflight",
        json=_command(
            "bailian",
            overrides={
                "base_url": "https://coding-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            },
        ),
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["readiness"]["ready"] is False
    assert "BASE_URL_NOT_ALLOWED" in payload["readiness"]["violations"]
    assert payload["readiness"]["network_access"] is False
    assert payload["readiness"]["credentials_resolved"] is False


def test_pricing_confirmation_is_required_before_binding_creation(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/model-providers/probe-preparation:preflight",
        json=_command(overrides={"pricing_source_confirmed": False}),
    )

    assert response.status_code == 422
    assert response.json()["code"] == "REQUEST_VALIDATION_FAILED"
