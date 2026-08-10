from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.application.provider_catalog_store import (
    ProviderCatalogNotInitializedError,
    ProviderCatalogVersionConflictError,
)
from deskpilot.core.config import Settings
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.provider_config import (
    CredentialReference,
    FakeProviderConfig,
    OpenAICompatibleChatProviderConfig,
)
from deskpilot.domain.provider_management import (
    ProviderCatalogDefinition,
    ProviderCatalogDefinitionEntry,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.infrastructure.provider_catalog_repository import (
    SqlAlchemyProviderCatalogRepository,
)
from deskpilot.main import create_app
from deskpilot.model_providers.fake import FakeModelProvider

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "provider-catalog-persistence-token-12345"
CREDENTIAL_ID = "DESKPILOT_CREDENTIAL_PERSISTENCE_TEST"
CREDENTIAL_SECRET = "credential-value-never-persisted"
CLOUD_BASE_URL = "https://private-model-endpoint.example.test/v1"


def _definition(
    *entries: tuple[FakeModelProvider, bool],
    default_provider_id: str,
) -> ProviderCatalogDefinition:
    return ProviderCatalogDefinition(
        default_provider_id=default_provider_id,
        providers=tuple(
            ProviderCatalogDefinitionEntry(
                descriptor=provider.descriptor,
                enabled=enabled,
            )
            for provider, enabled in entries
        ),
    )


async def _repository(
    database_path: Path,
) -> tuple[Database, SqlAlchemyProviderCatalogRepository]:
    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await database.migrate()
    return database, SqlAlchemyProviderCatalogRepository(database)


@pytest.mark.asyncio
async def test_catalog_read_requires_a_completed_import(tmp_path: Path) -> None:
    database, repository = await _repository(tmp_path / "uninitialized.db")

    try:
        with pytest.raises(ProviderCatalogNotInitializedError):
            await repository.get_catalog()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_startup_import_is_sorted_versioned_and_idempotent(
    tmp_path: Path,
) -> None:
    database, repository = await _repository(tmp_path / "idempotent.db")
    alpha = FakeModelProvider(provider_id="alpha-provider")
    zulu = FakeModelProvider(provider_id="zulu-provider")
    definition = _definition(
        (zulu, False),
        (alpha, True),
        default_provider_id="alpha-provider",
    )

    try:
        first = await repository.import_definition(definition)
        second = await repository.import_definition(definition)
        loaded = await repository.get_catalog()
    finally:
        await database.dispose()

    assert first.catalog_version == 1
    assert second.catalog_version == 1
    assert second.imported_at == first.imported_at
    assert loaded == second
    assert [
        entry.descriptor.provider_id for entry in loaded.definition.providers
    ] == ["alpha-provider", "zulu-provider"]
    assert loaded.definition.providers[1].enabled is False


@pytest.mark.asyncio
async def test_changed_import_replaces_entries_and_enforces_expected_version(
    tmp_path: Path,
) -> None:
    database, repository = await _repository(tmp_path / "versioned.db")
    first_provider = FakeModelProvider(provider_id="first-provider")
    removed_provider = FakeModelProvider(provider_id="removed-provider")
    replacement = FakeModelProvider(
        provider_id="first-provider",
        display_name="Renamed Provider",
    )

    try:
        first = await repository.import_definition(
            _definition(
                (first_provider, True),
                (removed_provider, False),
                default_provider_id="first-provider",
            ),
            expected_version=0,
        )
        second = await repository.import_definition(
            _definition(
                (replacement, True),
                default_provider_id="first-provider",
            ),
            expected_version=first.catalog_version,
        )
        with pytest.raises(ProviderCatalogVersionConflictError) as raised:
            await repository.import_definition(
                _definition(
                    (first_provider, True),
                    default_provider_id="first-provider",
                ),
                expected_version=first.catalog_version,
            )
        loaded = await repository.get_catalog()
    finally:
        await database.dispose()

    assert second.catalog_version == 2
    assert raised.value.expected_version == 1
    assert raised.value.actual_version == 2
    assert loaded.catalog_version == 2
    assert len(loaded.definition.providers) == 1
    assert loaded.definition.providers[0].descriptor.display_name == "Renamed Provider"


def test_app_restart_uses_persisted_runtime_config_after_initial_seed(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    def settings(
        providers: tuple[FakeProviderConfig, ...] = (),
    ) -> Settings:
        return Settings(
            _env_file=None,
            database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
            session_token=SecretStr(TEST_TOKEN),
            cors_origins=[TEST_ORIGIN],
            model_providers=providers,
        )

    with TestClient(create_app(settings()), headers=headers) as client:
        first = client.get("/api/v1/model-providers").json()
    with TestClient(create_app(settings()), headers=headers) as client:
        unchanged = client.get("/api/v1/model-providers").json()
    with TestClient(
        create_app(
            settings(
                (
                    FakeProviderConfig(
                        display_name="Renamed Persisted Fake",
                    ),
                )
            )
        ),
        headers=headers,
    ) as client:
        changed = client.get("/api/v1/model-providers").json()

    assert first["catalog_version"] == 1
    assert unchanged["catalog_version"] == 1
    assert unchanged["imported_at"] == first["imported_at"]
    assert changed["catalog_version"] == 1
    assert changed["imported_at"] == first["imported_at"]
    assert changed["providers"][0]["descriptor"]["display_name"] == (
        "DeskPilot Fake Model"
    )


def test_database_never_contains_endpoint_credential_identifier_or_secret(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "secret-free.db"
    cloud = OpenAICompatibleChatProviderConfig(
        provider_id="private-cloud",
        display_name="Private Cloud",
        model="private-model",
        base_url=CLOUD_BASE_URL,
        location=ModelLocation.CLOUD,
        credential_ref=CredentialReference(identifier=CREDENTIAL_ID),
    )
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        model_providers=(FakeProviderConfig(), cloud),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(
        create_app(
            settings,
            credential_resolver=EnvironmentCredentialResolver(
                {CREDENTIAL_ID: CREDENTIAL_SECRET}
            ),
        ),
        headers=headers,
    ) as client:
        response = client.get("/api/v1/model-providers")

    database_bytes = database_path.read_bytes()
    assert response.status_code == 200
    assert response.json()["catalog_version"] == 1
    assert CLOUD_BASE_URL.encode() not in database_bytes
    assert CREDENTIAL_ID.encode() not in database_bytes
    assert CREDENTIAL_SECRET.encode() not in database_bytes
    assert CLOUD_BASE_URL not in response.text
    assert CREDENTIAL_ID not in response.text
    assert CREDENTIAL_SECRET not in response.text
