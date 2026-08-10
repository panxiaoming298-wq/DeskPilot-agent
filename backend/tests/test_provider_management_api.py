from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.core.config import Settings
from deskpilot.domain.provider_config import FakeProviderConfig
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "provider-write-api-session-token-12345"
CREDENTIAL_ID = "DESKPILOT_CREDENTIAL_MANAGEMENT_API"
CREDENTIAL_SECRET = "provider-management-api-secret"
FIRST_ENDPOINT = "https://first-private-endpoint.example.invalid/v1"
SECOND_ENDPOINT = "https://second-private-endpoint.example.invalid/v1"


class XorProtector:
    scheme = "test_management_xor_v1"

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        mask = sum(context.encode("utf-8")) % 251 + 1
        return bytes(value ^ mask for value in plaintext)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        mask = sum(context.encode("utf-8")) % 251 + 1
        return bytearray(value ^ mask for value in payload)


def _settings(database_path: Path, *, renamed_seed: bool = False) -> Settings:
    providers = (
        (
            FakeProviderConfig(display_name="Ignored Renamed Seed"),
        )
        if renamed_seed
        else ()
    )
    return Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
        model_providers=providers,
    )


@contextmanager
def _client(
    database_path: Path,
    *,
    renamed_seed: bool = False,
    credentials: dict[str, str] | None = None,
) -> Iterator[TestClient]:
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    app = create_app(
        _settings(database_path, renamed_seed=renamed_seed),
        credential_resolver=EnvironmentCredentialResolver(credentials or {}),
        runtime_config_protector=XorProtector(),
    )
    with TestClient(app, headers=headers) as client:
        yield client


def _fake_body(provider_id: str = "second-fake") -> dict[str, object]:
    return {
        "kind": "fake",
        "enabled": True,
        "provider_id": provider_id,
        "display_name": "Second Fake",
        "model": "deskpilot-fake-v1",
        "delay_seconds": 0,
    }


def _cloud_body(*, base_url: str = FIRST_ENDPOINT) -> dict[str, object]:
    return {
        "kind": "openai_compatible_chat",
        "enabled": False,
        "provider_id": "managed-cloud",
        "display_name": "Managed Cloud",
        "model": "managed-model",
        "base_url": base_url,
        "location": "cloud",
        "credential_ref": {
            "backend": "environment",
            "identifier": CREDENTIAL_ID,
        },
    }


def _mutation_headers(version: int, key: str) -> dict[str, str]:
    return {
        "If-Match": f'"provider-catalog-v{version}"',
        "Idempotency-Key": key,
    }


def test_catalog_exposes_configuration_etag_and_requires_write_headers(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "headers.db"
    with _client(database_path) as client:
        listed = client.get("/api/v1/model-providers")
        missing_if_match = client.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers={"Idempotency-Key": "create-second-fake-0001"},
        )
        missing_idempotency = client.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers={"If-Match": '"provider-catalog-v1"'},
        )
        invalid_etag = client.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers=_mutation_headers(1, "create-second-fake-0002")
            | {"If-Match": "W/\"provider-catalog-v1\""},
        )

    assert listed.status_code == 200
    assert listed.headers["etag"] == '"provider-catalog-v1"'
    assert listed.headers["cache-control"] == "no-store"
    assert listed.headers["access-control-expose-headers"] == "ETag"
    assert missing_if_match.status_code == 428
    assert missing_if_match.json()["code"] == "IF_MATCH_REQUIRED"
    assert missing_idempotency.status_code == 400
    assert missing_idempotency.json()["code"] == "IDEMPOTENCY_KEY_REQUIRED"
    assert invalid_etag.status_code == 400
    assert invalid_etag.json()["code"] == "IF_MATCH_INVALID"


