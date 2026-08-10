"""Persistence port for atomic Provider configuration administration."""

from dataclasses import dataclass
from typing import Protocol

from deskpilot.domain.provider_admin import (
    ProviderManagementState,
    ProviderMutationAction,
    ProviderMutationResult,
)
from deskpilot.domain.provider_management import ProviderCatalogDefinition
from deskpilot.domain.provider_runtime import (
    ProviderConfigAuditContext,
    ProviderConfigAuditEvent,
    ProviderRuntimeConfigBundle,
)


class ProviderAlreadyExistsError(RuntimeError):
    code = "MODEL_PROVIDER_ALREADY_EXISTS"

    def __init__(self, provider_id: str) -> None:
        super().__init__("Model Provider already exists")
        self.provider_id = provider_id


class ProviderManagementNotFoundError(LookupError):
    code = "MODEL_PROVIDER_NOT_FOUND"

    def __init__(self, provider_id: str) -> None:
        super().__init__("Model Provider was not found")
        self.provider_id = provider_id


class ProviderManagementConflictError(RuntimeError):
    code = "MODEL_PROVIDER_MANAGEMENT_CONFLICT"

    def __init__(self, message: str, *, provider_id: str) -> None:
        super().__init__(message)
        self.provider_id = provider_id


class ProviderIdempotencyConflictError(RuntimeError):
    code = "IDEMPOTENCY_KEY_REUSED"


@dataclass(frozen=True, slots=True)
class ProviderIdempotencyContext:
    key_digest: str
    operation: str
    request_fingerprint: str


@dataclass(frozen=True, slots=True)
class PreparedProviderMutation:
    action: ProviderMutationAction
    provider_id: str
    definition: ProviderCatalogDefinition
    bundles: tuple[ProviderRuntimeConfigBundle, ...]
    expected_catalog_version: int
    audit: ProviderConfigAuditContext
    idempotency: ProviderIdempotencyContext


@dataclass(frozen=True, slots=True)
class ProviderManagementCommit:
    result: ProviderMutationResult
    replayed: bool


class ProviderManagementStore(Protocol):
    async def bootstrap(
        self,
        *,
        definition: ProviderCatalogDefinition,
        bundles: tuple[ProviderRuntimeConfigBundle, ...],
        audit: ProviderConfigAuditContext,
    ) -> ProviderManagementState: ...

    async def get_state(self) -> ProviderManagementState: ...

    async def replay(
        self,
        idempotency: ProviderIdempotencyContext,
    ) -> ProviderMutationResult | None: ...

    async def commit(
        self,
        mutation: PreparedProviderMutation,
    ) -> ProviderManagementCommit: ...

    async def list_audit_events(
        self,
        *,
        provider_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[ProviderConfigAuditEvent, ...]: ...
