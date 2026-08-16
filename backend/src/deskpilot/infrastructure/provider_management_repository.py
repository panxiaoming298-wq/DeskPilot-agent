"""Atomic SQLAlchemy repository for Provider management mutations."""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.provider_catalog_store import (
    ProviderCatalogNotInitializedError,
    ProviderCatalogVersionConflictError,
)
from deskpilot.application.provider_management_store import (
    PreparedProviderMutation,
    ProviderIdempotencyConflictError,
    ProviderIdempotencyContext,
    ProviderManagementCommit,
)
from deskpilot.application.provider_runtime_codec import ProviderRuntimeConfigCodec
from deskpilot.application.provider_runtime_store import (
    ProviderRuntimeConfigInvalidError,
)
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.provider_admin import (
    ProviderManagementState,
    ProviderMutationAction,
    ProviderMutationResult,
)
from deskpilot.domain.provider_config import ProviderConfig
from deskpilot.domain.provider_management import (
    ProviderCatalogDefinition,
)
from deskpilot.domain.provider_runtime import (
    CredentialAuditDisposition,
    ProviderConfigActorType,
    ProviderConfigAuditAction,
    ProviderConfigAuditContext,
    ProviderConfigAuditEvent,
    ProviderConfigAuditSource,
    ProviderRuntimeConfigBundle,
    ProviderRuntimeConfigSnapshot,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    ProviderCatalogEntryRecord,
    ProviderCatalogStateRecord,
    ProviderConfigAuditEventRecord,
    ProviderIdempotencyRecord,
    ProviderRuntimeConfigRecord,
    utc_now,
)

ACTIVE_CATALOG_ID = "active"
IDEMPOTENCY_RETENTION = timedelta(hours=24)


