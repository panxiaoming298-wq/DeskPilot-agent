"""Read-only Provider catalog and health use cases."""

from deskpilot.application.model_gateway import (
    DisabledModelProviderError,
    UnknownModelProviderError,
)
from deskpilot.application.provider_catalog_store import ProviderCatalogStore
from deskpilot.application.provider_health_service import ProviderHealthService
from deskpilot.domain.provider_management import (
    ProviderCatalogEntry,
    ProviderCatalogSnapshot,
    ProviderHealthSnapshot,
)


class ProviderCatalogService:
    def __init__(
        self,
        *,
        store: ProviderCatalogStore,
        health_service: ProviderHealthService,
    ) -> None:
        self._store = store
        self._health_service = health_service

    async def snapshot(self) -> ProviderCatalogSnapshot:
        persisted = await self._store.get_catalog()
        cached_health = await self._health_service.cached_snapshots()
        return ProviderCatalogSnapshot(
            catalog_version=persisted.catalog_version,
            imported_at=persisted.imported_at,
            default_provider_id=persisted.definition.default_provider_id,
            providers=tuple(
                ProviderCatalogEntry(
                    descriptor=entry.descriptor,
                    enabled=entry.enabled,
                    is_default=(
                        entry.descriptor.provider_id
                        == persisted.definition.default_provider_id
                    ),
                    cached_health=cached_health.get(
                        entry.descriptor.provider_id
                    ),
                )
                for entry in persisted.definition.providers
            ),
        )

    async def probe(self, provider_id: str) -> ProviderHealthSnapshot:
        persisted = await self._store.get_catalog()
        provider = next(
            (
                entry
                for entry in persisted.definition.providers
                if entry.descriptor.provider_id == provider_id
            ),
            None,
        )
        if provider is None:
            raise UnknownModelProviderError(
                f"Model provider is not configured: {provider_id}",
                provider_id=provider_id,
            )
        if not provider.enabled:
            raise DisabledModelProviderError(
                f"Model provider is disabled: {provider_id}",
                provider_id=provider_id,
            )
        return await self._health_service.get(provider_id)

    async def shutdown(self) -> None:
        await self._health_service.shutdown()