def test_create_replay_conflict_and_audit_are_secret_free(tmp_path: Path) -> None:
    database_path = tmp_path / "create.db"
    idempotency_key = "create-managed-cloud-0001"
    with _client(database_path) as client:
        created = client.post(
            "/api/v1/model-providers",
            json=_cloud_body(),
            headers=_mutation_headers(1, idempotency_key),
        )
        replayed = client.post(
            "/api/v1/model-providers",
            json=_cloud_body(),
            headers=_mutation_headers(1, idempotency_key),
        )
        conflicting_body = _cloud_body()
        conflicting_body["display_name"] = "Different Request"
        conflict = client.post(
            "/api/v1/model-providers",
            json=conflicting_body,
            headers=_mutation_headers(2, idempotency_key),
        )
        audit = client.get("/api/v1/model-providers/audit")

    database_bytes = database_path.read_bytes()
    assert created.status_code == 201
    assert created.headers["etag"] == '"provider-catalog-v2"'
    assert created.json()["catalog_version"] == 2
    assert created.json()["config_revision"] == 1
    assert created.json()["replayed"] is False
    assert replayed.status_code == 201
    assert replayed.json()["replayed"] is True
    assert replayed.json()["catalog_version"] == 2
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "IDEMPOTENCY_KEY_REUSED"
    assert [event["action"] for event in audit.json()["events"]] == [
        "created",
        "created",
    ]
    assert FIRST_ENDPOINT not in audit.text
    assert CREDENTIAL_ID not in audit.text
    assert FIRST_ENDPOINT.encode() not in database_bytes
    assert CREDENTIAL_ID.encode() not in database_bytes
    assert CREDENTIAL_SECRET.encode() not in database_bytes
    assert idempotency_key.encode() not in database_bytes


