import sys
from pathlib import Path

import pytest

from deskpilot.application.provider_runtime_codec import ProviderRuntimeConfigCodec
from deskpilot.application.provider_runtime_store import (
    ProviderRuntimeConfigNotFoundError,
    ProviderRuntimeConfigProtectionError,
    ProviderRuntimeConfigVersionConflictError,
)
from deskpilot.domain.model_contracts import ModelLocation
from deskpilot.domain.provider_config import (
    CredentialReference,
    FakeProviderConfig,
    OpenAICompatibleChatProviderConfig,
)
from deskpilot.domain.provider_runtime import (
    CredentialAuditDisposition,
    ProviderConfigActorType,
    ProviderConfigAuditContext,
    ProviderConfigAuditSource,
    ProviderRuntimeConfigBundle,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.provider_runtime_repository import (
    SqlAlchemyProviderRuntimeConfigRepository,
)
from deskpilot.infrastructure.windows_dpapi import (
    DPAPI_SCHEME,
    DataProtectionApi,
    WindowsDpapiProtector,
)

BASE_URL = "https://sensitive-runtime-endpoint.example.invalid/v1"
CREDENTIAL_ID = "DESKPILOT_CREDENTIAL_RUNTIME_TEST"


class RecordingProtector:
    scheme = "test_xor_v1"

    def __init__(self) -> None:
        self.protect_buffer: bytearray | None = None
        self.unprotect_buffer: bytearray | None = None
        self.contexts: list[str] = []

    def protect(self, plaintext: bytearray, *, context: str) -> bytes:
        self.protect_buffer = plaintext
        self.contexts.append(context)
        return bytes(value ^ 0xA5 for value in plaintext)

    def unprotect(self, payload: bytes, *, context: str) -> bytearray:
        self.contexts.append(context)
        plaintext = bytearray(value ^ 0xA5 for value in payload)
        self.unprotect_buffer = plaintext
        return plaintext


class FakeDataProtectionApi(DataProtectionApi):
    def __init__(self, error_operation: str | None = None) -> None:
        self.error_operation = error_operation
        self.protect_entropy: bytearray | None = None
        self.unprotect_entropy: bytearray | None = None

    def protect(
        self,
        plaintext: bytearray,
        *,
        entropy: bytearray,
        description: str,
    ) -> bytes:
        self.protect_entropy = entropy
        if self.error_operation == "protect":
            raise OSError(5, "sensitive system error")
        assert description == "DeskPilot Provider runtime configuration"
        return bytes(reversed(plaintext))

    def unprotect(
        self,
        payload: bytes,
        *,
        entropy: bytearray,
    ) -> bytearray:
        self.unprotect_entropy = entropy
        if self.error_operation == "unprotect":
            raise OSError(13, "sensitive system error")
        return bytearray(reversed(payload))


def _cloud_config(
    *,
    base_url: str = BASE_URL,
    credential_id: str = CREDENTIAL_ID,
) -> OpenAICompatibleChatProviderConfig:
    return OpenAICompatibleChatProviderConfig(
        provider_id="runtime-cloud",
        display_name="Runtime Cloud",
        model="runtime-model",
        base_url=base_url,
        location=ModelLocation.CLOUD,
        credential_ref=CredentialReference(identifier=credential_id),
    )


def _system_audit() -> ProviderConfigAuditContext:
    return ProviderConfigAuditContext(
        source=ProviderConfigAuditSource.STARTUP_IMPORT,
        actor_type=ProviderConfigActorType.SYSTEM,
        correlation_id="startup-import-1",
    )


def _user_audit() -> ProviderConfigAuditContext:
    return ProviderConfigAuditContext(
        source=ProviderConfigAuditSource.LOCAL_API,
        actor_type=ProviderConfigActorType.LOCAL_USER,
        correlation_id="request-1",
    )


async def _repository(
    database_path: Path,
) -> tuple[
    Database,
    SqlAlchemyProviderRuntimeConfigRepository,
    RecordingProtector,
]:
    database = Database(f"sqlite+aiosqlite:///{database_path.as_posix()}")
    await database.migrate()
    protector = RecordingProtector()
    repository = SqlAlchemyProviderRuntimeConfigRepository(
        database,
        ProviderRuntimeConfigCodec(protector),
    )
    return database, repository, protector


def test_runtime_bundle_rejects_mismatched_provider_identity() -> None:
    with pytest.raises(ValueError, match="Provider IDs must match"):
        ProviderRuntimeConfigBundle(
            provider_id="different-provider",
            config=FakeProviderConfig(),
        )


def test_codec_round_trip_binds_context_and_zeroes_working_buffers() -> None:
    protector = RecordingProtector()
    codec = ProviderRuntimeConfigCodec(protector)
    bundle = ProviderRuntimeConfigBundle.from_config(_cloud_config())

    protected = codec.encode(bundle)
    decoded = codec.decode(
        provider_id=bundle.provider_id,
        scheme=protected.scheme,
        payload=protected.payload,
    )

    assert decoded == bundle
    assert protector.contexts == [
        "DeskPilot/ProviderRuntime/runtime-cloud/v1",
        "DeskPilot/ProviderRuntime/runtime-cloud/v1",
    ]
    assert protector.protect_buffer is not None
    assert not any(protector.protect_buffer)
    assert protector.unprotect_buffer is not None
    assert not any(protector.unprotect_buffer)
    assert BASE_URL.encode() not in protected.payload
    assert CREDENTIAL_ID.encode() not in protected.payload


def test_dpapi_protector_zeroes_entropy_and_sanitizes_errors() -> None:
    api = FakeDataProtectionApi()
    protector = WindowsDpapiProtector(api)
    plaintext = bytearray(b"runtime-configuration")

    protected = protector.protect(plaintext, context="provider-context")
    cleartext = protector.unprotect(protected, context="provider-context")

    assert protector.scheme == DPAPI_SCHEME
    assert cleartext == plaintext
    assert api.protect_entropy is not None and not any(api.protect_entropy)
    assert api.unprotect_entropy is not None and not any(api.unprotect_entropy)

    failing = WindowsDpapiProtector(FakeDataProtectionApi("protect"))
    with pytest.raises(ProviderRuntimeConfigProtectionError) as raised:
        failing.protect(bytearray(b"secret-value"), context="sensitive-context")
    assert raised.value.operation == "protect"
    assert raised.value.os_error_code == 5
    assert "secret-value" not in str(raised.value)
    assert "sensitive-context" not in str(raised.value)
    assert "sensitive system error" not in str(raised.value)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows DPAPI only")
def test_real_windows_dpapi_round_trip_has_no_persistent_side_effect() -> None:
    codec = ProviderRuntimeConfigCodec(WindowsDpapiProtector())
    bundle = ProviderRuntimeConfigBundle.from_config(FakeProviderConfig())

    protected = codec.encode(bundle)
    decoded = codec.decode(
        provider_id=bundle.provider_id,
        scheme=protected.scheme,
        payload=protected.payload,
    )

    assert decoded == bundle
    assert protected.payload != b""
    assert b"deskpilot-fake-v1" not in protected.payload


@pytest.mark.asyncio
async def test_repository_create_idempotent_update_and_audit(
    tmp_path: Path,
) -> None:
    database, repository, _ = await _repository(tmp_path / "runtime.db")
    original = ProviderRuntimeConfigBundle.from_config(_cloud_config())
    updated = ProviderRuntimeConfigBundle.from_config(
        _cloud_config(
            base_url="https://changed-endpoint.example.invalid/v1"
        )
    )

    try:
        created = await repository.put(
            original,
            audit=_system_audit(),
            expected_revision=0,
        )
        unchanged = await repository.put(
            original,
            audit=_system_audit(),
            expected_revision=1,
        )
        changed = await repository.put(
            updated,
            audit=_user_audit(),
            expected_revision=1,
        )
        loaded = await repository.get(original.provider_id)
        events = await repository.list_audit_events(
            provider_id=original.provider_id
        )
    finally:
        await database.dispose()

    assert created.revision == 1
    assert unchanged.revision == 1
    assert unchanged.updated_at == created.updated_at
    assert changed.revision == 2
    assert loaded.bundle == updated
    assert [event.action.value for event in events] == ["created", "updated"]
    assert events[0].credential_disposition is (
        CredentialAuditDisposition.REFERENCE_ATTACHED
    )
    assert events[1].changed_fields == ("base_url",)
    assert events[1].credential_disposition is (
        CredentialAuditDisposition.REFERENCE_UNCHANGED
    )


@pytest.mark.asyncio
async def test_repository_version_conflict_preserves_current_configuration(
    tmp_path: Path,
) -> None:
    database, repository, _ = await _repository(tmp_path / "conflict.db")
    bundle = ProviderRuntimeConfigBundle.from_config(FakeProviderConfig())

    try:
        await repository.put(bundle, audit=_system_audit(), expected_revision=0)
        with pytest.raises(ProviderRuntimeConfigVersionConflictError) as raised:
            await repository.put(bundle, audit=_user_audit(), expected_revision=0)
        loaded = await repository.get(bundle.provider_id)
        events = await repository.list_audit_events()
    finally:
        await database.dispose()

    assert raised.value.expected_revision == 0
    assert raised.value.actual_revision == 1
    assert loaded.revision == 1
    assert len(events) == 1


@pytest.mark.asyncio
async def test_delete_retains_credential_and_database_contains_no_plaintext(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "delete.db"
    database, repository, _ = await _repository(database_path)
    bundle = ProviderRuntimeConfigBundle.from_config(_cloud_config())

    try:
        await repository.put(bundle, audit=_system_audit(), expected_revision=0)
        deleted = await repository.delete(
            bundle.provider_id,
            audit=_user_audit(),
            expected_revision=1,
        )
        deleted_again = await repository.delete(
            bundle.provider_id,
            audit=_user_audit(),
        )
        events = await repository.list_audit_events(provider_id=bundle.provider_id)
        with pytest.raises(ProviderRuntimeConfigNotFoundError):
            await repository.get(bundle.provider_id)
    finally:
        await database.dispose()

    database_bytes = database_path.read_bytes()
    assert deleted is True
    assert deleted_again is False
    assert [event.action.value for event in events] == ["created", "deleted"]
    assert events[-1].credential_disposition is (
        CredentialAuditDisposition.PROVIDER_DELETED_CREDENTIAL_RETAINED
    )
    assert BASE_URL.encode() not in database_bytes
    assert CREDENTIAL_ID.encode() not in database_bytes


@pytest.mark.asyncio
async def test_audit_pagination_is_stable_and_value_free(tmp_path: Path) -> None:
    database, repository, _ = await _repository(tmp_path / "audit.db")
    first = ProviderRuntimeConfigBundle.from_config(
        FakeProviderConfig(provider_id="audit-first")
    )
    second = ProviderRuntimeConfigBundle.from_config(
        FakeProviderConfig(provider_id="audit-second")
    )

    try:
        await repository.put(first, audit=_system_audit())
        await repository.put(second, audit=_system_audit())
        page = await repository.list_audit_events(after_sequence=1, limit=1)
    finally:
        await database.dispose()

    assert len(page) == 1
    assert page[0].sequence == 2
    serialized = page[0].model_dump_json()
    assert "base_url" not in serialized
    assert CREDENTIAL_ID not in serialized
