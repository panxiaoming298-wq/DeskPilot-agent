"""SQLAlchemy persistence for protected Provider runtime configuration."""

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import delete, select, update

from deskpilot.application.provider_runtime_codec import ProviderRuntimeConfigCodec
from deskpilot.application.provider_runtime_store import (
    ProviderRuntimeConfigNotFoundError,
    ProviderRuntimeConfigVersionConflictError,
)
from deskpilot.domain.provider_config import ProviderConfig
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
    ProviderConfigAuditEventRecord,
    ProviderRuntimeConfigRecord,
    utc_now,
)


class SqlAlchemyProviderRuntimeConfigRepository:
    """Atomically persist protected payload changes and value-free audit events."""

    def __init__(
        self,
        database: Database,
        codec: ProviderRuntimeConfigCodec,
    ) -> None:
        self._database = database
        self._codec = codec

    async def put(
        self,
        bundle: ProviderRuntimeConfigBundle,
        *,
        audit: ProviderConfigAuditContext,
        expected_revision: int | None = None,
    ) -> ProviderRuntimeConfigSnapshot:
        self._validate_expected_revision(expected_revision)
        timestamp = utc_now()

        async with self._database.session() as session:
            async with session.begin():
                record = await session.get(
                    ProviderRuntimeConfigRecord,
                    bundle.provider_id,
                )
                actual_revision = record.revision if record is not None else 0
                self._enforce_revision(expected_revision, actual_revision)

                previous_bundle = (
                    self._decode_record(record) if record is not None else None
                )
                if previous_bundle == bundle and record is not None:
                    return self._snapshot(record, bundle)

                protected = self._codec.encode(bundle)
                next_revision = actual_revision + 1
                if record is None:
                    record = ProviderRuntimeConfigRecord(
                        provider_id=bundle.provider_id,
                        config_kind=bundle.config.kind,
                        payload_schema_version=bundle.schema_version,
                        protection_scheme=protected.scheme,
                        protected_payload=protected.payload,
                        revision=next_revision,
                        created_at=timestamp,
                        updated_at=timestamp,
                    )
                    session.add(record)
                    action = ProviderConfigAuditAction.CREATED
                else:
                    updated_revision = await session.scalar(
                        update(ProviderRuntimeConfigRecord)
                        .where(
                            ProviderRuntimeConfigRecord.provider_id
                            == bundle.provider_id,
                            ProviderRuntimeConfigRecord.revision
                            == actual_revision,
                        )
                        .values(
                            config_kind=bundle.config.kind,
                            payload_schema_version=bundle.schema_version,
                            protection_scheme=protected.scheme,
                            protected_payload=protected.payload,
                            revision=next_revision,
                            updated_at=timestamp,
                        )
                        .returning(ProviderRuntimeConfigRecord.revision)
                    )
                    if updated_revision != next_revision:
                        latest_revision = await self._current_revision(
                            session,
                            bundle.provider_id,
                        )
                        raise ProviderRuntimeConfigVersionConflictError(
                            expected_revision=actual_revision,
                            actual_revision=latest_revision,
                        )
                    record.config_kind = bundle.config.kind
                    record.payload_schema_version = bundle.schema_version
                    record.protection_scheme = protected.scheme
                    record.protected_payload = protected.payload
                    record.revision = next_revision
                    record.updated_at = timestamp
                    action = ProviderConfigAuditAction.UPDATED

                audit_record = self._audit_record(
                    provider_id=bundle.provider_id,
                    action=action,
                    audit=audit,
                    config_revision=next_revision,
                    changed_fields=self._changed_fields(previous_bundle, bundle),
                    credential_disposition=self._credential_disposition(
                        previous_bundle,
                        bundle,
                    ),
                    occurred_at=timestamp,
                )
                session.add(audit_record)
                await session.flush()
                return self._snapshot(record, bundle)

    async def get(self, provider_id: str) -> ProviderRuntimeConfigSnapshot:
        async with self._database.session() as session:
            record = await session.get(ProviderRuntimeConfigRecord, provider_id)
            if record is None:
                raise ProviderRuntimeConfigNotFoundError(provider_id)
            return self._snapshot(record, self._decode_record(record))

    async def delete(
        self,
        provider_id: str,
        *,
        audit: ProviderConfigAuditContext,
        expected_revision: int | None = None,
    ) -> bool:
        self._validate_expected_revision(expected_revision)
        timestamp = utc_now()

        async with self._database.session() as session:
            async with session.begin():
                record = await session.get(ProviderRuntimeConfigRecord, provider_id)
                actual_revision = record.revision if record is not None else 0
                self._enforce_revision(expected_revision, actual_revision)
                if record is None:
                    return False

                bundle = self._decode_record(record)
                deleted_provider_id = await session.scalar(
                    delete(ProviderRuntimeConfigRecord).where(
                        ProviderRuntimeConfigRecord.provider_id == provider_id,
                        ProviderRuntimeConfigRecord.revision == actual_revision,
                    ).returning(ProviderRuntimeConfigRecord.provider_id)
                )
                if deleted_provider_id != provider_id:
                    latest_revision = await self._current_revision(
                        session,
                        provider_id,
                    )
                    raise ProviderRuntimeConfigVersionConflictError(
                        expected_revision=actual_revision,
                        actual_revision=latest_revision,
                    )
                credential = self._credential_reference(bundle.config)
                session.add(
                    self._audit_record(
                        provider_id=provider_id,
                        action=ProviderConfigAuditAction.DELETED,
                        audit=audit,
                        config_revision=actual_revision + 1,
                        changed_fields=tuple(
                            sorted(bundle.config.model_dump(mode="json"))
                        ),
                        credential_disposition=(
                            CredentialAuditDisposition.PROVIDER_DELETED_CREDENTIAL_RETAINED
                            if credential is not None
                            else CredentialAuditDisposition.NOT_APPLICABLE
                        ),
                        occurred_at=timestamp,
                    )
                )
                await session.flush()
                return True

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
            statement = statement.where(
                ProviderConfigAuditEventRecord.provider_id == provider_id
            )
        async with self._database.session() as session:
            records = (await session.scalars(statement)).all()
        return tuple(self._audit_event(record) for record in records)

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
            created_at=SqlAlchemyProviderRuntimeConfigRepository._as_utc(
                record.created_at
            ),
            updated_at=SqlAlchemyProviderRuntimeConfigRepository._as_utc(
                record.updated_at
            ),
            bundle=bundle,
        )

    @staticmethod
    def _changed_fields(
        previous: ProviderRuntimeConfigBundle | None,
        current: ProviderRuntimeConfigBundle,
    ) -> tuple[str, ...]:
        current_fields = current.config.model_dump(mode="json")
        if previous is None:
            return tuple(sorted(current_fields))
        previous_fields = previous.config.model_dump(mode="json")
        return tuple(
            sorted(
                key
                for key in current_fields.keys() | previous_fields.keys()
                if current_fields.get(key) != previous_fields.get(key)
            )
        )

    @classmethod
    def _credential_disposition(
        cls,
        previous: ProviderRuntimeConfigBundle | None,
        current: ProviderRuntimeConfigBundle,
    ) -> CredentialAuditDisposition:
        old = cls._credential_reference(previous.config) if previous else None
        new = cls._credential_reference(current.config)
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
            credential_disposition=CredentialAuditDisposition(
                record.credential_disposition
            ),
            correlation_id=record.correlation_id,
            occurred_at=SqlAlchemyProviderRuntimeConfigRepository._as_utc(
                record.occurred_at
            ),
        )

    @staticmethod
    async def _current_revision(session: object, provider_id: str) -> int:
        from sqlalchemy.ext.asyncio import AsyncSession

        if not isinstance(session, AsyncSession):
            raise TypeError("Expected an async SQLAlchemy session")
        revision = await session.scalar(
            select(ProviderRuntimeConfigRecord.revision).where(
                ProviderRuntimeConfigRecord.provider_id == provider_id
            )
        )
        return revision or 0

    @staticmethod
    def _validate_expected_revision(expected_revision: int | None) -> None:
        if expected_revision is not None and expected_revision < 0:
            raise ValueError("Expected Provider runtime revision cannot be negative")

    @staticmethod
    def _enforce_revision(
        expected_revision: int | None,
        actual_revision: int,
    ) -> None:
        if expected_revision is not None and expected_revision != actual_revision:
            raise ProviderRuntimeConfigVersionConflictError(
                expected_revision=expected_revision,
                actual_revision=actual_revision,
            )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