def test_hidden_runtime_update_increments_catalog_and_stale_etag_rolls_back(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "update.db"
    with _client(database_path) as client:
        created = client.post(
            "/api/v1/model-providers",
            json=_cloud_body(),
            headers=_mutation_headers(1, "create-cloud-update-0001"),
        )
        updated = client.put(
            "/api/v1/model-providers/managed-cloud",
            json=_cloud_body(base_url=SECOND_ENDPOINT),
            headers=_mutation_headers(2, "update-cloud-hidden-0001"),
        )
        stale = client.post(
            "/api/v1/model-providers/managed-cloud:disable",
            headers=_mutation_headers(2, "stale-disable-cloud-0001"),
        )
        listed = client.get("/api/v1/model-providers")
        audit = client.get(
            "/api/v1/model-providers/audit?provider_id=managed-cloud"
        )

    assert created.status_code == 201
    assert updated.status_code == 200
    assert updated.json()["catalog_version"] == 3
    assert updated.json()["config_revision"] == 2
    assert updated.headers["etag"] == '"provider-catalog-v3"'
    assert stale.status_code == 412
    assert stale.json()["code"] == "MODEL_PROVIDER_CATALOG_VERSION_CONFLICT"
    assert stale.json()["actual_version"] == 3
    assert listed.json()["catalog_version"] == 3
    changed_fields = [
        event["changed_fields"] for event in audit.json()["events"]
    ]
    assert "base_url" in changed_fields[0]
    assert "credential_ref" in changed_fields[0]
    assert changed_fields[1] == ["base_url"]
    database_bytes = database_path.read_bytes()
    assert FIRST_ENDPOINT.encode() not in database_bytes
    assert SECOND_ENDPOINT.encode() not in database_bytes


def test_dynamic_enable_default_disable_delete_reconfigures_gateway(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "lifecycle.db"
    with _client(database_path) as client:
        created = client.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers=_mutation_headers(1, "create-second-lifecycle-01"),
        )
        probed = client.get("/api/v1/model-providers/second-fake/health")
        made_default = client.post(
            "/api/v1/model-providers/second-fake:make-default",
            headers=_mutation_headers(2, "default-second-fake-0001"),
        )
        health = client.get("/api/v1/health")
        disabled = client.post(
            "/api/v1/model-providers/fake-local:disable",
            headers=_mutation_headers(3, "disable-old-fake-0001"),
        )
        deleted = client.delete(
            "/api/v1/model-providers/fake-local",
            headers=_mutation_headers(4, "delete-old-fake-000001"),
        )
        rejected_default_delete = client.delete(
            "/api/v1/model-providers/second-fake",
            headers=_mutation_headers(5, "delete-default-fake-0001"),
        )
        listed = client.get("/api/v1/model-providers")
        audit = client.get("/api/v1/model-providers/audit")

    assert created.status_code == 201
    assert probed.status_code == 200
    assert made_default.json()["catalog_version"] == 3
    assert health.json()["model_provider"] == "second-fake"
    assert disabled.json()["catalog_version"] == 4
    assert deleted.json()["catalog_version"] == 5
    assert rejected_default_delete.status_code == 409
    assert rejected_default_delete.json()["code"] == (
        "MODEL_PROVIDER_MANAGEMENT_CONFLICT"
    )
    assert listed.json()["default_provider_id"] == "second-fake"
    assert [
        provider["descriptor"]["provider_id"]
        for provider in listed.json()["providers"]
    ] == ["second-fake"]
    assert [event["action"] for event in audit.json()["events"]] == [
        "created",
        "created",
        "default_changed",
        "disabled",
        "deleted",
    ]


def test_missing_credential_prevents_enable_without_partial_commit(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "credential.db"
    with _client(database_path) as client:
        client.post(
            "/api/v1/model-providers",
            json=_cloud_body(),
            headers=_mutation_headers(1, "create-disabled-cloud-01"),
        )
        failed = client.post(
            "/api/v1/model-providers/managed-cloud:enable",
            headers=_mutation_headers(2, "enable-missing-credential"),
        )
        listed = client.get("/api/v1/model-providers")
        audit = client.get(
            "/api/v1/model-providers/audit?provider_id=managed-cloud"
        )

    assert failed.status_code == 409
    assert failed.json()["code"] == "CREDENTIAL_NOT_FOUND"
    assert listed.json()["catalog_version"] == 2
    managed = next(
        item
        for item in listed.json()["providers"]
        if item["descriptor"]["provider_id"] == "managed-cloud"
    )
    assert managed["enabled"] is False
    assert [event["action"] for event in audit.json()["events"]] == [
        "created"
    ]


def test_provider_delete_retains_referenced_credential(tmp_path: Path) -> None:
    database_path = tmp_path / "retention.db"
    with _client(database_path) as client:
        client.post(
            "/api/v1/model-providers",
            json=_cloud_body(),
            headers=_mutation_headers(1, "create-retained-cloud-01"),
        )
        deleted = client.delete(
            "/api/v1/model-providers/managed-cloud",
            headers=_mutation_headers(2, "delete-retained-cloud-01"),
        )
        audit = client.get(
            "/api/v1/model-providers/audit?provider_id=managed-cloud"
        )

    assert deleted.status_code == 200
    assert deleted.json()["credential_disposition"] == (
        "provider_deleted_credential_retained"
    )
    assert audit.json()["events"][-1]["credential_disposition"] == (
        "provider_deleted_credential_retained"
    )


def test_database_state_and_idempotency_replay_survive_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "restart.db"
    key = "create-persisted-second-01"
    with _client(database_path) as client:
        created = client.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers=_mutation_headers(1, key),
        )
        assert created.status_code == 201

    with _client(database_path, renamed_seed=True) as restarted:
        listed = restarted.get("/api/v1/model-providers")
        replayed = restarted.post(
            "/api/v1/model-providers",
            json=_fake_body(),
            headers=_mutation_headers(1, key),
        )

    assert listed.json()["catalog_version"] == 2
    by_id = {
        item["descriptor"]["provider_id"]: item
        for item in listed.json()["providers"]
    }
    assert by_id["fake-local"]["descriptor"]["display_name"] == (
        "DeskPilot Fake Model"
    )
    assert "second-fake" in by_id
    assert replayed.status_code == 201
    assert replayed.headers["etag"] == '"provider-catalog-v2"'
    assert replayed.json()["replayed"] is True
    assert replayed.json()["catalog_version"] == 2


def test_enabled_cloud_provider_can_be_created_without_network_probe(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "enabled-cloud.db"
    body = _cloud_body()
    body["enabled"] = True
    with _client(
        database_path,
        credentials={CREDENTIAL_ID: CREDENTIAL_SECRET},
    ) as client:
        created = client.post(
            "/api/v1/model-providers",
            json=body,
            headers=_mutation_headers(1, "create-enabled-cloud-0001"),
        )
        listed = client.get("/api/v1/model-providers")

    assert created.status_code == 201
    assert listed.status_code == 200
    managed = next(
        item
        for item in listed.json()["providers"]
        if item["descriptor"]["provider_id"] == "managed-cloud"
    )
    assert managed["enabled"] is True
    assert managed["cached_health"] is None
    assert FIRST_ENDPOINT not in created.text
    assert CREDENTIAL_ID not in created.text
    assert CREDENTIAL_SECRET not in created.text
