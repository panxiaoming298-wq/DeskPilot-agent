import sys
from io import StringIO

import pytest
from pydantic import SecretStr, ValidationError

from deskpilot.application.credential_resolver import (
    CredentialBackendUnavailableError,
    CredentialInvalidError,
    CredentialNotFoundError,
    CredentialOperationError,
)
from deskpilot.core.config import Settings
from deskpilot.credential_cli import run as run_credential_cli
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.provider_config import (
    CredentialReference,
    OpenAICompatibleChatProviderConfig,
)
from deskpilot.infrastructure.credential_resolvers import (
    CompositeCredentialResolver,
    create_default_credential_resolver,
)
from deskpilot.infrastructure.environment_credentials import (
    EnvironmentCredentialResolver,
)
from deskpilot.infrastructure.windows_credentials import (
    CRED_MAX_CREDENTIAL_BLOB_SIZE,
    WINDOWS_CREDENTIAL_TARGET_PREFIX,
    Win32CredentialApi,
    WindowsCredentialApiError,
    WindowsCredentialManager,
)
from deskpilot.model_providers.factory import create_configured_model_providers

WINDOWS_IDENTIFIER = "CLOUD_CHAT"
SECRET = "windows-manager-secret-must-not-leak"


class FakeWindowsCredentialApi:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.failure: WindowsCredentialApiError | None = None
        self.last_write_buffer: bytearray | None = None
        self.last_read_buffer: bytearray | None = None
        self.write_calls = 0

    def read(self, target_name: str) -> bytearray | None:
        self._raise_failure("read")
        value = self.values.get(target_name)
        if value is None:
            return None
        self.last_read_buffer = bytearray(value)
        return self.last_read_buffer

    def write(self, target_name: str, credential_blob: bytearray) -> None:
        self._raise_failure("write")
        self.write_calls += 1
        self.last_write_buffer = credential_blob
        self.values[target_name] = bytes(credential_blob)

    def delete(self, target_name: str) -> bool:
        self._raise_failure("delete")
        return self.values.pop(target_name, None) is not None

    def _raise_failure(self, operation: str) -> None:
        if self.failure is not None and self.failure.operation == operation:
            raise self.failure


def windows_reference() -> CredentialReference:
    return CredentialReference(
        backend="windows_credential_manager",
        identifier=WINDOWS_IDENTIFIER,
    )


def test_credential_reference_uses_backend_specific_identifier_namespaces() -> None:
    environment = CredentialReference(
        identifier="DESKPILOT_CREDENTIAL_CLOUD_CHAT"
    )
    windows = windows_reference()

    assert environment.backend == "environment"
    assert windows.backend == "windows_credential_manager"

    with pytest.raises(ValidationError):
        CredentialReference(
            backend="environment",
            identifier="CLOUD_CHAT",
        )
    with pytest.raises(ValidationError):
        CredentialReference(
            backend="windows_credential_manager",
            identifier="DESKPILOT/CLOUD_CHAT",
        )
    with pytest.raises(ValidationError):
        CredentialReference(
            backend="windows_credential_manager",
            identifier="cloud-chat",
        )


def test_windows_store_resolve_and_idempotent_delete_zeroize_temp_buffers() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    reference = windows_reference()
    target = f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{WINDOWS_IDENTIFIER}"

    manager.store(reference, SecretStr(SECRET))

    assert api.values[target] == SECRET.encode()
    assert api.last_write_buffer is not None
    assert set(api.last_write_buffer) == {0}

    resolved = manager.resolve(reference)

    assert resolved.get_secret_value() == SECRET
    assert SECRET not in repr(resolved)
    assert api.last_read_buffer is not None
    assert set(api.last_read_buffer) == {0}
    assert manager.delete(reference) is True
    assert manager.delete(reference) is False


def test_windows_resolver_rejects_missing_blank_and_invalid_utf8() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    reference = windows_reference()
    target = f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{WINDOWS_IDENTIFIER}"

    with pytest.raises(CredentialNotFoundError) as missing:
        manager.resolve(reference)
    assert missing.value.code == "CREDENTIAL_NOT_FOUND"

    api.values[target] = b"   "
    with pytest.raises(CredentialInvalidError) as blank:
        manager.resolve(reference)
    assert blank.value.code == "CREDENTIAL_INVALID"

    api.values[target] = b"\xff\xfe"
    with pytest.raises(CredentialInvalidError):
        manager.resolve(reference)


def test_windows_store_rejects_blank_and_oversized_utf8_without_writing() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    reference = windows_reference()

    with pytest.raises(CredentialInvalidError):
        manager.store(reference, SecretStr("   "))
    with pytest.raises(CredentialInvalidError):
        manager.store(
            reference,
            SecretStr("密" * CRED_MAX_CREDENTIAL_BLOB_SIZE),
        )

    assert api.write_calls == 0


