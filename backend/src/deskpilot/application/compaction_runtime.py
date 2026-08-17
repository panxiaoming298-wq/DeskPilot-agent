"""Deterministic context compaction with source and coverage revalidation."""

from collections.abc import Sequence

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.compaction import (
    CompactionCoverageItem,
    CompactionSnapshot,
    CompactionSnapshotPage,
    CompactionSourceRef,
    CompactionSourceStatus,
    CompactionStatus,
    CompactionStructuredFields,
    CoverageStatus,
)
from deskpilot.domain.context_memory import (
    ContextItem,
    ContextManifest,
    DataClassification,
    TrustClass,
    WorkingMemoryKind,
)
from deskpilot.domain.research import PageSnapshot
from deskpilot.domain.task_plans import TaskContract
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    AgentHandoffRecord,
    CompactionCoverageItemRecord,
    CompactionSnapshotRecord,
    CompactionSourceRefRecord,
    ContextManifestRecord,
    ConversationMessageRecord,
    LongTermMemoryItemRecord,
    ResearchClaimRecord,
    ResearchPageSnapshotRecord,
    TaskContractVersionRecord,
    TaskPlanningStateRecord,
    TaskRecord,
    WorkingMemoryItemRecord,
    utc_now,
)

COMPRESSOR_VERSION = "deskpilot.deterministic-compactor.v1"


class CompactionError(RuntimeError):
    code = "COMPACTION_ERROR"


class CompactionNotFoundError(CompactionError):
    code = "COMPACTION_NOT_FOUND"


class CompactionConflictError(CompactionError):
    code = "COMPACTION_CONFLICT"


class CompactionProofRejectedError(CompactionConflictError):
    code = "COMPACTION_PROOF_REJECTED"


