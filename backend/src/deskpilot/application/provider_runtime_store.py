"""Ports for protecting and persisting Provider runtime configuration."""

from dataclasses import dataclass
from typing import Protocol

from deskpilot.domain.provider_runtime import (
    ProviderConfigAuditContext,
    ProviderConfigAuditEvent,
    ProviderRuntimeConfigBundle,
    ProviderRuntimeConfigSnapshot,
)


class ProviderRuntimeConfigNotFoundError(LookupError):
    code = "MODEL_PROVIDER_RUNTIME_CONFIG_NOT_FOUND"

    def __init__(self, provider_id: str) -> None:
        super().__init__("Provider runtime configuration was not found")
        self.provider_id = provider_id


class ProviderRuntimeConfigVersionConflictError(RuntimeError):
    code = "MODEL_PROVIDER_RUNTIME_CONFIG_VERSION_CONFLICT"

    def __init__(self, *, expected_revision: int, actual_revision: int) -> None:
        super().__init__(
            "Provider runtime configuration version conflict: "
            f"expected {expected_revision}, actual {actual_revision}"
        )
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class ProviderRuntimeConfigInvalidError(RuntimeError):
    code = "MODEL_PROVIDER_RUNTIME_CONFIG_INVALID"


class ProviderRuntimeConfigProtectionError(RuntimeError):
    code = "MODEL_PROVIDER_RUNTIME_CONFIG_PROTECTION_FAILED"

    def __init__(
        self,
        *,
        operation: str,
        os_error_code: int | None = None,
    ) -> None:
        super().__init__("Provider runtime configuration protection failed")
        self.operation = operation
        self.os_error_code = os_error_code


class ProviderRuntimeConfigProtectionUnavailableError(RuntimeError):
    code = "MODEL_PROVIDER_RUNTIME_CONFIG_PROTECTION_UNAVAILABLE"


class RuntimeConfigProtector(Protocol):
    @property
    def scheme(self) -> str: ...

    def protect(self, plaintext: bytearray, *, context: str) -> bytes: ...

    def unprotect(self, payload: bytes, *, context: str) -> bytearray: ...


@dataclass(frozen=True, slots=True)
class ProtectedRuntimeConfigPayload:
    scheme: str
    payload: bytes


class ProviderRuntimeConfigStore(Protocol):
    async def put(
        self,
        bundle: ProviderRuntimeConfigBundle,
        *,
        audit: ProviderConfigAuditContext,
        expected_revision: int | None = None,
    ) -> ProviderRuntimeConfigSnapshot: ...

    async def get(self, provider_id: str) -> ProviderRuntimeConfigSnapshot: ...

    async def delete(
        self,
        provider_id: str,
        *,
        audit: ProviderConfigAuditContext,
        expected_revision: int | None = None,
    ) -> bool: ...

    async def list_audit_events(
        self,
        *,
        provider_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[ProviderConfigAuditEvent, ...]: ...