class SqlAlchemyProviderManagementRepository:
    """Use one transaction for catalog, ciphertext, audit, and replay receipt."""

    def __init__(
        self,
        database: Database,
        codec: ProviderRuntimeConfigCodec,
    ) -> None:
        self._database = database
        self._codec = codec

    async def bootstrap(
        self,
        *,
        definition: ProviderCatalogDefinition,
        bundles: tuple[ProviderRuntimeConfigBundle, ...],
        audit: ProviderConfigAuditContext,
    ) -> ProviderManagementState:
        normalized, normalized_bundles = self._validate_candidate(
            definition,
            bundles,
        )
        timestamp = utc_now()
        async with self._database.session() as session:
            async with session.begin():
                existing_count = len(
                    (await session.scalars(select(ProviderRuntimeConfigRecord.provider_id))).all()
                )
                if existing_count:
                    return await self._load_state(session)

                state = await session.get(
                    ProviderCatalogStateRecord,
                    ACTIVE_CATALOG_ID,
                )
                digest = sha256_digest(normalized)
                if state is None:
                    state = ProviderCatalogStateRecord(
                        catalog_id=ACTIVE_CATALOG_ID,
                        version=1,
                        default_provider_id=normalized.default_provider_id,
                        content_digest=digest,
                        imported_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(state)
                    await self._replace_catalog_entries(
                        session,
                        normalized,
                        timestamp,
                    )
                elif state.content_digest != digest:
                    state.version += 1
                    state.default_provider_id = normalized.default_provider_id
                    state.content_digest = digest
                    state.imported_at = timestamp
                    state.updated_at = timestamp
                    await self._replace_catalog_entries(
                        session,
                        normalized,
                        timestamp,
                    )

                snapshots: list[ProviderRuntimeConfigSnapshot] = []
                for bundle in normalized_bundles:
                    protected = self._codec.encode(bundle)
                    record = ProviderRuntimeConfigRecord(
                        provider_id=bundle.provider_id,
                        config_kind=bundle.config.kind,
                        payload_schema_version=bundle.schema_version,
                        protection_scheme=protected.scheme,
                        protected_payload=protected.payload,
                        revision=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(record)
                    session.add(
                        self._audit_record(
                            provider_id=bundle.provider_id,
                            action=ProviderConfigAuditAction.CREATED,
                            audit=audit,
                            config_revision=1,
                            changed_fields=tuple(sorted(bundle.config.model_dump(mode="json"))),
                            credential_disposition=(
                                CredentialAuditDisposition.REFERENCE_ATTACHED
                                if self._credential_reference(bundle.config) is not None
                                else CredentialAuditDisposition.NOT_APPLICABLE
                            ),
                            occurred_at=timestamp,
                        )
                    )
                    snapshots.append(self._snapshot(record, bundle))
                await session.flush()
                return ProviderManagementState(
                    catalog_version=state.version,
                    imported_at=self._as_utc(state.imported_at),
                    default_provider_id=state.default_provider_id,
                    providers=tuple(snapshots),
                )

    async def get_state(self) -> ProviderManagementState:
        async with self._database.session() as session:
            return await self._load_state(session)

    async def replay(
        self,
        idempotency: ProviderIdempotencyContext,
    ) -> ProviderMutationResult | None:
        timestamp = utc_now()
        async with self._database.session() as session:
            async with session.begin():
                record = await session.get(
                    ProviderIdempotencyRecord,
                    idempotency.key_digest,
                )
                if record is None:
                    return None
                if self._as_utc(record.expires_at) <= timestamp:
                    await session.delete(record)
                    return None
                self._validate_idempotency(record, idempotency)
                return ProviderMutationResult.model_validate(record.response).model_copy(
                    update={"replayed": True}
                )

    async def commit(
        self,
        mutation: PreparedProviderMutation,
    ) -> ProviderManagementCommit:
        """Let a durable replay receipt normalize cross-instance write races."""
        for attempt in range(3):
            try:
                return await self._commit_once(mutation)
            except (
                IntegrityError,
                OperationalError,
                ProviderCatalogVersionConflictError,
            ):
                replay = await self.replay(mutation.idempotency)
                if replay is not None:
                    return ProviderManagementCommit(result=replay, replayed=True)
                if attempt == 2:
                    raise
                await asyncio.sleep(0.01 * (attempt + 1))
        raise RuntimeError("Provider idempotency race retry was exhausted")

    async def _commit_once(
        self,
        mutation: PreparedProviderMutation,
    ) -> ProviderManagementCommit:
        normalized, normalized_bundles = self._validate_candidate(
            mutation.definition,
            mutation.bundles,
        )
        timestamp = utc_now()
        async with self._database.session() as session:
            async with session.begin():
                replay = await self._find_replay(
                    session,
                    mutation.idempotency,
                    timestamp,
                )
                if replay is not None:
                    return ProviderManagementCommit(result=replay, replayed=True)

                current_state = await self._load_state(session)
                if mutation.expected_catalog_version != current_state.catalog_version:
                    raise ProviderCatalogVersionConflictError(
                        expected_version=mutation.expected_catalog_version,
                        actual_version=current_state.catalog_version,
                    )

                current = {item.bundle.provider_id: item for item in current_state.providers}
                candidate = {bundle.provider_id: bundle for bundle in normalized_bundles}
                current_configs = {
                    provider_id: snapshot.bundle for provider_id, snapshot in current.items()
                }
                state_changed = (
                    current_configs != candidate
                    or current_state.default_provider_id != normalized.default_provider_id
                )
                if not state_changed:
                    target = current[mutation.provider_id]
                    result = ProviderMutationResult(
                        action=mutation.action,
                        provider_id=mutation.provider_id,
                        catalog_version=current_state.catalog_version,
                        config_revision=target.revision,
                        default_provider_id=current_state.default_provider_id,
                        credential_disposition=self._credential_disposition(
                            target.bundle,
                            target.bundle,
                        ),
                    )
                    await self._store_idempotency(
                        session,
                        mutation.idempotency,
                        result,
                        timestamp,
                    )
                    return ProviderManagementCommit(result=result, replayed=False)

                self._validate_mutation_shape(
                    mutation,
                    current_configs,
                    candidate,
                    current_state.default_provider_id,
                    normalized.default_provider_id,
                )
                target_before = current.get(mutation.provider_id)
                target_after = candidate.get(mutation.provider_id)
                config_revision = await self._apply_runtime_changes(
                    session,
                    current,
                    candidate,
                    mutation.provider_id,
                    timestamp,
                )

                next_version = current_state.catalog_version + 1
                updated_version = await session.scalar(
                    update(ProviderCatalogStateRecord)
                    .where(
                        ProviderCatalogStateRecord.catalog_id == ACTIVE_CATALOG_ID,
                        ProviderCatalogStateRecord.version == current_state.catalog_version,
                    )
                    .values(
                        version=next_version,
                        default_provider_id=normalized.default_provider_id,
                        content_digest=sha256_digest(normalized),
                        imported_at=timestamp,
                        updated_at=timestamp,
                    )
                    .returning(ProviderCatalogStateRecord.version)
                )
                if updated_version != next_version:
                    latest = await session.scalar(
                        select(ProviderCatalogStateRecord.version).where(
                            ProviderCatalogStateRecord.catalog_id == ACTIVE_CATALOG_ID
                        )
                    )
                    raise ProviderCatalogVersionConflictError(
                        expected_version=current_state.catalog_version,
                        actual_version=latest or 0,
                    )
                await self._replace_catalog_entries(
                    session,
                    normalized,
                    timestamp,
                )

                credential_disposition = self._credential_disposition(
                    target_before.bundle if target_before is not None else None,
                    target_after,
                    deleted=mutation.action is ProviderMutationAction.DELETED,
                )
                session.add(
                    self._audit_record(
                        provider_id=mutation.provider_id,
                        action=ProviderConfigAuditAction(mutation.action.value),
                        audit=mutation.audit,
                        config_revision=config_revision,
                        changed_fields=self._mutation_changed_fields(
                            mutation.action,
                            target_before.bundle if target_before is not None else None,
                            target_after,
                        ),
                        credential_disposition=credential_disposition,
                        occurred_at=timestamp,
                    )
                )
                result = ProviderMutationResult(
                    action=mutation.action,
                    provider_id=mutation.provider_id,
                    catalog_version=next_version,
                    config_revision=config_revision,
                    default_provider_id=normalized.default_provider_id,
                    credential_disposition=credential_disposition,
                )
                await self._store_idempotency(
                    session,
                    mutation.idempotency,
                    result,
                    timestamp,
                )
                await session.flush()
                return ProviderManagementCommit(result=result, replayed=False)

    async def list_audit_events(
        self,
        *,
        provider_id: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> tuple[ProviderConfigAuditEvent, ...]:
        if after_sequence < 0:
            raise ValueError("Provider audit sequence cannot be negative")
        if limit < 1 or limit > 500:
            raise ValueError("Provider audit limit must be between 1 and 500")
        statement = (
            select(ProviderConfigAuditEventRecord)
            .where(ProviderConfigAuditEventRecord.sequence > after_sequence)
            .order_by(ProviderConfigAuditEventRecord.sequence)
            .limit(limit)
        )
        if provider_id is not None:
            statement = statement.where(ProviderConfigAuditEventRecord.provider_id == provider_id)
        async with self._database.session() as session:
            records = (await session.scalars(statement)).all()
        return tuple(self._audit_event(record) for record in records)

    async def _load_state(self, session: AsyncSession) -> ProviderManagementState:
        state = await session.get(ProviderCatalogStateRecord, ACTIVE_CATALOG_ID)
        if state is None:
            raise ProviderCatalogNotInitializedError("Provider catalog has not been initialized")
        records = (
            await session.scalars(
                select(ProviderRuntimeConfigRecord).order_by(
                    ProviderRuntimeConfigRecord.provider_id
                )
            )
        ).all()
        if not records:
            raise ProviderCatalogNotInitializedError(
                "Provider runtime configuration has not been initialized"
            )
        snapshots = tuple(self._snapshot(record, self._decode_record(record)) for record in records)
        entries = (
            await session.scalars(
                select(ProviderCatalogEntryRecord).where(
                    ProviderCatalogEntryRecord.catalog_id == ACTIVE_CATALOG_ID
                )
            )
        ).all()
        runtime_enabled = {
            item.bundle.provider_id: item.bundle.config.enabled for item in snapshots
        }
        public_enabled = {entry.provider_id: entry.enabled for entry in entries}
        if runtime_enabled != public_enabled:
            raise ProviderRuntimeConfigInvalidError(
                "Provider public catalog and runtime configuration are inconsistent"
            )
        return ProviderManagementState(
            catalog_version=state.version,
            imported_at=self._as_utc(state.imported_at),
            default_provider_id=state.default_provider_id,
            providers=snapshots,
        )

    async def _find_replay(
        self,
        session: AsyncSession,
        idempotency: ProviderIdempotencyContext,
        timestamp: datetime,
    ) -> ProviderMutationResult | None:
        record = await session.get(
            ProviderIdempotencyRecord,
            idempotency.key_digest,
        )
        if record is None:
            return None
        if self._as_utc(record.expires_at) <= timestamp:
            await session.delete(record)
            await session.flush()
            return None
        self._validate_idempotency(record, idempotency)
        return ProviderMutationResult.model_validate(record.response).model_copy(
            update={"replayed": True}
        )

    @staticmethod
    def _validate_idempotency(
        record: ProviderIdempotencyRecord,
        expected: ProviderIdempotencyContext,
    ) -> None:
        if (
            record.operation != expected.operation
            or record.request_fingerprint != expected.request_fingerprint
        ):
            raise ProviderIdempotencyConflictError(
                "Idempotency key was already used for another Provider request"
            )

    @staticmethod
    async def _store_idempotency(
        session: AsyncSession,
        idempotency: ProviderIdempotencyContext,
        result: ProviderMutationResult,
        timestamp: datetime,
    ) -> None:
        session.add(
            ProviderIdempotencyRecord(
                key_digest=idempotency.key_digest,
                operation=idempotency.operation,
                request_fingerprint=idempotency.request_fingerprint,
                response=result.model_dump(mode="json"),
                created_at=timestamp,
                expires_at=timestamp + IDEMPOTENCY_RETENTION,
            )
        )

    async def _apply_runtime_changes(
        self,
        session: AsyncSession,
        current: dict[str, ProviderRuntimeConfigSnapshot],
        candidate: dict[str, ProviderRuntimeConfigBundle],
        target_provider_id: str,
        timestamp: datetime,
    ) -> int:
        result_revision = 0
        for provider_id in sorted(current.keys() | candidate.keys()):
            before = current.get(provider_id)
            after = candidate.get(provider_id)
            if before is None and after is not None:
                protected = self._codec.encode(after)
                session.add(
                    ProviderRuntimeConfigRecord(
                        provider_id=provider_id,
                        config_kind=after.config.kind,
                        payload_schema_version=after.schema_version,
                        protection_scheme=protected.scheme,
                        protected_payload=protected.payload,
                        revision=1,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                )
                revision = 1
            elif before is not None and after is None:
                await session.execute(
                    delete(ProviderRuntimeConfigRecord).where(
                        ProviderRuntimeConfigRecord.provider_id == provider_id,
                        ProviderRuntimeConfigRecord.revision == before.revision,
                    )
                )
                revision = before.revision + 1
            elif before is not None and after is not None:
                if before.bundle == after:
                    revision = before.revision
                else:
                    protected = self._codec.encode(after)
                    next_revision = before.revision + 1
                    updated_revision = await session.scalar(
                        update(ProviderRuntimeConfigRecord)
                        .where(
                            ProviderRuntimeConfigRecord.provider_id == provider_id,
                            ProviderRuntimeConfigRecord.revision == before.revision,
                        )
                        .values(
                            config_kind=after.config.kind,
                            payload_schema_version=after.schema_version,
                            protection_scheme=protected.scheme,
                            protected_payload=protected.payload,
                            revision=next_revision,
                            updated_at=timestamp,
                        )
                        .returning(ProviderRuntimeConfigRecord.revision)
                    )
                    if updated_revision != next_revision:
                        raise ProviderCatalogVersionConflictError(
                            expected_version=before.revision,
                            actual_version=updated_revision or 0,
                        )
                    revision = next_revision
            else:
                raise AssertionError("Unreachable Provider state")
            if provider_id == target_provider_id:
                result_revision = revision
        if result_revision < 1:
            raise ProviderRuntimeConfigInvalidError(
                "Provider mutation target revision could not be determined"
            )
        return result_revision

    @staticmethod
    async def _replace_catalog_entries(
        session: AsyncSession,
        definition: ProviderCatalogDefinition,
        timestamp: datetime,
    ) -> None:
        await session.execute(
            delete(ProviderCatalogEntryRecord).where(
                ProviderCatalogEntryRecord.catalog_id == ACTIVE_CATALOG_ID
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
                for ordinal, entry in enumerate(definition.providers)
            ]
        )

    def _decode_record(
        self,
        record: ProviderRuntimeConfigRecord,
    ) -> ProviderRuntimeConfigBundle:
        return self._codec.decode(
            provider_id=record.provider_id,
            scheme=record.protection_scheme,
            payload=record.protected_payload,
        )

    @staticmethod
    def _snapshot(
        record: ProviderRuntimeConfigRecord,
        bundle: ProviderRuntimeConfigBundle,
    ) -> ProviderRuntimeConfigSnapshot:
        return ProviderRuntimeConfigSnapshot(
            revision=record.revision,
            created_at=SqlAlchemyProviderManagementRepository._as_utc(record.created_at),
            updated_at=SqlAlchemyProviderManagementRepository._as_utc(record.updated_at),
            bundle=bundle,
        )

    @staticmethod
    def _validate_candidate(
        definition: ProviderCatalogDefinition,
        bundles: tuple[ProviderRuntimeConfigBundle, ...],
    ) -> tuple[ProviderCatalogDefinition, tuple[ProviderRuntimeConfigBundle, ...]]:
        normalized = ProviderCatalogDefinition(
            default_provider_id=definition.default_provider_id,
            providers=tuple(
                sorted(
                    definition.providers,
                    key=lambda item: item.descriptor.provider_id,
                )
            ),
        )
        normalized_bundles = tuple(sorted(bundles, key=lambda item: item.provider_id))
        runtime_enabled = {
            bundle.provider_id: bundle.config.enabled for bundle in normalized_bundles
        }
        public_enabled = {
            entry.descriptor.provider_id: entry.enabled for entry in normalized.providers
        }
        if runtime_enabled != public_enabled:
            raise ProviderRuntimeConfigInvalidError(
                "Provider candidate public and runtime projections do not match"
            )
        if len(runtime_enabled) != len(normalized_bundles):
            raise ProviderRuntimeConfigInvalidError(
                "Provider candidate contains duplicate runtime IDs"
            )
        return normalized, normalized_bundles

    @staticmethod
    def _validate_mutation_shape(
        mutation: PreparedProviderMutation,
        current: dict[str, ProviderRuntimeConfigBundle],
        candidate: dict[str, ProviderRuntimeConfigBundle],
        current_default: str,
        candidate_default: str,
    ) -> None:
        added = candidate.keys() - current.keys()
        removed = current.keys() - candidate.keys()
        changed = {
            provider_id
            for provider_id in current.keys() & candidate.keys()
            if current[provider_id] != candidate[provider_id]
        }
        action = mutation.action
        target = mutation.provider_id
        valid = False
        if action is ProviderMutationAction.CREATED:
            valid = added == {target} and not removed and not changed
        elif action is ProviderMutationAction.DELETED:
            valid = removed == {target} and not added and not changed
        elif action in {
            ProviderMutationAction.UPDATED,
            ProviderMutationAction.ENABLED,
            ProviderMutationAction.DISABLED,
        }:
            valid = changed == {target} and not added and not removed
        elif action is ProviderMutationAction.DEFAULT_CHANGED:
            valid = (
                not added
                and not removed
                and not changed
                and current_default != candidate_default == target
            )
        if not valid:
            raise ProviderRuntimeConfigInvalidError(
                "Provider mutation changed fields outside its declared scope"
            )

    @classmethod
    def _mutation_changed_fields(
        cls,
        action: ProviderMutationAction,
        previous: ProviderRuntimeConfigBundle | None,
        current: ProviderRuntimeConfigBundle | None,
    ) -> tuple[str, ...]:
        if action is ProviderMutationAction.DEFAULT_CHANGED:
            return ("default_provider_id",)
        if previous is None and current is not None:
            return tuple(sorted(current.config.model_dump(mode="json")))
        if previous is not None and current is None:
            return tuple(sorted(previous.config.model_dump(mode="json")))
        if previous is None or current is None:
            return ()
        before = previous.config.model_dump(mode="json")
        after = current.config.model_dump(mode="json")
        return tuple(
            sorted(key for key in before.keys() | after.keys() if before.get(key) != after.get(key))
        )

    @classmethod
    def _credential_disposition(
        cls,
        previous: ProviderRuntimeConfigBundle | None,
        current: ProviderRuntimeConfigBundle | None,
        *,
        deleted: bool = False,
    ) -> CredentialAuditDisposition:
        old = cls._credential_reference(previous.config) if previous else None
        new = cls._credential_reference(current.config) if current else None
        if deleted and old is not None:
            return CredentialAuditDisposition.PROVIDER_DELETED_CREDENTIAL_RETAINED
        if old is None and new is None:
            return CredentialAuditDisposition.NOT_APPLICABLE
        if old == new:
            return CredentialAuditDisposition.REFERENCE_UNCHANGED
        if old is None:
            return CredentialAuditDisposition.REFERENCE_ATTACHED
        if new is None:
            return CredentialAuditDisposition.REFERENCE_REMOVED_OLD_RETAINED
        return CredentialAuditDisposition.REFERENCE_CHANGED_OLD_RETAINED

    @staticmethod
    def _credential_reference(config: ProviderConfig) -> object | None:
        return getattr(config, "credential_ref", None)

    @staticmethod
    def _audit_record(
        *,
        provider_id: str,
        action: ProviderConfigAuditAction,
        audit: ProviderConfigAuditContext,
        config_revision: int,
        changed_fields: tuple[str, ...],
        credential_disposition: CredentialAuditDisposition,
        occurred_at: datetime,
    ) -> ProviderConfigAuditEventRecord:
        return ProviderConfigAuditEventRecord(
            event_id=f"pca_{uuid4().hex}",
            provider_id=provider_id,
            action=action.value,
            source=audit.source.value,
            actor_type=audit.actor_type.value,
            config_revision=config_revision,
            changed_fields=list(changed_fields),
            credential_disposition=credential_disposition.value,
            correlation_id=audit.correlation_id,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _audit_event(
        record: ProviderConfigAuditEventRecord,
    ) -> ProviderConfigAuditEvent:
        return ProviderConfigAuditEvent(
            sequence=record.sequence,
            event_id=record.event_id,
            provider_id=record.provider_id,
            action=ProviderConfigAuditAction(record.action),
            source=ProviderConfigAuditSource(record.source),
            actor_type=ProviderConfigActorType(record.actor_type),
            config_revision=record.config_revision,
            changed_fields=tuple(record.changed_fields),
            credential_disposition=CredentialAuditDisposition(record.credential_disposition),
            correlation_id=record.correlation_id,
            occurred_at=SqlAlchemyProviderManagementRepository._as_utc(record.occurred_at),
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