class CompactionRuntime:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create_for_task(
        self, task_id: str, parent_snapshot_id: str | None = None
    ) -> CompactionSnapshot:
        async with self._database.session() as session, session.begin():
            task = await session.get(TaskRecord, task_id)
            if task is None:
                raise CompactionNotFoundError("Task does not exist")
            manifest_record = await session.scalar(
                select(ContextManifestRecord)
                .where(ContextManifestRecord.task_id == task_id)
                .order_by(ContextManifestRecord.created_at.desc())
                .limit(1)
            )
            if manifest_record is None:
                raise CompactionNotFoundError("Task has no Context Manifest to compact")
            manifest = self._manifest_read(manifest_record)
            return await self._create(session, task, manifest.included_items, parent_snapshot_id)

    async def create_from_items(
        self,
        session: AsyncSession,
        task: TaskRecord,
        items: Sequence[ContextItem],
        parent_snapshot_id: str | None = None,
    ) -> CompactionSnapshot:
        return await self._create(session, task, items, parent_snapshot_id)

    async def list_for_task(self, task_id: str) -> CompactionSnapshotPage:
        async with self._database.session() as session, session.begin():
            records = tuple(
                (
                    await session.scalars(
                        select(CompactionSnapshotRecord)
                        .where(CompactionSnapshotRecord.task_id == task_id)
                        .order_by(CompactionSnapshotRecord.created_at.desc())
                    )
                ).all()
            )
            items = [await self._refresh_and_read(session, record) for record in records]
            return CompactionSnapshotPage(items=tuple(items))

    async def get(self, snapshot_id: str) -> CompactionSnapshot:
        async with self._database.session() as session, session.begin():
            record = await session.get(CompactionSnapshotRecord, snapshot_id)
            if record is None:
                raise CompactionNotFoundError("Compaction Snapshot does not exist")
            return await self._refresh_and_read(session, record)

    async def rebuild(self, snapshot_id: str) -> CompactionSnapshot:
        async with self._database.session() as session, session.begin():
            record = await session.get(CompactionSnapshotRecord, snapshot_id)
            if record is None:
                raise CompactionNotFoundError("Compaction Snapshot does not exist")
            current = await self._refresh_and_read(session, record)
            if current.status is not CompactionStatus.ACTIVE:
                return current
            task = await session.get(TaskRecord, current.task_id)
            if task is None:
                raise CompactionNotFoundError("Task does not exist")
            items = tuple(
                ContextItem.model_validate(
                    {
                        "item_id": f"ctx_{sha256_digest(self._source_identity(item))}",
                        "source_type": item.source_type,
                        "source_ref": item.source_ref,
                        "source_version": item.source_version,
                        "content_digest": item.content_digest,
                        "authority_class": item.authority_class,
                        "trust_class": self._trust_for(item.source_type),
                        "classification": item.classification,
                        "token_count": 1,
                        "inclusion_reason": "compaction_rebuild_source",
                    }
                )
                for item in current.source_refs
            )
            return await self._create(session, task, items, current.snapshot_id)

    async def latest_active(self, session: AsyncSession, task_id: str) -> CompactionSnapshot | None:
        record = await session.scalar(
            select(CompactionSnapshotRecord)
            .where(
                CompactionSnapshotRecord.task_id == task_id,
                CompactionSnapshotRecord.status == CompactionStatus.ACTIVE.value,
            )
            .order_by(CompactionSnapshotRecord.created_at.desc())
            .limit(1)
        )
        if record is None:
            return None
        snapshot = await self._refresh_and_read(session, record)
        return snapshot if snapshot.status is CompactionStatus.ACTIVE else None

    async def _create(
        self,
        session: AsyncSession,
        task: TaskRecord,
        items: Sequence[ContextItem],
        parent_snapshot_id: str | None,
    ) -> CompactionSnapshot:
        if parent_snapshot_id is not None:
            parent = await session.get(CompactionSnapshotRecord, parent_snapshot_id)
            if parent is None or parent.task_id != task.task_id:
                raise CompactionConflictError("Parent snapshot belongs to another task")
        source_refs = tuple(
            CompactionSourceRef(
                source_type=item.source_type,
                source_ref=item.source_ref,
                source_version=item.source_version,
                content_digest=item.content_digest,
                authority_class=item.authority_class,
                classification=item.classification,
            )
            for item in sorted(items, key=lambda value: (value.source_type, value.source_ref))
            if item.source_type != "compaction_snapshot"
        )
        if not source_refs:
            raise CompactionConflictError("Compaction requires at least one original source")
        refreshed_source_refs: list[CompactionSourceRef] = []
        for source_ref in source_refs:
            refreshed_source_refs.append(
                await self._with_current_status(session, task.task_id, source_ref)
            )
        source_refs = tuple(refreshed_source_refs)
        structured, coverage = await self._structured_fields(session, task.task_id, source_refs)
        status = self._status(source_refs, coverage)
        classification = max(
            (item.classification for item in source_refs), key=self._classification_rank
        )
        source_set_digest = sha256_digest(
            {"sources": [item.model_dump(mode="json", exclude={"status"}) for item in source_refs]}
        )
        identity = {
            "task_id": task.task_id,
            "parent_snapshot_id": parent_snapshot_id,
            "source_set_digest": source_set_digest,
            "structured_fields": structured,
            "compressor_version": COMPRESSOR_VERSION,
        }
        snapshot_id = f"cps_{sha256_digest(identity)}"
        existing = await session.get(CompactionSnapshotRecord, snapshot_id)
        if existing is not None:
            return await self._refresh_and_read(session, existing)
        created_at = utc_now()
        material = {
            "schema_version": "deskpilot.compaction-snapshot.v1",
            "snapshot_id": snapshot_id,
            "task_id": task.task_id,
            "conversation_id": task.conversation_id,
            "parent_snapshot_id": parent_snapshot_id,
            "source_refs": source_refs,
            "source_set_digest": source_set_digest,
            "structured_fields": structured,
            "narrative_summary": None,
            "coverage_items": coverage,
            "compressor_version": COMPRESSOR_VERSION,
            "classification": classification,
            "status": status,
            "created_at": created_at,
            "stale_at": created_at if status is CompactionStatus.STALE else None,
        }
        digest_material = {
            key: value for key, value in material.items() if key not in {"created_at", "stale_at"}
        }
        snapshot = CompactionSnapshot.model_validate(
            {**material, "snapshot_digest": sha256_digest(digest_material)}
        )
        session.add(
            CompactionSnapshotRecord(
                snapshot_id=snapshot.snapshot_id,
                task_id=snapshot.task_id,
                conversation_id=snapshot.conversation_id,
                parent_snapshot_id=snapshot.parent_snapshot_id,
                source_set_digest=snapshot.source_set_digest,
                structured_fields=snapshot.structured_fields.model_dump(mode="json"),
                narrative_summary=None,
                coverage_manifest=[item.model_dump(mode="json") for item in coverage],
                compressor_version=COMPRESSOR_VERSION,
                classification=snapshot.classification.value,
                status=snapshot.status.value,
                snapshot_digest=snapshot.snapshot_digest,
                created_at=created_at,
                stale_at=snapshot.stale_at,
            )
        )
        await session.flush()
        for ordinal, source_item in enumerate(source_refs):
            session.add(
                CompactionSourceRefRecord(
                    snapshot_id=snapshot.snapshot_id,
                    ordinal=ordinal,
                    **source_item.model_dump(mode="json"),
                )
            )
        for ordinal, coverage_item in enumerate(coverage):
            session.add(
                CompactionCoverageItemRecord(
                    snapshot_id=snapshot.snapshot_id,
                    ordinal=ordinal,
                    field_kind=coverage_item.field_kind,
                    value_digest=coverage_item.value_digest,
                    source_refs=list(coverage_item.source_refs),
                    status=coverage_item.status.value,
                )
            )
        return snapshot

    async def _structured_fields(
        self,
        session: AsyncSession,
        task_id: str,
        sources: Sequence[CompactionSourceRef],
    ) -> tuple[CompactionStructuredFields, tuple[CompactionCoverageItem, ...]]:
        values: dict[str, list[tuple[str, str]]] = {
            "goal": [],
            "active_constraint": [],
            "confirmed_decision": [],
            "open_question": [],
            "artifact_ref": [],
            "evidence_ref": [],
            "active_memory_ref": [],
        }
        for source in sources:
            if source.status is not CompactionSourceStatus.ACTIVE:
                continue
            if source.source_type == "task_contract":
                version = int(source.source_version)
                record = await session.get(TaskContractVersionRecord, (task_id, version))
                if record is None:
                    continue
                contract = TaskContract.model_validate(record.manifest)
                values["goal"].append((contract.normalized_objective, source.source_ref))
                values["active_constraint"].extend(
                    (constraint, source.source_ref) for constraint in contract.constraints
                )
            elif source.source_type == "working_memory":
                memory_id = source.source_ref.rsplit("/", 1)[-1]
                memory = await session.get(WorkingMemoryItemRecord, memory_id)
                if memory is None:
                    continue
                field = {
                    WorkingMemoryKind.CURRENT_GOAL.value: "goal",
                    WorkingMemoryKind.ACTIVE_CONSTRAINT.value: "active_constraint",
                    WorkingMemoryKind.CONFIRMED_DECISION.value: "confirmed_decision",
                    WorkingMemoryKind.OPEN_QUESTION.value: "open_question",
                    WorkingMemoryKind.SELECTED_ARTIFACT.value: "artifact_ref",
                }.get(memory.kind)
                if field is not None:
                    values[field].append((memory.content, source.source_ref))
            elif source.source_type == "verified_claim":
                values["evidence_ref"].append((source.source_ref, source.source_ref))
            elif source.source_type == "long_term_memory":
                values["active_memory_ref"].append((source.source_ref, source.source_ref))
        task_goals = {value for value, ref in values["goal"] if ref.startswith("task-contract://")}
        conflicting_goals = bool(task_goals) and any(
            value not in task_goals
            for value, ref in values["goal"]
            if not ref.startswith("task-contract://")
        )
        coverage: list[CompactionCoverageItem] = []
        deduped: dict[str, tuple[str, ...]] = {}
        for field, pairs in values.items():
            seen: dict[str, list[str]] = {}
            for value, source_ref in pairs:
                seen.setdefault(value, []).append(source_ref)
            deduped[field] = tuple(seen)
            for value, source_refs in seen.items():
                coverage.append(
                    CompactionCoverageItem.model_validate(
                        {
                            "field_kind": field,
                            "value_digest": sha256_digest({"value": value}),
                            "source_refs": tuple(source_refs),
                            "status": (
                                CoverageStatus.CONFLICT
                                if field == "goal" and conflicting_goals
                                else CoverageStatus.COVERED
                            ),
                        }
                    )
                )
        structured = CompactionStructuredFields(
            goals=deduped["goal"],
            active_constraints=deduped["active_constraint"],
            confirmed_decisions=deduped["confirmed_decision"],
            open_questions=deduped["open_question"],
            artifact_refs=deduped["artifact_ref"],
            evidence_refs=deduped["evidence_ref"],
            active_memory_refs=deduped["active_memory_ref"],
        )
        return structured, tuple(coverage)

    async def _refresh_and_read(
        self, session: AsyncSession, record: CompactionSnapshotRecord
    ) -> CompactionSnapshot:
        source_rows = tuple(
            (
                await session.scalars(
                    select(CompactionSourceRefRecord)
                    .where(CompactionSourceRefRecord.snapshot_id == record.snapshot_id)
                    .order_by(CompactionSourceRefRecord.ordinal)
                )
            ).all()
        )
        coverage_rows = tuple(
            (
                await session.scalars(
                    select(CompactionCoverageItemRecord)
                    .where(CompactionCoverageItemRecord.snapshot_id == record.snapshot_id)
                    .order_by(CompactionCoverageItemRecord.ordinal)
                )
            ).all()
        )
        stored = self._snapshot_read(record, source_rows, coverage_rows)
        refreshed_source_list: list[CompactionSourceRef] = []
        for source_ref in stored.source_refs:
            refreshed_source_list.append(
                await self._with_current_status(session, record.task_id, source_ref)
            )
        refreshed_sources = tuple(refreshed_source_list)
        if refreshed_sources == stored.source_refs:
            return stored
        coverage_status = (
            CoverageStatus.STALE
            if any(item.status is not CompactionSourceStatus.ACTIVE for item in refreshed_sources)
            else None
        )
        refreshed_coverage = tuple(
            item.model_copy(update={"status": coverage_status})
            if coverage_status is not None
            else item
            for item in stored.coverage_items
        )
        status = self._status(refreshed_sources, refreshed_coverage)
        stale_at = stored.stale_at or (utc_now() if status is CompactionStatus.STALE else None)
        material = stored.model_dump(mode="json", exclude={"snapshot_digest"})
        material.update(
            source_refs=refreshed_sources,
            coverage_items=refreshed_coverage,
            status=status,
            stale_at=stale_at,
        )
        digest_material = {
            key: value for key, value in material.items() if key not in {"created_at", "stale_at"}
        }
        refreshed = CompactionSnapshot.model_validate(
            {**material, "snapshot_digest": sha256_digest(digest_material)}
        )
        record.status = refreshed.status.value
        record.stale_at = refreshed.stale_at
        record.coverage_manifest = [
            item.model_dump(mode="json") for item in refreshed.coverage_items
        ]
        record.snapshot_digest = refreshed.snapshot_digest
        for source_row, source_item in zip(source_rows, refreshed.source_refs, strict=True):
            source_row.status = source_item.status.value
        for coverage_row, coverage_item in zip(
            coverage_rows, refreshed.coverage_items, strict=True
        ):
            coverage_row.status = coverage_item.status.value
        return refreshed

    @staticmethod
    def _snapshot_read(
        record: CompactionSnapshotRecord,
        source_rows: Sequence[CompactionSourceRefRecord],
        coverage_rows: Sequence[CompactionCoverageItemRecord],
    ) -> CompactionSnapshot:
        try:
            source_refs = tuple(
                CompactionSourceRef.model_validate(
                    {
                        "source_type": source_row.source_type,
                        "source_ref": source_row.source_ref,
                        "source_version": source_row.source_version,
                        "content_digest": source_row.content_digest,
                        "authority_class": source_row.authority_class,
                        "classification": source_row.classification,
                        "status": source_row.status,
                    }
                )
                for source_row in source_rows
            )
            coverage = tuple(
                CompactionCoverageItem.model_validate(
                    {
                        "field_kind": coverage_row.field_kind,
                        "value_digest": coverage_row.value_digest,
                        "source_refs": tuple(coverage_row.source_refs),
                        "status": coverage_row.status,
                    }
                )
                for coverage_row in coverage_rows
            )
            if [item.model_dump(mode="json") for item in coverage] != record.coverage_manifest:
                raise ValueError("Stored coverage rows do not match the manifest")
            return CompactionSnapshot.model_validate(
                {
                    "snapshot_id": record.snapshot_id,
                    "task_id": record.task_id,
                    "conversation_id": record.conversation_id,
                    "parent_snapshot_id": record.parent_snapshot_id,
                    "source_refs": source_refs,
                    "source_set_digest": record.source_set_digest,
                    "structured_fields": record.structured_fields,
                    "narrative_summary": record.narrative_summary,
                    "coverage_items": coverage,
                    "compressor_version": record.compressor_version,
                    "classification": record.classification,
                    "status": record.status,
                    "created_at": record.created_at,
                    "stale_at": record.stale_at,
                    "snapshot_digest": record.snapshot_digest,
                }
            )
        except (ValidationError, ValueError) as error:
            raise CompactionProofRejectedError(
                "Stored Compaction Snapshot proof was rejected"
            ) from error

    async def _with_current_status(
        self, session: AsyncSession, task_id: str, source: CompactionSourceRef
    ) -> CompactionSourceRef:
        status = await self._source_status(session, task_id, source)
        return source.model_copy(update={"status": status})

    @staticmethod
    async def _source_status(
        session: AsyncSession, task_id: str, source: CompactionSourceRef
    ) -> CompactionSourceStatus:
        ref = source.source_ref
        if source.source_type == "task_contract":
            contract_record = await session.get(
                TaskContractVersionRecord, (task_id, int(source.source_version))
            )
            planning = await session.get(TaskPlanningStateRecord, task_id)
            return (
                CompactionSourceStatus.ACTIVE
                if contract_record is not None
                and contract_record.contract_digest == source.content_digest
                and planning is not None
                and planning.active_contract_version == int(source.source_version)
                else CompactionSourceStatus.STALE
            )
        if source.source_type == "handoff":
            handoff_record = await session.get(AgentHandoffRecord, ref.removeprefix("handoff://"))
            return (
                CompactionSourceStatus.ACTIVE
                if handoff_record is not None
                and handoff_record.handoff_digest == source.content_digest
                else CompactionSourceStatus.STALE
            )
        if source.source_type == "working_memory":
            memory_record = await session.get(WorkingMemoryItemRecord, ref.rsplit("/", 1)[-1])
            if memory_record is None or memory_record.status == "deleted":
                return CompactionSourceStatus.DELETED
            return (
                CompactionSourceStatus.ACTIVE
                if memory_record.status == "active"
                and memory_record.content_digest == source.content_digest
                else CompactionSourceStatus.STALE
            )
        if source.source_type == "conversation_message":
            message_record = await session.get(
                ConversationMessageRecord, ref.removeprefix("conversation-message://")
            )
            if message_record is None or message_record.status == "deleted":
                return CompactionSourceStatus.DELETED
            digest = sha256_digest(
                {
                    "content": message_record.content,
                    "content_ref": message_record.content_ref,
                }
            )
            return (
                CompactionSourceStatus.ACTIVE
                if message_record.task_id == task_id and digest == source.content_digest
                else CompactionSourceStatus.OUT_OF_SCOPE
            )
        if source.source_type == "long_term_memory":
            memory_id = ref.removeprefix("memory://").split("/", 1)[0]
            long_term_record = await session.get(LongTermMemoryItemRecord, memory_id)
            if long_term_record is None or long_term_record.status == "deleted":
                return CompactionSourceStatus.DELETED
            return (
                CompactionSourceStatus.ACTIVE
                if long_term_record.status == "active"
                and long_term_record.value_digest == source.content_digest
                and str(long_term_record.version) == source.source_version
                else CompactionSourceStatus.STALE
            )
        if source.source_type == "verified_claim":
            claim_record = await session.get(
                ResearchClaimRecord, ref.removeprefix("verified-claim://")
            )
            return (
                CompactionSourceStatus.ACTIVE
                if claim_record is not None and claim_record.claim_digest == source.content_digest
                else CompactionSourceStatus.STALE
            )
        if source.source_type == "external_untrusted_page_snapshot":
            page_record = await session.get(
                ResearchPageSnapshotRecord, ref.removeprefix("page-snapshot://")
            )
            if page_record is None:
                return CompactionSourceStatus.STALE
            try:
                page = PageSnapshot.model_validate(page_record.manifest)
            except ValidationError:
                return CompactionSourceStatus.STALE
            return (
                CompactionSourceStatus.ACTIVE
                if page.snapshot_digest == page_record.snapshot_digest
                and page.content_digest == source.content_digest
                else CompactionSourceStatus.STALE
            )
        return CompactionSourceStatus.OUT_OF_SCOPE

    @staticmethod
    def _status(
        sources: Sequence[CompactionSourceRef],
        coverage: Sequence[CompactionCoverageItem],
    ) -> CompactionStatus:
        if any(item.status is not CompactionSourceStatus.ACTIVE for item in sources):
            return CompactionStatus.STALE
        if any(item.status is CoverageStatus.CONFLICT for item in coverage):
            return CompactionStatus.CONFLICT
        return CompactionStatus.ACTIVE

    @staticmethod
    def _classification_rank(value: DataClassification) -> int:
        return {
            DataClassification.PUBLIC: 0,
            DataClassification.INTERNAL: 1,
            DataClassification.SENSITIVE: 2,
        }[value]

    @staticmethod
    def _manifest_read(record: ContextManifestRecord) -> ContextManifest:
        try:
            manifest = ContextManifest.model_validate(record.manifest)
        except (ValidationError, ValueError) as error:
            raise CompactionProofRejectedError("Context Manifest proof was rejected") from error
        if manifest.manifest_digest != record.manifest_digest:
            raise CompactionProofRejectedError("Context Manifest digest does not match")
        return manifest

    @staticmethod
    def _trust_for(source_type: str) -> TrustClass:
        return {
            "task_contract": TrustClass.TRUSTED_RUNTIME,
            "handoff": TrustClass.TRUSTED_RUNTIME,
            "working_memory": TrustClass.TRUSTED_USER_INPUT,
            "conversation_message": TrustClass.TRUSTED_USER_INPUT,
            "long_term_memory": TrustClass.TRUSTED_USER_INPUT,
            "verified_claim": TrustClass.TRUSTED_EVIDENCE,
            "external_untrusted_page_snapshot": TrustClass.UNTRUSTED_EXTERNAL_CONTENT,
        }.get(source_type, TrustClass.UNTRUSTED_MODEL_OUTPUT)

    @staticmethod
    def _source_identity(source: CompactionSourceRef) -> dict[str, str]:
        return {
            "source_ref": source.source_ref,
            "content_digest": source.content_digest,
        }
