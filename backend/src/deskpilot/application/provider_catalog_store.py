"""Persistence port for the versioned, secret-free Provider catalog."""

from typing import Protocol

from deskpilot.domain.provider_management import (
    PersistedProviderCatalog,
    ProviderCatalogDefinition,
)


class ProviderCatalogNotInitializedError(LookupError):
    code = "MODEL_PROVIDER_CATALOG_NOT_INITIALIZED"


class ProviderCatalogVersionConflictError(RuntimeError):
    code = "MODEL_PROVIDER_CATALOG_VERSION_CONFLICT"

    def __init__(self, *, expected_version: int, actual_version: int) -> None:
        super().__init__(
            "Provider catalog version conflict: "
            f"expected {expected_version}, actual {actual_version}"
        )
        self.expected_version = expected_version
        self.actual_version = actual_version


class ProviderCatalogStore(Protocol):
    async def import_definition(
        self,
        definition: ProviderCatalogDefinition,
        *,
        expected_version: int | None = None,
    ) -> PersistedProviderCatalog: ...

    async def get_catalog(self) -> PersistedProviderCatalog: ...
