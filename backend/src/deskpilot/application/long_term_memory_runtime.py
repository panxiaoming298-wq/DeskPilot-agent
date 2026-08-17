"""Trusted reducers for protected, versioned long-term memory."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.provider_runtime_store import RuntimeConfigProtector
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.agent_runtime import AgentResult
from deskpilot.domain.artifact_runtime import DeliveryManifestRead
from deskpilot.domain.context_memory import ContextManifest, DataClassification
from deskpilot.domain.long_term_memory import (
    CreateLongTermMemoryRequest,
    EditLongTermMemoryRequest,
    LongTermMemoryExport,
    LongTermMemoryKind,
    LongTermMemoryPage,
    LongTermMemoryRead,
    LongTermMemoryStatus,
    MemoryConflictRead,
    MemoryCreatedBy,
    MemoryProposalRead,
    MemorySourceType,
    MemoryUsageRead,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentResultRecord,
    DeliveryManifestRecord,
    LongTermMemoryConflictRecord,
    LongTermMemoryItemRecord,
    LongTermMemoryProposalRecord,
    LongTermMemoryTombstoneRecord,
    LongTermMemoryUsageRecord,
    utc_now,
)

MEMORY_POLICY_REFERENCE = "deskpilot.long-term-memory-policy.v1"
DEFAULT_EPISODE_TTL = timedelta(days=30)


class LongTermMemoryError(RuntimeError):
    code = "LONG_TERM_MEMORY_ERROR"


class LongTermMemoryNotFoundError(LongTermMemoryError):
    code = "LONG_TERM_MEMORY_NOT_FOUND"


class LongTermMemoryConflictError(LongTermMemoryError):
    code = "LONG_TERM_MEMORY_CONFLICT"


class LongTermMemoryProofRejectedError(LongTermMemoryConflictError):
    code = "LONG_TERM_MEMORY_PROOF_REJECTED"


@dataclass(frozen=True, slots=True)
class LongTermContextCandidate:
    memory_id: str
    version: int
    key: str
    kind: LongTermMemoryKind
    content: str
    content_digest: str
    classification: DataClassification


class LongTermMemoryRuntime:
    def __init__(self, database: Database, protector: RuntimeConfigProtector) -> None:
        self._database = database
        self._protector = protector

    async def create_user_memory(self, request: CreateLongTermMemoryRequest) -> LongTermMemoryPage:
        source_id = "local-user"
        source_digest = sha256_digest({"source_type": "user_explicit", "source_id": source_id})
        if request.verified_delivery_id is not None:
            async with self._database.session() as session:
                delivery = await self._verified_delivery(session, request.verified_delivery_id)
            source_id = delivery.delivery_id
            source_digest = delivery.manifest_digest
        status = (
            LongTermMemoryStatus.ACTIVE
            if request.kind
            in {
                LongTermMemoryKind.PREFERENCE,
                LongTermMemoryKind.RESTRICTIVE_PERMISSION,
                LongTermMemoryKind.SKILL_TEMPLATE,
            }
            else LongTermMemoryStatus.PENDING_CONFIRMATION
        )
        await self._create_proposal(
            key=request.key,
            kind=request.kind,
            value=request.value,
            source_type=(
                MemorySourceType.VERIFIED_DELIVERY
                if request.verified_delivery_id
                else MemorySourceType.USER_EXPLICIT
            ),
            source_id=source_id,
            source_digest=source_digest,
            created_by=MemoryCreatedBy.USER,
            classification=request.classification,
            confidence=1.0,
            expires_at=request.expires_at,
            activate=status is LongTermMemoryStatus.ACTIVE,
        )
        return await self.list_all()

    async def propose_from_agent_result(
        self,
        *,
        result_id: str,
        key: str,
        kind: LongTermMemoryKind,
        value: str,
        classification: DataClassification = DataClassification.INTERNAL,
        confidence: float = 0.5,
        expires_at: datetime | None = None,
    ) -> MemoryProposalRead:
        async with self._database.session() as session:
            record = await session.get(AgentResultRecord, result_id)
            if record is None:
                raise LongTermMemoryProofRejectedError("Agent Result proof does not exist")
            try:
                result = AgentResult.model_validate(record.manifest)
            except ValidationError as error:
                raise LongTermMemoryProofRejectedError("Agent Result proof is invalid") from error
            if result.result_digest != record.result_digest:
                raise LongTermMemoryProofRejectedError("Agent Result digest does not match")
        return await self._create_proposal(
            key=key,
            kind=kind,
            value=value,
            source_type=MemorySourceType.AGENT_RESULT,
            source_id=result_id,
            source_digest=result.result_digest,
            created_by=MemoryCreatedBy.AGENT,
            classification=classification,
            confidence=confidence,
            expires_at=expires_at,
            activate=False,
        )

    async def propose_verified_episode(self, delivery: DeliveryManifestRead) -> MemoryProposalRead:
        async with self._database.session() as session:
            verified = await self._verified_delivery(session, delivery.delivery_id)
            existing = await session.scalar(
                select(LongTermMemoryProposalRecord).where(
                    LongTermMemoryProposalRecord.source_type
                    == MemorySourceType.VERIFIED_DELIVERY.value,
                    LongTermMemoryProposalRecord.source_id == delivery.delivery_id,
                    LongTermMemoryProposalRecord.kind == LongTermMemoryKind.VERIFIED_EPISODE.value,
                )
            )
            if existing is not None:
                return self._proposal_read(existing)
        return await self._create_proposal(
            key=f"verified_episode.{delivery.task_id}",
            kind=LongTermMemoryKind.VERIFIED_EPISODE,
            value=(
                f"Verified delivery {delivery.delivery_id}; artifact {delivery.artifact_id}; "
                f"revision {delivery.revision_id}."
            ),
            source_type=MemorySourceType.VERIFIED_DELIVERY,
            source_id=delivery.delivery_id,
            source_digest=verified.manifest_digest,
            created_by=MemoryCreatedBy.SYSTEM,
            classification=DataClassification.INTERNAL,
            confidence=1.0,
            expires_at=utc_now() + DEFAULT_EPISODE_TTL,
            activate=False,
        )

    async def confirm(self, proposal_id: str) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            proposal = await session.get(LongTermMemoryProposalRecord, proposal_id)
            if proposal is None:
                raise LongTermMemoryNotFoundError("Memory proposal does not exist")
            if proposal.status == LongTermMemoryStatus.REJECTED.value:
                raise LongTermMemoryConflictError("Rejected proposal cannot be confirmed")
            if proposal.status != LongTermMemoryStatus.CONFIRMED.value:
                await self._activate(session, proposal)
        return await self.list_all()

    async def reject(self, proposal_id: str) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            proposal = await session.get(LongTermMemoryProposalRecord, proposal_id)
            if proposal is None:
                raise LongTermMemoryNotFoundError("Memory proposal does not exist")
            item = await session.scalar(
                select(LongTermMemoryItemRecord).where(
                    LongTermMemoryItemRecord.proposal_id == proposal_id
                )
            )
            if item is not None:
                raise LongTermMemoryConflictError("Active memory must be deleted, not rejected")
            proposal.status = LongTermMemoryStatus.REJECTED.value
            proposal.decided_at = utc_now()
            proposal.value_payload = None
        return await self.list_all()

    async def edit(self, memory_id: str, request: EditLongTermMemoryRequest) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            old = await session.get(LongTermMemoryItemRecord, memory_id)
            if old is None:
                raise LongTermMemoryNotFoundError("Long-term memory does not exist")
            if old.status in {"deleted", "expired"}:
                raise LongTermMemoryConflictError("Deleted or expired memory cannot be edited")
            self._delete_record(session, old, "superseded")
            value = request.value
            classification = DataClassification(request.classification or old.classification)
            source_digest = sha256_digest(
                {"source_type": "user_explicit", "source_id": "local-user-edit"}
            )
            proposal = self._new_proposal(
                key=old.memory_key,
                kind=LongTermMemoryKind(old.kind),
                value=value,
                source_type=MemorySourceType.USER_EXPLICIT,
                source_id="local-user-edit",
                source_digest=source_digest,
                created_by=MemoryCreatedBy.USER,
                classification=classification,
                confidence=1.0,
                expires_at=(
                    request.expires_at if request.expires_at is not None else old.expires_at
                ),
                status=LongTermMemoryStatus.CONFIRMED,
            )
            session.add(proposal)
            await session.flush()
            await self._activate(session, proposal, supersedes_memory_id=memory_id)
        return await self.list_all()

    async def delete(self, memory_id: str) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            item = await session.get(LongTermMemoryItemRecord, memory_id)
            if item is None:
                raise LongTermMemoryNotFoundError("Long-term memory does not exist")
            self._delete_record(session, item, "user_deleted")
        return await self.list_all()

    async def resolve_conflict(
        self, conflict_id: str, selected_memory_id: str
    ) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            conflict = await session.get(LongTermMemoryConflictRecord, conflict_id)
            if conflict is None:
                raise LongTermMemoryNotFoundError("Memory conflict does not exist")
            if conflict.status != "open":
                raise LongTermMemoryConflictError("Memory conflict is already resolved")
            if selected_memory_id not in conflict.memory_ids:
                raise LongTermMemoryConflictError("Selected memory is outside the conflict")
            for memory_id in conflict.memory_ids:
                item = await session.get(LongTermMemoryItemRecord, memory_id)
                if item is None:
                    raise LongTermMemoryProofRejectedError("Conflict member is missing")
                if memory_id == selected_memory_id:
                    item.status = LongTermMemoryStatus.ACTIVE.value
                else:
                    self._delete_record(session, item, "conflict_resolved")
            conflict.status = "resolved"
            conflict.selected_memory_id = selected_memory_id
            conflict.resolved_at = utc_now()
        return await self.list_all()

    async def list_all(self) -> LongTermMemoryPage:
        async with self._database.session() as session, session.begin():
            await self._expire(session)
            items = tuple(
                (
                    await session.scalars(
                        select(LongTermMemoryItemRecord).order_by(
                            LongTermMemoryItemRecord.created_at.desc()
                        )
                    )
                ).all()
            )
            proposals = tuple(
                (
                    await session.scalars(
                        select(LongTermMemoryProposalRecord).order_by(
                            LongTermMemoryProposalRecord.created_at.desc()
                        )
                    )
                ).all()
            )
            conflicts = tuple(
                (
                    await session.scalars(
                        select(LongTermMemoryConflictRecord).order_by(
                            LongTermMemoryConflictRecord.created_at.desc()
                        )
                    )
                ).all()
            )
            usage = tuple(
                (
                    await session.scalars(
                        select(LongTermMemoryUsageRecord).order_by(
                            LongTermMemoryUsageRecord.supplied_at.desc()
                        )
                    )
                ).all()
            )
            return LongTermMemoryPage(
                items=tuple(self._item_read(item) for item in items),
                proposals=tuple(self._proposal_read(item) for item in proposals),
                conflicts=tuple(self._conflict_read(item) for item in conflicts),
                usage=tuple(self._usage_read(item, items) for item in usage),
            )

    async def export(self) -> LongTermMemoryExport:
        page = await self.list_all()
        async with self._database.session() as session:
            tombstones = tuple(
                (
                    await session.scalars(
                        select(LongTermMemoryTombstoneRecord).order_by(
                            LongTermMemoryTombstoneRecord.deleted_at.desc()
                        )
                    )
                ).all()
            )
        material: dict[str, object] = {
            "schema_version": "deskpilot.long-term-memory-export.v1",
            "items": page.items,
            "proposals": page.proposals,
            "conflicts": page.conflicts,
            "usage": page.usage,
            "tombstones": tuple(
                {
                    "tombstone_id": item.tombstone_id,
                    "memory_id": item.memory_id,
                    "memory_key_digest": item.memory_key_digest,
                    "value_digest": item.value_digest,
                    "reason": item.reason,
                    "deleted_at": item.deleted_at,
                }
                for item in tombstones
            ),
        }
        return LongTermMemoryExport.model_validate(
            {
                **material,
                "exported_at": utc_now(),
                "export_digest": sha256_digest(material),
            }
        )

    async def context_candidates(
        self, session: AsyncSession
    ) -> tuple[LongTermContextCandidate, ...]:
        await self._expire(session)
        records = tuple(
            (
                await session.scalars(
                    select(LongTermMemoryItemRecord)
                    .where(LongTermMemoryItemRecord.status == "active")
                    .order_by(LongTermMemoryItemRecord.kind, LongTermMemoryItemRecord.created_at)
                )
            ).all()
        )
        candidates: list[LongTermContextCandidate] = []
        for item in records:
            if item.value_payload is None:
                continue
            value = self._decrypt(item.value_payload, item.value_scheme, item.memory_id)
            self._assert_item(item, value)
            candidates.append(
                LongTermContextCandidate(
                    memory_id=item.memory_id,
                    version=item.version,
                    key=item.memory_key,
                    kind=LongTermMemoryKind(item.kind),
                    content=value,
                    content_digest=item.value_digest,
                    classification=DataClassification(item.classification),
                )
            )
        return tuple(candidates)

    async def record_context_usage(
        self,
        session: AsyncSession,
        manifest: ContextManifest,
        *,
        agent_id: str,
        provider_id: str,
        provider_location: str,
    ) -> None:
        for context_item in manifest.included_items:
            if context_item.source_type != "long_term_memory":
                continue
            memory_id = context_item.source_ref.removeprefix("memory://").split("/", 1)[0]
            usage_identity = {
                "memory_id": memory_id,
                "manifest_id": manifest.manifest_id,
            }
            usage_id = f"mus_{sha256_digest(usage_identity)}"
            if await session.get(LongTermMemoryUsageRecord, usage_id) is not None:
                continue
            session.add(
                LongTermMemoryUsageRecord(
                    usage_id=usage_id,
                    memory_id=memory_id,
                    memory_version=int(context_item.source_version),
                    task_id=manifest.task_id,
                    invocation_id=manifest.invocation_id,
                    context_manifest_id=manifest.manifest_id,
                    agent_id=agent_id,
                    provider_id=provider_id,
                    provider_location=provider_location,
                    purpose="model_context",
                    supplied_at=utc_now(),
                    policy_reference=MEMORY_POLICY_REFERENCE,
                )
            )

    async def _create_proposal(
        self,
        *,
        key: str,
        kind: LongTermMemoryKind,
        value: str,
        source_type: MemorySourceType,
        source_id: str,
        source_digest: str,
        created_by: MemoryCreatedBy,
        classification: DataClassification,
        confidence: float,
        expires_at: datetime | None,
        activate: bool,
    ) -> MemoryProposalRead:
        proposal = self._new_proposal(
            key=key,
            kind=kind,
            value=value,
            source_type=source_type,
            source_id=source_id,
            source_digest=source_digest,
            created_by=created_by,
            classification=classification,
            confidence=confidence,
            expires_at=expires_at,
            status=(
                LongTermMemoryStatus.CONFIRMED
                if activate
                else LongTermMemoryStatus.PENDING_CONFIRMATION
            ),
        )
        async with self._database.session() as session, session.begin():
            session.add(proposal)
            await session.flush()
            if activate:
                await self._activate(session, proposal)
        return self._proposal_read(proposal)

    def _new_proposal(
        self,
        *,
        key: str,
        kind: LongTermMemoryKind,
        value: str,
        source_type: MemorySourceType,
        source_id: str,
        source_digest: str,
        created_by: MemoryCreatedBy,
        classification: DataClassification,
        confidence: float,
        expires_at: datetime | None,
        status: LongTermMemoryStatus,
    ) -> LongTermMemoryProposalRecord:
        now = utc_now()
        proposal_id = f"mpr_{sha256_digest({'nonce': uuid4().hex})}"
        value_digest = sha256_digest({"value": value})
        material = {
            "proposal_id": proposal_id,
            "key": key,
            "kind": kind.value,
            "value_digest": value_digest,
            "source_type": source_type.value,
            "source_id": source_id,
            "source_digest": source_digest,
            "created_by": created_by.value,
            "scope": "user",
            "classification": classification.value,
            "confidence_micros": round(confidence * 1_000_000),
            "expires_at": self._normalized_time(expires_at),
        }
        return LongTermMemoryProposalRecord(
            proposal_id=proposal_id,
            memory_key=key,
            kind=kind.value,
            value_scheme=self._protector.scheme,
            value_payload=self._encrypt(value, proposal_id),
            value_digest=value_digest,
            source_type=source_type.value,
            source_id=source_id,
            source_digest=source_digest,
            created_by=created_by.value,
            scope="user",
            classification=classification.value,
            confidence_micros=round(confidence * 1_000_000),
            status=status.value,
            proposal_digest=sha256_digest(material),
            created_at=now,
            expires_at=expires_at,
            decided_at=now if status is LongTermMemoryStatus.CONFIRMED else None,
        )

    async def _activate(
        self,
        session: AsyncSession,
        proposal: LongTermMemoryProposalRecord,
        *,
        supersedes_memory_id: str | None = None,
    ) -> None:
        existing_item = await session.scalar(
            select(LongTermMemoryItemRecord).where(
                LongTermMemoryItemRecord.proposal_id == proposal.proposal_id
            )
        )
        if existing_item is not None:
            proposal.status = LongTermMemoryStatus.CONFIRMED.value
            proposal.decided_at = proposal.decided_at or utc_now()
            return
        if proposal.value_payload is None:
            raise LongTermMemoryProofRejectedError("Proposal value was erased")
        maximum_version = await session.scalar(
            select(func.max(LongTermMemoryItemRecord.version)).where(
                LongTermMemoryItemRecord.memory_key == proposal.memory_key,
                LongTermMemoryItemRecord.kind == proposal.kind,
            )
        )
        version = int(maximum_version or 0) + 1
        memory_id = f"mem_{sha256_digest({'proposal_id': proposal.proposal_id})}"
        value = self._decrypt(proposal.value_payload, proposal.value_scheme, proposal.proposal_id)
        item_material = {
            "memory_id": memory_id,
            "proposal_id": proposal.proposal_id,
            "key": proposal.memory_key,
            "version": version,
            "kind": proposal.kind,
            "value_digest": proposal.value_digest,
            "source_type": proposal.source_type,
            "source_id": proposal.source_id,
            "source_digest": proposal.source_digest,
            "created_by": proposal.created_by,
            "scope": proposal.scope,
            "classification": proposal.classification,
            "confidence_micros": proposal.confidence_micros,
            "supersedes_memory_id": supersedes_memory_id,
            "expires_at": self._normalized_time(proposal.expires_at),
        }
        item = LongTermMemoryItemRecord(
            memory_id=memory_id,
            proposal_id=proposal.proposal_id,
            memory_key=proposal.memory_key,
            version=version,
            kind=proposal.kind,
            value_scheme=self._protector.scheme,
            value_payload=self._encrypt(value, memory_id),
            value_digest=proposal.value_digest,
            source_type=proposal.source_type,
            source_id=proposal.source_id,
            source_digest=proposal.source_digest,
            created_by=proposal.created_by,
            scope=proposal.scope,
            classification=proposal.classification,
            confidence_micros=proposal.confidence_micros,
            status=LongTermMemoryStatus.ACTIVE.value,
            item_digest=sha256_digest(item_material),
            supersedes_memory_id=supersedes_memory_id,
            created_at=utc_now(),
            expires_at=proposal.expires_at,
            deleted_at=None,
        )
        session.add(item)
        await session.flush()
        peers = tuple(
            (
                await session.scalars(
                    select(LongTermMemoryItemRecord).where(
                        LongTermMemoryItemRecord.memory_key == proposal.memory_key,
                        LongTermMemoryItemRecord.kind == proposal.kind,
                        LongTermMemoryItemRecord.memory_id != memory_id,
                        LongTermMemoryItemRecord.status.in_(("active", "conflict")),
                        LongTermMemoryItemRecord.value_digest != proposal.value_digest,
                    )
                )
            ).all()
        )
        if peers:
            members = tuple(sorted({memory_id, *(peer.memory_id for peer in peers)}))
            for peer in peers:
                peer.status = LongTermMemoryStatus.CONFLICT.value
            item.status = LongTermMemoryStatus.CONFLICT.value
            conflict = await session.scalar(
                select(LongTermMemoryConflictRecord).where(
                    LongTermMemoryConflictRecord.memory_key == proposal.memory_key,
                    LongTermMemoryConflictRecord.kind == proposal.kind,
                    LongTermMemoryConflictRecord.status == "open",
                )
            )
            material = {"key": proposal.memory_key, "kind": proposal.kind, "memory_ids": members}
            if conflict is None:
                session.add(
                    LongTermMemoryConflictRecord(
                        conflict_id=f"mcf_{sha256_digest(material)}",
                        memory_key=proposal.memory_key,
                        kind=proposal.kind,
                        memory_ids=list(members),
                        status="open",
                        selected_memory_id=None,
                        conflict_digest=sha256_digest(material),
                        created_at=utc_now(),
                        resolved_at=None,
                    )
                )
            else:
                conflict.memory_ids = list(members)
                conflict.conflict_digest = sha256_digest(material)
        proposal.status = LongTermMemoryStatus.CONFIRMED.value
        proposal.decided_at = utc_now()

    async def _expire(self, session: AsyncSession) -> None:
        now = utc_now()
        active = tuple(
            (
                await session.scalars(
                    select(LongTermMemoryItemRecord).where(
                        LongTermMemoryItemRecord.status == "active",
                        LongTermMemoryItemRecord.expires_at.is_not(None),
                    )
                )
            ).all()
        )
        for item in active:
            expires_at = item.expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=UTC)
            if expires_at is not None and now >= expires_at:
                item.status = LongTermMemoryStatus.EXPIRED.value

    def _delete_record(
        self, session: AsyncSession, item: LongTermMemoryItemRecord, reason: str
    ) -> None:
        if item.status == LongTermMemoryStatus.DELETED.value:
            return
        now = utc_now()
        item.status = LongTermMemoryStatus.DELETED.value
        item.value_payload = None
        item.deleted_at = now
        session.add(
            LongTermMemoryTombstoneRecord(
                tombstone_id=f"mts_{sha256_digest({'memory_id': item.memory_id})}",
                memory_id=item.memory_id,
                memory_key_digest=sha256_digest({"key": item.memory_key}),
                value_digest=item.value_digest,
                reason=reason,
                deleted_at=now,
            )
        )

    async def _verified_delivery(
        self, session: AsyncSession, delivery_id: str
    ) -> DeliveryManifestRead:
        record = await session.get(DeliveryManifestRecord, delivery_id)
        if record is None:
            raise LongTermMemoryProofRejectedError("Verified delivery proof does not exist")
        try:
            manifest = DeliveryManifestRead.model_validate(record.manifest)
        except ValidationError as error:
            raise LongTermMemoryProofRejectedError("Verified delivery proof is invalid") from error
        if manifest.manifest_digest != record.manifest_digest:
            raise LongTermMemoryProofRejectedError("Verified delivery digest does not match")
        return manifest

    def _encrypt(self, value: str, context: str) -> bytes:
        plaintext = bytearray(value.encode("utf-8"))
        try:
            return self._protector.protect(plaintext, context=f"memory:{context}")
        finally:
            plaintext[:] = b"\x00" * len(plaintext)

    def _decrypt(self, payload: bytes | None, scheme: str, context: str) -> str:
        if payload is None:
            raise LongTermMemoryProofRejectedError("Protected memory value is unavailable")
        if scheme != self._protector.scheme:
            raise LongTermMemoryProofRejectedError("Memory protection scheme does not match")
        plaintext = self._protector.unprotect(payload, context=f"memory:{context}")
        try:
            return plaintext.decode("utf-8")
        finally:
            plaintext[:] = b"\x00" * len(plaintext)

    def _proposal_read(self, record: LongTermMemoryProposalRecord) -> MemoryProposalRead:
        value = (
            self._decrypt(record.value_payload, record.value_scheme, record.proposal_id)
            if record.value_payload is not None
            else None
        )
        self._assert_proposal(record, value)
        return MemoryProposalRead.model_validate(
            {
                "proposal_id": record.proposal_id,
                "key": record.memory_key,
                "kind": record.kind,
                "value": value,
                "source_type": record.source_type,
                "source_id": record.source_id,
                "source_digest": record.source_digest,
                "created_by": record.created_by,
                "scope": record.scope,
                "classification": record.classification,
                "confidence": record.confidence_micros / 1_000_000,
                "status": record.status,
                "value_digest": record.value_digest,
                "proposal_digest": record.proposal_digest,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "decided_at": record.decided_at,
            }
        )

    def _item_read(self, record: LongTermMemoryItemRecord) -> LongTermMemoryRead:
        value = (
            self._decrypt(record.value_payload, record.value_scheme, record.memory_id)
            if record.value_payload is not None
            else None
        )
        self._assert_item(record, value)
        return LongTermMemoryRead.model_validate(
            {
                "memory_id": record.memory_id,
                "proposal_id": record.proposal_id,
                "key": record.memory_key,
                "version": record.version,
                "kind": record.kind,
                "value": value,
                "source_type": record.source_type,
                "source_id": record.source_id,
                "source_digest": record.source_digest,
                "created_by": record.created_by,
                "scope": record.scope,
                "classification": record.classification,
                "confidence": record.confidence_micros / 1_000_000,
                "status": record.status,
                "value_digest": record.value_digest,
                "item_digest": record.item_digest,
                "supersedes_memory_id": record.supersedes_memory_id,
                "created_at": record.created_at,
                "expires_at": record.expires_at,
                "deleted_at": record.deleted_at,
            }
        )

    @staticmethod
    def _conflict_read(record: LongTermMemoryConflictRecord) -> MemoryConflictRead:
        return MemoryConflictRead.model_validate(
            {
                "conflict_id": record.conflict_id,
                "key": record.memory_key,
                "kind": record.kind,
                "memory_ids": tuple(record.memory_ids),
                "status": record.status,
                "selected_memory_id": record.selected_memory_id,
                "conflict_digest": record.conflict_digest,
                "created_at": record.created_at,
                "resolved_at": record.resolved_at,
            }
        )

    @staticmethod
    def _usage_read(
        record: LongTermMemoryUsageRecord,
        items: tuple[LongTermMemoryItemRecord, ...],
    ) -> MemoryUsageRead:
        by_id = {item.memory_id: item for item in items}
        return MemoryUsageRead(
            usage_id=record.usage_id,
            memory_id=record.memory_id,
            memory_version=record.memory_version,
            task_id=record.task_id,
            invocation_id=record.invocation_id,
            context_manifest_id=record.context_manifest_id,
            agent_id=record.agent_id,
            provider_id=record.provider_id,
            provider_location=record.provider_location,
            purpose=record.purpose,
            supplied_at=record.supplied_at,
            policy_reference=record.policy_reference,
            deleted_after_use=(
                by_id.get(record.memory_id) is not None
                and by_id[record.memory_id].status == LongTermMemoryStatus.DELETED.value
            ),
        )

    @staticmethod
    def _normalized_time(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC).isoformat()

    def _assert_proposal(self, record: LongTermMemoryProposalRecord, value: str | None) -> None:
        material = {
            "proposal_id": record.proposal_id,
            "key": record.memory_key,
            "kind": record.kind,
            "value_digest": record.value_digest,
            "source_type": record.source_type,
            "source_id": record.source_id,
            "source_digest": record.source_digest,
            "created_by": record.created_by,
            "scope": record.scope,
            "classification": record.classification,
            "confidence_micros": record.confidence_micros,
            "expires_at": self._normalized_time(record.expires_at),
        }
        if record.proposal_digest != sha256_digest(material):
            raise LongTermMemoryProofRejectedError("Memory proposal digest does not match")
        if value is not None and record.value_digest != sha256_digest({"value": value}):
            raise LongTermMemoryProofRejectedError("Memory proposal value digest does not match")

    def _assert_item(self, record: LongTermMemoryItemRecord, value: str | None) -> None:
        material = {
            "memory_id": record.memory_id,
            "proposal_id": record.proposal_id,
            "key": record.memory_key,
            "version": record.version,
            "kind": record.kind,
            "value_digest": record.value_digest,
            "source_type": record.source_type,
            "source_id": record.source_id,
            "source_digest": record.source_digest,
            "created_by": record.created_by,
            "scope": record.scope,
            "classification": record.classification,
            "confidence_micros": record.confidence_micros,
            "supersedes_memory_id": record.supersedes_memory_id,
            "expires_at": self._normalized_time(record.expires_at),
        }
        if record.item_digest != sha256_digest(material):
            raise LongTermMemoryProofRejectedError("Memory item digest does not match")
        if value is not None and record.value_digest != sha256_digest({"value": value}):
            raise LongTermMemoryProofRejectedError("Memory item value digest does not match")
