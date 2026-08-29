from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from fastapi.testclient import TestClient
from pydantic import SecretStr

from deskpilot.core.config import Settings
from deskpilot.infrastructure.windows_credentials import (
    WINDOWS_CREDENTIAL_TARGET_PREFIX,
    WindowsCredentialApiError,
    WindowsCredentialManager,
)
from deskpilot.main import create_app

TEST_ORIGIN = "http://127.0.0.1:5173"
TEST_TOKEN = "managed-credential-api-session-token-12345"
IDENTIFIER = "OPENAI_RESPONSES"
SECRET = "test-secret-that-must-never-appear"


class XorProtector:
    scheme = "test_managed_credential_xor_v1"

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        mask = sum(context.encode("utf-8")) % 251 + 1
        return bytes(value ^ mask for value in plaintext)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        mask = sum(context.encode("utf-8")) % 251 + 1
        return bytearray(value ^ mask for value in payload)


class FakeWindowsCredentialApi:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.failure: WindowsCredentialApiError | None = None

    def read(self, target_name: str) -> bytearray | None:
        self._raise_failure("read")
        value = self.values.get(target_name)
        return None if value is None else bytearray(value)

    def write(self, target_name: str, credential_blob: bytearray) -> None:
        self._raise_failure("write")
        self.values[target_name] = bytes(credential_blob)

    def delete(self, target_name: str) -> bool:
        self._raise_failure("delete")
        return self.values.pop(target_name, None) is not None

    def _raise_failure(self, operation: str) -> None:
        if self.failure is not None and self.failure.operation == operation:
            raise self.failure


@contextmanager
def client(
    database_path: Path,
    api: FakeWindowsCredentialApi,
) -> Iterator[TestClient]:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
    )
    manager = WindowsCredentialManager(api)
    app = create_app(
        settings,
        credential_resolver=manager,
        managed_credential_store=manager,
        runtime_config_protector=XorProtector(),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }
    with TestClient(app, headers=headers) as test_client:
        yield test_client


def test_store_status_and_delete_are_secret_free(tmp_path: Path) -> None:
    api = FakeWindowsCredentialApi()
    path = f"/api/v1/model-providers/credentials/{IDENTIFIER}"
    target = f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{IDENTIFIER}"

    with client(tmp_path / "managed-credential.db", api) as test_client:
        missing = test_client.get(path)
        stored = test_client.put(path, json={"secret": SECRET})
        available = test_client.get(path)
        refused = test_client.delete(path)
        deleted = test_client.delete(
            path,
            headers={"X-DeskPilot-Credential-Confirmation": IDENTIFIER},
        )
        missing_again = test_client.get(path)

    serialized = "".join(
        response.text
        for response in (missing, stored, available, refused, deleted, missing_again)
    )
    assert missing.status_code == 200
    assert missing.json()["state"] == "missing"
    assert stored.status_code == 200
    assert stored.json()["state"] == "available"
    assert stored.headers["cache-control"] == "no-store"
    assert api.values.get(target) is None
    assert available.json()["state"] == "available"
    assert refused.status_code == 400
    assert refused.json()["code"] == "CREDENTIAL_DELETE_CONFIRMATION_REQUIRED"
    assert deleted.json() == {
        "schema_version": "deskpilot.managed-credential-status.v1",
        "backend": "windows_credential_manager",
        "identifier": IDENTIFIER,
        "state": "missing",
        "writable": True,
        "deleted": True,
    }
    assert missing_again.json()["state"] == "missing"
    assert SECRET not in serialized
    assert WINDOWS_CREDENTIAL_TARGET_PREFIX not in serialized


def test_delete_confirmation_mismatch_retains_credential(tmp_path: Path) -> None:
    api = FakeWindowsCredentialApi()
    path = f"/api/v1/model-providers/credentials/{IDENTIFIER}"
    target = f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{IDENTIFIER}"

    with client(tmp_path / "confirmation.db", api) as test_client:
        test_client.put(path, json={"secret": SECRET})
        response = test_client.delete(
            path,
            headers={"X-DeskPilot-Credential-Confirmation": "DEEPSEEK"},
        )

    assert response.status_code == 400
    assert api.values[target] == SECRET.encode()
    assert SECRET not in response.text


def test_backend_failures_and_invalid_identifiers_are_sanitized(
    tmp_path: Path,
) -> None:
    api = FakeWindowsCredentialApi()
    api.failure = WindowsCredentialApiError(operation="write", error_code=1312)

    with client(tmp_path / "failure.db", api) as test_client:
        failed = test_client.put(
            f"/api/v1/model-providers/credentials/{IDENTIFIER}",
            json={"secret": SECRET},
        )
        invalid = test_client.get(
            "/api/v1/model-providers/credentials/lowercase-key"
        )

    assert failed.status_code == 503
    assert failed.json()["code"] == "CREDENTIAL_BACKEND_OPERATION_FAILED"
    assert invalid.status_code == 422
    assert SECRET not in failed.text + invalid.text


def test_delete_confirmation_header_is_allowed_by_cors(tmp_path: Path) -> None:
    api = FakeWindowsCredentialApi()
    path = f"/api/v1/model-providers/credentials/{IDENTIFIER}"

    with client(tmp_path / "cors.db", api) as test_client:
        response = test_client.options(
            path,
            headers={
                "Access-Control-Request-Method": "DELETE",
                "Access-Control-Request-Headers": (
                    "authorization,x-deskpilot-client,"
                    "x-deskpilot-credential-confirmation"
                ),
            },
        )

    assert response.status_code == 200
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-deskpilot-credential-confirmation" in allowed


def test_managed_credential_api_is_unavailable_without_an_injected_store(
    tmp_path: Path,
) -> None:
    settings = Settings(
        _env_file=None,
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 'unavailable.db').as_posix()}",
        session_token=SecretStr(TEST_TOKEN),
        cors_origins=[TEST_ORIGIN],
    )
    app = create_app(
        settings,
        credential_resolver=WindowsCredentialManager(FakeWindowsCredentialApi()),
        runtime_config_protector=XorProtector(),
    )
    headers = {
        "Authorization": f"Bearer {TEST_TOKEN}",
        "Origin": TEST_ORIGIN,
        "X-DeskPilot-Client": "deskpilot-web-v1",
    }

    with TestClient(app, headers=headers) as test_client:
        response = test_client.get(
            f"/api/v1/model-providers/credentials/{IDENTIFIER}"
        )

    assert response.status_code == 503
    assert response.json()["code"] == "MANAGED_CREDENTIAL_STORE_UNAVAILABLE"
