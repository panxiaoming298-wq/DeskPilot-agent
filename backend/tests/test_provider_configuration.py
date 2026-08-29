import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError

from deskpilot.application.credential_resolver import CredentialNotFoundError
from deskpilot.application.model_gateway import UnknownModelProviderError
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelLocation, ModelProtocol
from deskpilot.domain.provider_config import (
    CredentialReference,
    FakeProviderConfig,
    OpenAICompatibleChatProviderConfig,
    OpenAICompatibleResponsesProviderConfig,
)
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.main import create_app
from deskpilot.model_providers.factory import create_configured_model_providers

CLOUD_CREDENTIAL_ID = "DESKPILOT_CREDENTIAL_CLOUD_CHAT"
CLOUD_SECRET = "cloud-secret-must-never-be-serialized"


def cloud_config(*, enabled: bool = True) -> OpenAICompatibleChatProviderConfig:
    return OpenAICompatibleChatProviderConfig(
        provider_id="cloud-chat",
        display_name="Cloud Chat",
        model="cloud-model",
        base_url="https://models.example.test/v1",
        location=ModelLocation.CLOUD,
        credential_ref=CredentialReference(identifier=CLOUD_CREDENTIAL_ID),
        enabled=enabled,
        supports_strict_json_schema=True,
        max_context_tokens=128_000,
    )


def local_ollama_config() -> OpenAICompatibleChatProviderConfig:
    return OpenAICompatibleChatProviderConfig(
        provider_id="ollama-local",
        display_name="Local Ollama OpenAI API",
        model="local-model",
        base_url="http://127.0.0.1:11434/v1",
        location=ModelLocation.LOCAL,
        supports_strict_json_schema=False,
        max_context_tokens=32_768,
    )


def disabled_responses_config(
    provider_id: str,
    model: str,
    base_url: str,
    credential_id: str,
) -> OpenAICompatibleResponsesProviderConfig:
    return OpenAICompatibleResponsesProviderConfig(
        enabled=False,
        provider_id=provider_id,
        display_name=f"Disabled {provider_id}",
        model=model,
        base_url=base_url,
        location=ModelLocation.CLOUD,
        credential_ref=CredentialReference(identifier=credential_id),
        max_context_tokens=1_000_000,
    )


def test_legacy_empty_catalog_keeps_default_fake_provider() -> None:
    settings = Settings(_env_file=None)
    providers = create_configured_model_providers(
        settings,
        EnvironmentCredentialResolver({}),
    )

    assert len(providers) == 1
    assert providers[0].descriptor.provider_id == "fake-local"
    assert providers[0].descriptor.protocol is ModelProtocol.FAKE
    assert providers[0].descriptor.location is ModelLocation.LOCAL


def test_multi_provider_catalog_builds_fake_local_and_cloud_without_secret_leak() -> None:
    settings = Settings(
        _env_file=None,
        model_providers=(
            FakeProviderConfig(),
            local_ollama_config(),
            cloud_config(),
        ),
    )
    resolver = EnvironmentCredentialResolver(
        {CLOUD_CREDENTIAL_ID: CLOUD_SECRET}
    )

    providers = create_configured_model_providers(settings, resolver)
    descriptors = {provider.descriptor.provider_id: provider.descriptor for provider in providers}

    assert set(descriptors) == {"fake-local", "ollama-local", "cloud-chat"}
    assert descriptors["ollama-local"].location is ModelLocation.LOCAL
    assert descriptors["ollama-local"].protocol is ModelProtocol.OPENAI_COMPATIBLE_CHAT
    assert descriptors["cloud-chat"].location is ModelLocation.CLOUD
    assert descriptors["cloud-chat"].capabilities.strict_json_schema is True
    assert CLOUD_SECRET not in settings.model_dump_json()
    assert CLOUD_SECRET not in "".join(
        descriptor.model_dump_json() for descriptor in descriptors.values()
    )


def test_environment_credential_resolver_returns_secretstr_and_sanitizes_failure() -> None:
    reference = CredentialReference(identifier=CLOUD_CREDENTIAL_ID)
    resolver = EnvironmentCredentialResolver(
        {CLOUD_CREDENTIAL_ID: CLOUD_SECRET}
    )

    secret = resolver.resolve(reference)

    assert isinstance(secret, SecretStr)
    assert secret.get_secret_value() == CLOUD_SECRET
    assert CLOUD_SECRET not in repr(secret)

    missing = EnvironmentCredentialResolver({})
    with pytest.raises(CredentialNotFoundError) as raised:
        missing.resolve(reference)
    assert raised.value.code == "CREDENTIAL_NOT_FOUND"
    assert raised.value.credential_id == CLOUD_CREDENTIAL_ID
    assert CLOUD_SECRET not in str(raised.value)


