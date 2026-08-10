"""SQLite-backed repository for the public Provider catalog projection."""

from datetime import UTC, datetime

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.provider_catalog_store import (
    ProviderCatalogNotInitializedError,
    ProviderCatalogVersionConflictError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.model_contracts import ModelProviderDescriptor
from deskpilot.domain.provider_management import (
    PersistedProviderCatalog,
    ProviderCatalogDefinition,
    ProviderCatalogDefinitionEntry,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ProviderCatalogEntryRecord,
    ProviderCatalogStateRecord,
    utc_now,
)

ACTIVE_CATALOG_ID = "active"


class SqlAlchemyProviderCatalogRepository:
    """Persist only the public projection needed by read APIs."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def import_definition(
        self,
        definition: ProviderCatalogDefinition,
        *,
        expected_version: int | None = None,
    ) -> PersistedProviderCatalog:
        if expected_version is not None and expected_version < 0:
            raise ValueError("Expected Provider catalog version cannot be negative")

        normalized = self._normalize(definition)
        digest = sha256_digest(normalized)
        timestamp = utc_now()

        async with self._database.session() as session:
            async with session.begin():
                state = await session.get(
                    ProviderCatalogStateRecord,
                    ACTIVE_CATALOG_ID,
                )
                actual_version = state.version if state is not None else 0
                if (
                    expected_version is not None
                    and expected_version != actual_version
                ):
                    raise ProviderCatalogVersionConflictError(
                        expected_version=expected_version,
                        actual_version=actual_version,
                    )

                if state is not None and state.content_digest == digest:
                    return await self._load_catalog(session, state)

                next_version = actual_version + 1
                if state is None:
                    state = ProviderCatalogStateRecord(
                        catalog_id=ACTIVE_CATALOG_ID,
                        version=next_version,
                        default_provider_id=normalized.default_provider_id,
                        content_digest=digest,
                        imported_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(state)
                else:
                    updated_version = await session.scalar(
                        update(ProviderCatalogStateRecord)
                        .where(
                            ProviderCatalogStateRecord.catalog_id
                            == ACTIVE_CATALOG_ID,
                            ProviderCatalogStateRecord.version == actual_version,
                        )
                        .values(
                            version=next_version,
                            default_provider_id=normalized.default_provider_id,
                            content_digest=digest,
                            imported_at=timestamp,
                            updated_at=timestamp,
                        )
                        .returning(ProviderCatalogStateRecord.version)
                    )
                    if updated_version != next_version:
                        latest_version = await session.scalar(
                            select(ProviderCatalogStateRecord.version).where(
                                ProviderCatalogStateRecord.catalog_id
                                == ACTIVE_CATALOG_ID
                            )
                        )
                        raise ProviderCatalogVersionConflictError(
                            expected_version=actual_version,
                            actual_version=latest_version or 0,
                        )
                    await session.execute(
                        delete(ProviderCatalogEntryRecord).where(
                            ProviderCatalogEntryRecord.catalog_id
                            == ACTIVE_CATALOG_ID
                        )
                    )

                session.add_all(
                    [
                        ProviderCatalogEntryRecord(
                            catalog_id=ACTIVE_CATALOG_ID,
                            provider_id=entry.descriptor.provider_id,
                            ordinal=ordinal,
                            descriptor=entry.descriptor.model_dump(mode="json"),
                            enabled=entry.enabled,
                            created_at=timestamp,
                            updated_at=timestamp,
                        )
                        for ordinal, entry in enumerate(normalized.providers)
                    ]
                )
                await session.flush()
                return PersistedProviderCatalog(
                    catalog_version=next_version,
                    imported_at=timestamp,
                    definition=normalized,
                )

    async def get_catalog(self) -> PersistedProviderCatalog:
        async with self._database.session() as session:
            state = await session.get(
                ProviderCatalogStateRecord,
                ACTIVE_CATALOG_ID,
            )
            if state is None:
                raise ProviderCatalogNotInitializedError(
                    "Provider catalog has not been imported"
                )
            return await self._load_catalog(session, state)

    @staticmethod
    async def _load_catalog(
        session: AsyncSession,
        state: ProviderCatalogStateRecord,
    ) -> PersistedProviderCatalog:
        entries = (
            await session.scalars(
                select(ProviderCatalogEntryRecord)
                .where(
                    ProviderCatalogEntryRecord.catalog_id == state.catalog_id
                )
                .order_by(ProviderCatalogEntryRecord.ordinal)
            )
        ).all()
        definition = ProviderCatalogDefinition(
            default_provider_id=state.default_provider_id,
            providers=tuple(
                ProviderCatalogDefinitionEntry(
                    descriptor=ModelProviderDescriptor.model_validate(
                        entry.descriptor
                    ),
                    enabled=entry.enabled,
                )
                for entry in entries
            ),
        )
        return PersistedProviderCatalog(
            catalog_version=state.version,
            imported_at=SqlAlchemyProviderCatalogRepository._as_utc(
                state.imported_at
            ),
            definition=definition,
        )

    @staticmethod
    def _normalize(
        definition: ProviderCatalogDefinition,
    ) -> ProviderCatalogDefinition:
        return ProviderCatalogDefinition(
            default_provider_id=definition.default_provider_id,
            providers=tuple(
                sorted(
                    definition.providers,
                    key=lambda entry: entry.descriptor.provider_id,
                )
            ),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