@pytest.mark.parametrize("operation", ["read", "write", "delete"])
def test_windows_api_failures_are_stable_and_sanitized(operation: str) -> None:
    api = FakeWindowsCredentialApi()
    api.failure = WindowsCredentialApiError(
        operation=operation,
        error_code=1312,
    )
    manager = WindowsCredentialManager(api)
    reference = windows_reference()

    with pytest.raises(CredentialOperationError) as raised:
        if operation == "read":
            manager.resolve(reference)
        elif operation == "write":
            manager.store(reference, SecretStr(SECRET))
        else:
            manager.delete(reference)

    error = raised.value
    assert error.code == "CREDENTIAL_BACKEND_OPERATION_FAILED"
    assert error.operation == operation
    assert error.os_error_code == 1312
    assert SECRET not in str(error)
    assert WINDOWS_CREDENTIAL_TARGET_PREFIX not in str(error)


def test_resolvers_never_fall_back_across_explicit_backends() -> None:
    environment = EnvironmentCredentialResolver(
        {"DESKPILOT_CREDENTIAL_CLOUD_CHAT": SECRET}
    )
    composite = CompositeCredentialResolver({"environment": environment})
    reference = windows_reference()

    with pytest.raises(CredentialBackendUnavailableError) as raised:
        composite.resolve(reference)

    assert raised.value.backend == "windows_credential_manager"
    assert raised.value.code == "CREDENTIAL_BACKEND_UNAVAILABLE"
    assert SECRET not in str(raised.value)

    with pytest.raises(CredentialBackendUnavailableError):
        environment.resolve(reference)


def test_default_composite_keeps_environment_backend_compatible(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identifier = "DESKPILOT_CREDENTIAL_DEFAULT_COMPOSITE"
    monkeypatch.setenv(identifier, SECRET)
    resolver = create_default_credential_resolver()

    resolved = resolver.resolve(CredentialReference(identifier=identifier))

    assert resolved.get_secret_value() == SECRET


def test_provider_factory_resolves_windows_reference_through_existing_port() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    reference = windows_reference()
    manager.store(reference, SecretStr(SECRET))
    settings = Settings(
        _env_file=None,
        model_default_provider_id="windows-cloud",
        model_providers=(
            OpenAICompatibleChatProviderConfig(
                provider_id="windows-cloud",
                display_name="Windows Credential Cloud",
                model="cloud-model",
                base_url="https://models.example.test/v1",
                location=ModelLocation.CLOUD,
                credential_ref=reference,
            ),
        ),
    )

    providers = create_configured_model_providers(settings, manager)

    assert len(providers) == 1
    assert providers[0].descriptor.provider_id == "windows-cloud"
    serialized = settings.model_dump_json() + providers[0].descriptor.model_dump_json()
    assert SECRET not in serialized
    assert WINDOWS_CREDENTIAL_TARGET_PREFIX not in serialized


def test_credential_cli_stores_and_checks_without_printing_secret() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    output = StringIO()
    errors = StringIO()
    supplied = iter((SECRET, SECRET))

    stored = run_credential_cli(
        ("store", WINDOWS_IDENTIFIER),
        manager=manager,
        secret_reader=lambda _: next(supplied),
        stdout=output,
        stderr=errors,
    )
    checked = run_credential_cli(
        ("status", WINDOWS_IDENTIFIER),
        manager=manager,
        stdout=output,
        stderr=errors,
    )

    serialized_output = output.getvalue() + errors.getvalue()
    assert stored == 0
    assert checked == 0
    assert "Credential stored." in serialized_output
    assert "Credential is available." in serialized_output
    assert SECRET not in serialized_output


def test_credential_cli_requires_explicit_delete_confirmation() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    reference = windows_reference()
    manager.store(reference, SecretStr(SECRET))
    output = StringIO()
    errors = StringIO()

    refused = run_credential_cli(
        ("delete", WINDOWS_IDENTIFIER),
        manager=manager,
        stdout=output,
        stderr=errors,
    )
    target = f"{WINDOWS_CREDENTIAL_TARGET_PREFIX}{WINDOWS_IDENTIFIER}"

    assert refused == 2
    assert target in api.values

    deleted = run_credential_cli(
        ("delete", WINDOWS_IDENTIFIER, "--yes"),
        manager=manager,
        stdout=output,
        stderr=errors,
    )

    assert manager.delete(reference) is False
    assert deleted == 0
    assert "CREDENTIAL_DELETE_CONFIRMATION_REQUIRED" in errors.getvalue()
    assert SECRET not in output.getvalue() + errors.getvalue()


def test_credential_cli_rejects_confirmation_mismatch_and_invalid_identifier() -> None:
    api = FakeWindowsCredentialApi()
    manager = WindowsCredentialManager(api)
    errors = StringIO()
    supplied = iter((SECRET, "different-secret"))

    mismatch = run_credential_cli(
        ("store", WINDOWS_IDENTIFIER),
        manager=manager,
        secret_reader=lambda _: next(supplied),
        stderr=errors,
    )
    invalid = run_credential_cli(
        ("status", "invalid/name"),
        manager=manager,
        stderr=errors,
    )

    assert mismatch == 2
    assert invalid == 2
    assert api.write_calls == 0
    assert SECRET not in errors.getvalue()


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 API is Windows-only")
def test_win32_credential_api_loads_without_accessing_the_real_store() -> None:
    api = Win32CredentialApi()

    assert api is not None