def test_settings_parses_secret_free_provider_catalog_from_json_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = [
        {"kind": "fake", "provider_id": "fake-local"},
        {
            "kind": "openai_compatible_chat",
            "provider_id": "ollama-local",
            "display_name": "Local Ollama",
            "model": "local-model",
            "base_url": "http://localhost:11434/v1",
            "location": "local",
        },
        {
            "kind": "openai_compatible_chat",
            "provider_id": "cloud-chat",
            "display_name": "Cloud Chat",
            "model": "cloud-model",
            "base_url": "https://models.example.test/v1",
            "location": "cloud",
            "credential_ref": {
                "backend": "environment",
                "identifier": CLOUD_CREDENTIAL_ID,
            },
        },
    ]
    monkeypatch.setenv("DESKPILOT_MODEL_PROVIDERS", json.dumps(catalog))

    settings = Settings(_env_file=None)

    assert [provider.provider_id for provider in settings.model_providers] == [
        "fake-local",
        "ollama-local",
        "cloud-chat",
    ]
    serialized = settings.model_dump_json()
    assert CLOUD_CREDENTIAL_ID in serialized
    assert CLOUD_SECRET not in serialized


def test_disabled_deepseek_and_bailian_responses_profiles_parse_without_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    catalog = [
        {"kind": "fake", "provider_id": "fake-local"},
        disabled_responses_config(
            "deepseek-v4-flash",
            "deepseek-v4-flash",
            "https://api.deepseek.com",
            "DESKPILOT_CREDENTIAL_DEEPSEEK",
        ).model_dump(mode="json"),
        disabled_responses_config(
            "bailian-qwen38-max",
            "qwen3.8-max",
            "https://workspace.cn-beijing.maas.aliyuncs.com/compatible-mode/v1",
            "DESKPILOT_CREDENTIAL_BAILIAN",
        ).model_dump(mode="json"),
    ]
    monkeypatch.setenv("DESKPILOT_MODEL_PROVIDERS", json.dumps(catalog))

    settings = Settings(_env_file=None)
    providers = create_configured_model_providers(
        settings,
        EnvironmentCredentialResolver({}),
    )

    assert [provider.descriptor.provider_id for provider in providers] == [
        "fake-local"
    ]
    configured = {item.provider_id: item for item in settings.model_providers}
    assert configured["deepseek-v4-flash"].kind == "openai_compatible_responses"
    assert configured["bailian-qwen38-max"].kind == "openai_compatible_responses"


def test_settings_parses_role_routing_and_pricing_policy_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = {
        "role_routes": [
            {
                "role": "planner",
                "provider_ids": ["cloud-chat", "fake-local"],
                "strategy": "latency_aware",
            }
        ],
        "provider_pricing": [
            {
                "provider_id": "cloud-chat",
                "input_micros_per_million_tokens": 500_000,
                "output_micros_per_million_tokens": 1_500_000,
            }
        ],
        "default_max_attempts": 3,
        "default_retry_delay_budget_seconds": 8,
        "default_task_cost_budget_micros": 25_000,
    }
    monkeypatch.setenv("DESKPILOT_MODEL_GATEWAY_POLICY", json.dumps(policy))

    settings = Settings(_env_file=None)

    assert settings.model_gateway_policy.role_routes[0].role == "planner"
    assert settings.model_gateway_policy.role_routes[0].strategy == "latency_aware"
    assert settings.model_gateway_policy.default_max_attempts == 3
    assert (
        settings.model_gateway_policy.provider_pricing[0]
        .output_micros_per_million_tokens
        == 1_500_000
    )


@pytest.mark.parametrize(
    "config",
    [
        {
            "provider_id": "cloud-http",
            "display_name": "Cloud HTTP",
            "model": "model",
            "base_url": "http://models.example.test/v1",
            "location": "cloud",
            "credential_ref": {"identifier": CLOUD_CREDENTIAL_ID},
        },
        {
            "provider_id": "cloud-no-credential",
            "display_name": "Cloud Missing Credential",
            "model": "model",
            "base_url": "https://models.example.test/v1",
            "location": "cloud",
        },
        {
            "provider_id": "fake-local-name",
            "display_name": "Unverifiable Local DNS",
            "model": "model",
            "base_url": "https://modelbox.local/v1",
            "location": "local",
        },
        {
            "provider_id": "public-as-local",
            "display_name": "Public IP",
            "model": "model",
            "base_url": "https://8.8.8.8/v1",
            "location": "local",
        },
        {
            "provider_id": "private-without-opt-in",
            "display_name": "Private IP",
            "model": "model",
            "base_url": "http://192.168.1.20:11434/v1",
            "location": "local",
        },
    ],
)
def test_provider_config_rejects_unsafe_endpoint_or_credential_shapes(
    config: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        OpenAICompatibleChatProviderConfig.model_validate(config)


def test_private_network_endpoint_requires_explicit_opt_in() -> None:
    provider = OpenAICompatibleChatProviderConfig(
        provider_id="lan-model",
        display_name="Trusted LAN Model",
        model="local-model",
        base_url="http://192.168.1.20:11434/v1",
        location=ModelLocation.LOCAL,
        allow_private_network=True,
    )

    assert provider.allow_private_network is True


def test_config_models_reject_plaintext_secret_and_broad_environment_reference() -> None:
    with pytest.raises(ValidationError):
        CredentialReference(identifier="OPENAI_API_KEY")

    with pytest.raises(ValidationError):
        OpenAICompatibleChatProviderConfig.model_validate(
            {
                "provider_id": "cloud-chat",
                "display_name": "Cloud Chat",
                "model": "model",
                "base_url": "https://models.example.test/v1",
                "location": "cloud",
                "credential_ref": {"identifier": CLOUD_CREDENTIAL_ID},
                "api_key": CLOUD_SECRET,
            }
        )


def test_settings_rejects_duplicate_provider_ids() -> None:
    with pytest.raises(ValidationError):
        Settings(
            _env_file=None,
            model_providers=(
                FakeProviderConfig(),
                FakeProviderConfig(display_name="Duplicate"),
            ),
        )


def test_disabled_provider_does_not_require_credential_resolution() -> None:
    settings = Settings(
        _env_file=None,
        model_providers=(FakeProviderConfig(), cloud_config(enabled=False)),
    )

    providers = create_configured_model_providers(
        settings,
        EnvironmentCredentialResolver({}),
    )

    assert [provider.descriptor.provider_id for provider in providers] == [
        "fake-local"
    ]


def test_missing_configured_default_provider_fails_on_first_bootstrap(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'missing-default.db').as_posix()}",
        model_default_provider_id="missing-provider",
        model_providers=(FakeProviderConfig(),),
    )

    with pytest.raises(UnknownModelProviderError):
        with TestClient(
            create_app(
                settings,
                credential_resolver=EnvironmentCredentialResolver({}),
            )
        ):
            pass


def test_disabled_default_provider_fails_without_resolving_its_credential(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'disabled-default.db').as_posix()}",
        model_default_provider_id="cloud-chat",
        model_providers=(FakeProviderConfig(), cloud_config(enabled=False)),
    )

    with pytest.raises(UnknownModelProviderError):
        with TestClient(
            create_app(
                settings,
                credential_resolver=EnvironmentCredentialResolver({}),
            )
        ):
            pass


def test_app_composition_registers_multiple_configured_providers(
    tmp_path: Path,
) -> None:
    session_token = "provider-config-test-session-token"
    origin = "http://127.0.0.1:5173"
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'providers.db').as_posix()}",
        session_token=SecretStr(session_token),
        cors_origins=[origin],
        model_default_provider_id="fake-local",
        model_providers=(
            FakeProviderConfig(),
            local_ollama_config(),
            cloud_config(),
        ),
    )
    app = create_app(
        settings,
        credential_resolver=EnvironmentCredentialResolver(
            {CLOUD_CREDENTIAL_ID: CLOUD_SECRET}
        ),
    )
    headers = {
        "Authorization": f"Bearer {session_token}",
        "Origin": origin,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(app, headers=headers) as client:
        response = client.get("/api/v1/health")
        descriptors = app.state.model_gateway.descriptors()

    assert response.status_code == 200
    assert response.json()["model_provider"] == "fake-local"
    assert [descriptor.provider_id for descriptor in descriptors] == [
        "cloud-chat",
        "fake-local",
        "ollama-local",
    ]
