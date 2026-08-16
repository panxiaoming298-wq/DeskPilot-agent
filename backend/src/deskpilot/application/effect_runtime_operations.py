"""Protected queries, retention and immutable audit for effect-runtime operations."""

import asyncio
import base64
import binascii
import json
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, cast
from uuid import uuid4

from sqlalchemy import delete, exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.core.canonical_json import canonical_json_bytes, sha256_digest
from deskpilot.domain.effect_graph import EFFECT_DAG_SCHEMA_VERSION, EffectGraphStatus
from deskpilot.domain.effect_runtime_operations import (
    AdmissionOperationsMetrics,
    AdmissionOperationsRead,
    EffectRuntimeAuditEventRead,
    EffectRuntimeAuditExportPage,
    EffectRuntimeAuditPage,
    EffectRuntimeOperationsSnapshot,
    GraphControlOperationsMetrics,
    GraphControlOperationsRead,
    MetricsAuditResult,
    OperationsAlert,
    OperationsAlertNotificationPage,
    OperationsAlertNotificationRead,
    OperationsAlertSeverity,
    OperationsAlertTransition,
    OutboxOperationsMetrics,
    OutboxOperationsRead,
    OutboxRequeueResult,
    ReadyProjectionOperationsMetrics,
    ReadyProjectionOperationsRead,
    RetentionCounts,
    RetentionRunResult,
)
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import (
    EffectRuntimeAlertNotificationRecord,
    EffectRuntimeAlertStateRecord,
    EffectRuntimeOperationsAuditRecord,
    EffectRuntimeOperationsStateRecord,
    InboxDeliveryRecord,
    OutboxMessageRecord,
    ToolEffectDagAdmissionRecord,
    ToolEffectDagAdmissionShardRecord,
    ToolEffectDagAdmissionStateRecord,
    ToolEffectDagReadyNodeRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectGraphControlRecord,
    ToolEffectGraphRecord,
    ToolEffectNodeRecord,
    ToolEffectReadySetCheckpointRecord,
)

logger = logging.getLogger(__name__)
_OPERATIONS_SCOPE = "effect_runtime"
_TERMINAL_RETENTION_GRAPH_STATUSES = (
    EffectGraphStatus.SUCCEEDED.value,
    EffectGraphStatus.COMPENSATED.value,
    EffectGraphStatus.FAILED.value,
    EffectGraphStatus.CANCELLED.value,
)
_LIVE_GRAPH_STATUSES = (
    EffectGraphStatus.ACTIVE.value,
    EffectGraphStatus.COMPENSATING.value,
)
_TERMINAL_ADMISSION_STATUSES = (
    "released",
    "cancelled",
    "withdrawn",
    "expired",
)
_AUDIT_EXPORT_CURSOR_SCHEMA_VERSION = "deskpilot.effect-runtime-audit-export-cursor.v1"
_AUDIT_EXPORT_SCHEMA_VERSION = "deskpilot.effect-runtime-audit-export.v1"


class EffectRuntimeOperationsIdempotencyConflictError(RuntimeError):
    code = "EFFECT_RUNTIME_OPERATIONS_IDEMPOTENCY_CONFLICT"


class EffectRuntimeOperationsAuditRejectedError(RuntimeError):
    code = "EFFECT_RUNTIME_OPERATIONS_AUDIT_REJECTED"


class OutboxDeadLetterNotFoundError(LookupError):
    code = "OUTBOX_DEAD_LETTER_NOT_FOUND"

    def __init__(self, message_id: str) -> None:
        super().__init__(f"Outbox dead letter not found: {message_id}")
        self.message_id = message_id


class EffectRuntimeOperationsService:
    """One authenticated, secret-free operational view over durable runtime state."""

    def __init__(
        self,
        database: Database,
        *,
        outbox_notify: Callable[[], None] | None = None,
        alert_notify: Callable[[tuple[OperationsAlertNotificationRead, ...]], None] | None = None,
        retention_days: int = 30,
        retention_interval_seconds: float = 3_600,
        metrics_interval_seconds: float = 300,
        retention_batch_size: int = 1_000,
        stalled_after_seconds: float = 60,
    ) -> None:
        if not 1 <= retention_days <= 3_650:
            raise ValueError("Effect-runtime retention days are invalid")
        if not 1 <= retention_interval_seconds <= 604_800:
            raise ValueError("Effect-runtime retention interval is invalid")
        if not 1 <= metrics_interval_seconds <= 86_400:
            raise ValueError("Effect-runtime metrics interval is invalid")
        if not 1 <= retention_batch_size <= 10_000:
            raise ValueError("Effect-runtime retention batch size is invalid")
        if not 1 <= stalled_after_seconds <= 86_400:
            raise ValueError("Effect-runtime stalled threshold is invalid")
        self._database = database
        self._outbox_notify = outbox_notify
        self._alert_notify = alert_notify
        self._retention_days = retention_days
        self._retention_interval = retention_interval_seconds
        self._metrics_interval = metrics_interval_seconds
        self._retention_batch_size = retention_batch_size
        self._stalled_after_seconds = stalled_after_seconds
        self._mutation_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Effect-runtime operations scheduler already started")
        self._stopping = False
        self._runner = asyncio.create_task(
            self._run_scheduler(),
            name="effect-runtime-operations",
        )

    async def shutdown(self) -> None:
        if self._runner is None:
            return
        self._stopping = True
        self._wake.set()
        await self._runner
        self._runner = None

    async def snapshot(self, *, sample_limit: int = 50) -> EffectRuntimeOperationsSnapshot:
        self._validate_limit(sample_limit)
        async with self._database.session() as session:
            return await self._snapshot(session, sample_limit=sample_limit)

    async def sample_metrics(
        self,
        *,
        actor_id: str,
        sample_limit: int = 50,
    ) -> MetricsAuditResult:
        self._validate_actor(actor_id)
        self._validate_limit(sample_limit)
        notifications: tuple[OperationsAlertNotificationRead, ...] = ()
        async with self._mutation_lock:
            async with self._database.session() as session:
                async with session.begin():
                    snapshot = await self._snapshot(session, sample_limit=sample_limit)
                    request_digest = sha256_digest(
                        {
                            "schema_version": "deskpilot.effect-runtime-metrics-request.v1",
                            "sample_limit": sample_limit,
                        }
                    )
                    event = await self._append_audit(
                        session,
                        action="metrics.sampled",
                        actor_id=actor_id,
                        request_digest=request_digest,
                        result_digest=snapshot.snapshot_digest,
                        details={
                            "snapshot": snapshot.model_dump(mode="json"),
                        },
                    )
                    notifications = await self._reconcile_alerts(
                        session,
                        snapshot=snapshot,
                        audit_event=event,
                    )
                    result = MetricsAuditResult(
                        snapshot=snapshot,
                        audit_event=event,
                        alert_notifications=notifications,
                    )
        if notifications and self._alert_notify is not None:
            try:
                self._alert_notify(notifications)
            except Exception:
                logger.exception("Effect-runtime alert wake notification failed")
        return result

    async def audit_page(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> EffectRuntimeAuditPage:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("Effect-runtime audit page is invalid")
        async with self._database.session() as session:
            state = await session.get(EffectRuntimeOperationsStateRecord, _OPERATIONS_SCOPE)
            if state is None:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            through_sequence = state.next_sequence - 1
            through_digest = state.last_event_digest
            records = tuple(
                (
                    await session.scalars(
                        select(EffectRuntimeOperationsAuditRecord)
                        .where(
                            EffectRuntimeOperationsAuditRecord.sequence > after_sequence,
                            EffectRuntimeOperationsAuditRecord.sequence <= through_sequence,
                        )
                        .order_by(EffectRuntimeOperationsAuditRecord.sequence)
                        .limit(limit + 1)
                    )
                ).all()
            )
            await self._validate_audit_page(
                session,
                records=records,
                after_sequence=after_sequence,
                limit=limit,
                through_sequence=through_sequence,
                through_digest=through_digest,
            )
        has_more = len(records) > limit
        page = records[:limit]
        return EffectRuntimeAuditPage(
            events=tuple(self._to_audit_event(record) for record in page),
            next_after_sequence=(page[-1].sequence if page else after_sequence),
            has_more=has_more,
        )

    async def alert_notification_page(
        self,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> OperationsAlertNotificationPage:
        if after_sequence < 0 or not 1 <= limit <= 500:
            raise ValueError("Effect-runtime alert notification page is invalid")
        async with self._database.session() as session:
            state = await session.get(EffectRuntimeOperationsStateRecord, _OPERATIONS_SCOPE)
            if state is None:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            through_sequence = state.next_alert_sequence - 1
            through_digest = state.last_alert_event_digest
            records = tuple(
                (
                    await session.scalars(
                        select(EffectRuntimeAlertNotificationRecord)
                        .where(
                            EffectRuntimeAlertNotificationRecord.sequence > after_sequence,
                            EffectRuntimeAlertNotificationRecord.sequence <= through_sequence,
                        )
                        .order_by(EffectRuntimeAlertNotificationRecord.sequence)
                        .limit(limit + 1)
                    )
                ).all()
            )
            await self._validate_alert_notification_page(
                session,
                records=records,
                after_sequence=after_sequence,
                limit=limit,
                through_sequence=through_sequence,
                through_digest=through_digest,
            )
        has_more = len(records) > limit
        page = records[:limit]
        return OperationsAlertNotificationPage(
            notifications=tuple(self._to_alert_notification(record) for record in page),
            next_after_sequence=(page[-1].sequence if page else after_sequence),
            has_more=has_more,
        )

    async def audit_export_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 500,
    ) -> EffectRuntimeAuditExportPage:
        if not 1 <= limit <= 500:
            raise ValueError("Effect-runtime audit export page is invalid")
        async with self._database.session() as session:
            if cursor is None:
                state = await session.get(EffectRuntimeOperationsStateRecord, _OPERATIONS_SCOPE)
                if state is None:
                    raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
                database_time = await database_utc_now(session)
                through_sequence = state.next_sequence - 1
                through_digest = state.last_event_digest
                after_sequence = 0
                after_digest: str | None = None
                export_id = self._audit_export_id(
                    database_time=database_time,
                    through_sequence=through_sequence,
                    through_digest=through_digest,
                )
            else:
                cursor_material = self._decode_audit_export_cursor(cursor)
                database_time = datetime.fromisoformat(cast(str, cursor_material["database_time"]))
                through_sequence = cast(int, cursor_material["through_sequence"])
                through_digest = cast(str | None, cursor_material["through_event_digest"])
                after_sequence = cast(int, cursor_material["after_sequence"])
                after_digest = cast(str | None, cursor_material["after_event_digest"])
                export_id = cast(str, cursor_material["export_id"])
                if export_id != self._audit_export_id(
                    database_time=database_time,
                    through_sequence=through_sequence,
                    through_digest=through_digest,
                ):
                    raise EffectRuntimeOperationsAuditRejectedError(export_id)
            records = tuple(
                (
                    await session.scalars(
                        select(EffectRuntimeOperationsAuditRecord)
                        .where(
                            EffectRuntimeOperationsAuditRecord.sequence > after_sequence,
                            EffectRuntimeOperationsAuditRecord.sequence <= through_sequence,
                        )
                        .order_by(EffectRuntimeOperationsAuditRecord.sequence)
                        .limit(limit + 1)
                    )
                ).all()
            )
            await self._validate_audit_export_page(
                session,
                records=records,
                after_sequence=after_sequence,
                after_digest=after_digest,
                through_sequence=through_sequence,
                through_digest=through_digest,
                limit=limit,
            )
        has_more = len(records) > limit
        page = records[:limit]
        events = tuple(self._to_audit_event(record) for record in page)
        page_material: dict[str, object] = {
            "schema_version": _AUDIT_EXPORT_SCHEMA_VERSION,
            "export_id": export_id,
            "database_time": self._as_utc(database_time).isoformat(),
            "through_sequence": through_sequence,
            "through_event_digest": through_digest,
            "after_sequence": after_sequence,
            "after_event_digest": after_digest,
            "events": [event.model_dump(mode="json") for event in events],
        }
        next_cursor = None
        if has_more:
            last = page[-1]
            next_cursor = self._encode_audit_export_cursor(
                export_id=export_id,
                database_time=database_time,
                through_sequence=through_sequence,
                through_digest=through_digest,
                after_sequence=last.sequence,
                after_digest=last.event_digest,
            )
        page_material["has_more"] = has_more
        page_material["next_cursor"] = next_cursor
        return EffectRuntimeAuditExportPage(
            export_id=export_id,
            database_time=self._as_utc(database_time),
            through_sequence=through_sequence,
            through_event_digest=through_digest,
            events=events,
            page_digest=sha256_digest(page_material),
            next_cursor=next_cursor,
            has_more=has_more,
        )

    async def run_retention(
        self,
        *,
        actor_id: str,
        idempotency_key: str,
        retention_days: int | None = None,
    ) -> RetentionRunResult:
        self._validate_actor(actor_id)
        days = retention_days if retention_days is not None else self._retention_days
        if not 1 <= days <= 3_650:
            raise ValueError("Effect-runtime retention days are invalid")
        key_digest = self._idempotency_digest(idempotency_key)
        request_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-retention-request.v1",
                "retention_days": days,
                "batch_size": self._retention_batch_size,
            }
        )
        async with self._mutation_lock:
            async with self._database.session() as session:
                async with session.begin():
                    await self._lock_audit_state(session)
                    replay = await self._idempotent_event(
                        session,
                        action="retention.completed",
                        idempotency_key_digest=key_digest,
                        request_digest=request_digest,
                    )
                    if replay is not None:
                        return self._retention_from_event(replay)
                    database_now = await database_utc_now(session)
                    cutoff = database_now - timedelta(days=days)
                    counts, manifest_digest = await self._apply_retention(
                        session,
                        cutoff=cutoff,
                    )
                    result_digest = sha256_digest(
                        {
                            "schema_version": "deskpilot.effect-runtime-retention-result.v1",
                            "cutoff": cutoff.isoformat(),
                            "counts": counts.model_dump(mode="json"),
                            "manifest_digest": manifest_digest,
                        }
                    )
                    event = await self._append_audit(
                        session,
                        action="retention.completed",
                        actor_id=actor_id,
                        request_digest=request_digest,
                        result_digest=result_digest,
                        details={
                            "cutoff": cutoff.isoformat(),
                            "counts": counts.model_dump(mode="json"),
                            "manifest_digest": manifest_digest,
                        },
                        idempotency_key_digest=key_digest,
                        retention_at=database_now,
                    )
                    return RetentionRunResult(
                        cutoff=cutoff,
                        counts=counts,
                        manifest_digest=manifest_digest,
                        audit_event=event,
                    )

    async def requeue_dead_letter(
        self,
        message_id: str,
        *,
        actor_id: str,
        idempotency_key: str,
    ) -> OutboxRequeueResult:
        if not 1 <= len(message_id) <= 40:
            raise ValueError("Outbox message ID is invalid")
        self._validate_actor(actor_id)
        key_digest = self._idempotency_digest(idempotency_key)
        request_digest = sha256_digest(
            {
                "schema_version": "deskpilot.outbox-dead-letter-requeue-request.v1",
                "message_id": message_id,
            }
        )
        async with self._mutation_lock:
            async with self._database.session() as session:
                async with session.begin():
                    await self._lock_audit_state(session)
                    replay = await self._idempotent_event(
                        session,
                        action="outbox.dead_letter.requeued",
                        idempotency_key_digest=key_digest,
                        request_digest=request_digest,
                    )
                    if replay is not None:
                        return self._requeue_from_event(replay)
                    record = await session.get(
                        OutboxMessageRecord,
                        message_id,
                        with_for_update=True,
                    )
                    if (
                        record is None
                        or record.published_at is not None
                        or record.dead_lettered_at is None
                    ):
                        raise OutboxDeadLetterNotFoundError(message_id)
                    database_now = await database_utc_now(session)
                    previous_digest = sha256_digest(
                        {
                            "message_id": record.message_id,
                            "attempt_count": record.attempt_count,
                            "dead_lettered_at": self._iso(record.dead_lettered_at),
                            "reason": record.dead_letter_reason,
                            "claim_fencing_token": record.claim_fencing_token,
                        }
                    )
                    record.attempt_count = 0
                    record.available_at = database_now
                    record.last_error = None
                    record.dead_lettered_at = None
                    record.dead_letter_reason = None
                    record.delivery_id = None
                    record.delivery_attempted_at = None
                    record.claim_owner_id = None
                    record.claim_acquired_at = None
                    record.claim_expires_at = None
                    record.claim_fencing_token += 1
                    await session.flush()
                    details: dict[str, object] = {
                        "message_id": record.message_id,
                        "attempt_count": record.attempt_count,
                        "claim_fencing_token": record.claim_fencing_token,
                        "available_at": database_now.isoformat(),
                        "previous_dead_letter_digest": previous_digest,
                    }
                    result_digest = sha256_digest(
                        {
                            "schema_version": "deskpilot.outbox-dead-letter-requeue-result.v1",
                            **details,
                        }
                    )
                    event = await self._append_audit(
                        session,
                        action="outbox.dead_letter.requeued",
                        actor_id=actor_id,
                        request_digest=request_digest,
                        result_digest=result_digest,
                        details=details,
                        idempotency_key_digest=key_digest,
                    )
                    result = OutboxRequeueResult(
                        message_id=record.message_id,
                        attempt_count=record.attempt_count,
                        claim_fencing_token=record.claim_fencing_token,
                        available_at=database_now,
                        audit_event=event,
                    )
        if self._outbox_notify is not None:
            self._outbox_notify()
        return result

    async def _snapshot(
        self,
        session: AsyncSession,
        *,
        sample_limit: int,
    ) -> EffectRuntimeOperationsSnapshot:
        database_now = await database_utc_now(session)
        graph_control_metrics = await self._graph_control_metrics(session, database_now)
        admission_metrics = await self._admission_metrics(session, database_now)
        ready_metrics = await self._ready_metrics(session, database_now)
        outbox_metrics = await self._outbox_metrics(session, database_now)
        alerts = self._alerts(
            database_now=database_now,
            graph_controls=graph_control_metrics,
            admissions=admission_metrics,
            ready_projection=ready_metrics,
            outbox=outbox_metrics,
        )
        graph_control_samples = await self._graph_control_samples(session, sample_limit)
        admission_samples = await self._admission_samples(session, sample_limit)
        ready_samples = await self._ready_samples(session, sample_limit)
        outbox_samples = await self._outbox_samples(session, sample_limit, database_now)
        material: dict[str, Any] = {
            "schema_version": "deskpilot.effect-runtime-operations.v1",
            "database_time": database_now.isoformat(),
            "graph_controls": graph_control_metrics.model_dump(mode="json"),
            "admissions": admission_metrics.model_dump(mode="json"),
            "ready_projection": ready_metrics.model_dump(mode="json"),
            "outbox": outbox_metrics.model_dump(mode="json"),
            "alerts": [alert.model_dump(mode="json") for alert in alerts],
            "graph_control_samples": [
                value.model_dump(mode="json") for value in graph_control_samples
            ],
            "admission_samples": [value.model_dump(mode="json") for value in admission_samples],
            "ready_projection_samples": [value.model_dump(mode="json") for value in ready_samples],
            "outbox_samples": [value.model_dump(mode="json") for value in outbox_samples],
        }
        return EffectRuntimeOperationsSnapshot(
            **material,
            snapshot_digest=sha256_digest(material),
        )

    async def _graph_control_metrics(
        self,
        session: AsyncSession,
        database_now: datetime,
    ) -> GraphControlOperationsMetrics:
        counts = await self._status_counts(session, ToolEffectGraphControlRecord.status)
        actionable_condition = (ToolEffectGraphControlRecord.status == "pending") & (
            ToolEffectGraphControlRecord.available_at <= database_now
        )
        actionable = await self._count(session, ToolEffectGraphControlRecord, actionable_condition)
        claim_expired = await self._count(
            session,
            ToolEffectGraphControlRecord,
            (ToolEffectGraphControlRecord.status == "processing")
            & (ToolEffectGraphControlRecord.claim_expires_at.is_not(None))
            & (ToolEffectGraphControlRecord.claim_expires_at <= database_now),
        )
        unrouted = await self._count(
            session,
            ToolEffectGraphControlRecord,
            (ToolEffectGraphControlRecord.status == "pending")
            & (ToolEffectGraphControlRecord.target_owner_id.is_(None)),
        )
        oldest = await session.scalar(
            select(func.min(ToolEffectGraphControlRecord.created_at)).where(
                actionable_condition
                | (
                    (ToolEffectGraphControlRecord.status == "processing")
                    & (ToolEffectGraphControlRecord.claim_expires_at <= database_now)
                )
            )
        )
        return GraphControlOperationsMetrics(
            total=sum(counts.values()),
            pending=counts.get("pending", 0),
            processing=counts.get("processing", 0),
            applied=counts.get("applied", 0),
            superseded=counts.get("superseded", 0),
            actionable=actionable,
            claim_expired=claim_expired,
            unrouted=unrouted,
            oldest_actionable_at=self._as_utc_or_none(oldest),
        )

    async def _admission_metrics(
        self,
        session: AsyncSession,
        database_now: datetime,
    ) -> AdmissionOperationsMetrics:
        counts = await self._status_counts(session, ToolEffectDagAdmissionRecord.status)
        live_pending = await self._count(
            session,
            ToolEffectDagAdmissionRecord,
            (ToolEffectDagAdmissionRecord.status == "pending")
            & (ToolEffectDagAdmissionRecord.expires_at > database_now),
        )
        live_granted = await self._count(
            session,
            ToolEffectDagAdmissionRecord,
            (ToolEffectDagAdmissionRecord.status == "granted")
            & (ToolEffectDagAdmissionRecord.expires_at > database_now),
        )
        expired_leases = await self._count(
            session,
            ToolEffectDagAdmissionRecord,
            ToolEffectDagAdmissionRecord.status.in_(("pending", "granted"))
            & (ToolEffectDagAdmissionRecord.expires_at <= database_now),
        )
        state = await session.get(ToolEffectDagAdmissionStateRecord, "global")
        if state is None:
            raise RuntimeError("Effect DAG admission scheduler state is missing")
        shard_revision = int(
            (await session.scalar(select(func.sum(ToolEffectDagAdmissionShardRecord.revision))))
            or 0
        )
        shard_sequence = int(
            (
                await session.scalar(
                    select(func.max(ToolEffectDagAdmissionShardRecord.last_grant_sequence))
                )
            )
            or 0
        )
        return AdmissionOperationsMetrics(
            total=sum(counts.values()),
            pending=counts.get("pending", 0),
            granted=counts.get("granted", 0),
            released=counts.get("released", 0),
            cancelled=counts.get("cancelled", 0),
            withdrawn=counts.get("withdrawn", 0),
            expired=counts.get("expired", 0),
            live_pending=live_pending,
            live_granted=live_granted,
            expired_leases=expired_leases,
            scheduler_revision=state.revision + shard_revision,
            next_grant_sequence=max(
                state.next_grant_sequence,
                shard_sequence + 1,
            ),
            configuration_digest=state.configuration_digest,
            global_limit=state.global_limit,
            per_graph_limit=state.per_graph_limit,
            default_tool_limit=state.default_tool_limit,
        )

    async def _ready_metrics(
        self,
        session: AsyncSession,
        database_now: datetime,
    ) -> ReadyProjectionOperationsMetrics:
        projected_graphs = await self._count(session, ToolEffectDagReadyStateRecord)
        projected_nodes = await self._count(session, ToolEffectDagReadyNodeRecord)
        ready_nodes = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolEffectDagReadyNodeRecord)
                    .join(
                        ToolEffectNodeRecord,
                        ToolEffectNodeRecord.node_id == ToolEffectDagReadyNodeRecord.node_id,
                    )
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectDagReadyNodeRecord.graph_id,
                    )
                    .where(
                        ToolEffectGraphRecord.status == EffectGraphStatus.ACTIVE.value,
                        ToolEffectGraphRecord.cancel_requested_at.is_(None),
                        ToolEffectDagReadyNodeRecord.membership_ready.is_(True),
                    )
                )
            )
            or 0
        )
        live_graph = (
            ToolEffectGraphRecord.schema_version == EFFECT_DAG_SCHEMA_VERSION
        ) & ToolEffectGraphRecord.status.in_(_LIVE_GRAPH_STATUSES)
        missing_live = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolEffectGraphRecord)
                    .where(
                        live_graph,
                        ~exists(
                            select(1).where(
                                ToolEffectDagReadyStateRecord.graph_id
                                == ToolEffectGraphRecord.graph_id
                            )
                        ),
                    )
                )
            )
            or 0
        )
        event_drift = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolEffectGraphRecord)
                    .join(
                        ToolEffectDagReadyStateRecord,
                        ToolEffectDagReadyStateRecord.graph_id == ToolEffectGraphRecord.graph_id,
                    )
                    .where(
                        live_graph,
                        ToolEffectDagReadyStateRecord.event_seq
                        != ToolEffectGraphRecord.last_event_seq,
                    )
                )
            )
            or 0
        )
        graph_node_count = (
            select(func.count())
            .select_from(ToolEffectNodeRecord)
            .where(ToolEffectNodeRecord.graph_id == ToolEffectGraphRecord.graph_id)
            .correlate(ToolEffectGraphRecord)
            .scalar_subquery()
        )
        projection_node_count = (
            select(func.count())
            .select_from(ToolEffectDagReadyNodeRecord)
            .where(ToolEffectDagReadyNodeRecord.graph_id == ToolEffectGraphRecord.graph_id)
            .correlate(ToolEffectGraphRecord)
            .scalar_subquery()
        )
        projection_membership_count = (
            select(func.count())
            .select_from(ToolEffectDagReadyNodeRecord)
            .where(
                ToolEffectDagReadyNodeRecord.graph_id == ToolEffectGraphRecord.graph_id,
                ToolEffectDagReadyNodeRecord.membership_ready.is_(True),
            )
            .correlate(ToolEffectGraphRecord)
            .scalar_subquery()
        )
        database_membership_count = (
            select(func.count())
            .select_from(ToolEffectDagReadyNodeRecord)
            .join(
                ToolEffectNodeRecord,
                ToolEffectNodeRecord.node_id == ToolEffectDagReadyNodeRecord.node_id,
            )
            .where(
                ToolEffectDagReadyNodeRecord.graph_id == ToolEffectGraphRecord.graph_id,
                ToolEffectDagReadyNodeRecord.branch_rejected.is_(False),
                ToolEffectDagReadyNodeRecord.remaining_predecessors == 0,
                ToolEffectDagReadyNodeRecord.unresolved_branches == 0,
                ToolEffectNodeRecord.status.in_(("pending", "active")),
                or_(
                    ToolEffectNodeRecord.claim_owner_id.is_(None),
                    ToolEffectNodeRecord.claim_expires_at.is_(None),
                    ToolEffectNodeRecord.claim_expires_at <= database_now,
                ),
            )
            .correlate(ToolEffectGraphRecord)
            .scalar_subquery()
        )
        row_drift = int(
            (
                await session.scalar(
                    select(func.count())
                    .select_from(ToolEffectGraphRecord)
                    .join(
                        ToolEffectDagReadyStateRecord,
                        ToolEffectDagReadyStateRecord.graph_id == ToolEffectGraphRecord.graph_id,
                    )
                    .where(
                        live_graph,
                        or_(
                            graph_node_count != projection_node_count,
                            projection_node_count
                            != ToolEffectDagReadyStateRecord.projected_node_count,
                            projection_membership_count
                            != ToolEffectDagReadyStateRecord.ready_node_count,
                            database_membership_count
                            != ToolEffectDagReadyStateRecord.ready_node_count,
                        ),
                    )
                )
            )
            or 0
        )
        rebuilds = int(
            (await session.scalar(select(func.sum(ToolEffectDagReadyStateRecord.rebuild_count))))
            or 0
        )
        rebuilt_at = await session.scalar(
            select(func.max(ToolEffectDagReadyStateRecord.rebuilt_at))
        )
        return ReadyProjectionOperationsMetrics(
            projected_graphs=projected_graphs,
            projected_nodes=projected_nodes,
            ready_nodes=ready_nodes,
            missing_live_graphs=missing_live,
            event_drift_graphs=event_drift,
            row_count_drift_graphs=row_drift,
            rebuilds_observed=rebuilds,
            last_rebuilt_at=self._as_utc_or_none(rebuilt_at),
        )

    async def _outbox_metrics(
        self,
        session: AsyncSession,
        database_now: datetime,
    ) -> OutboxOperationsMetrics:
        total = await self._count(session, OutboxMessageRecord)
        published = await self._count(
            session,
            OutboxMessageRecord,
            OutboxMessageRecord.published_at.is_not(None),
        )
        dead_lettered = await self._count(
            session,
            OutboxMessageRecord,
            OutboxMessageRecord.dead_lettered_at.is_not(None),
        )
        unpublished = OutboxMessageRecord.published_at.is_(
            None
        ) & OutboxMessageRecord.dead_lettered_at.is_(None)
        in_flight_condition = (
            unpublished
            & OutboxMessageRecord.claim_owner_id.is_not(None)
            & (OutboxMessageRecord.claim_expires_at > database_now)
        )
        in_flight = await self._count(session, OutboxMessageRecord, in_flight_condition)
        pending_ready = await self._count(
            session,
            OutboxMessageRecord,
            unpublished & (OutboxMessageRecord.available_at <= database_now) & ~in_flight_condition,
        )
        retry_scheduled = await self._count(
            session,
            OutboxMessageRecord,
            unpublished & (OutboxMessageRecord.available_at > database_now),
        )
        oldest_pending = await session.scalar(
            select(func.min(OutboxMessageRecord.created_at)).where(unpublished)
        )
        oldest_dlq = await session.scalar(
            select(func.min(OutboxMessageRecord.dead_lettered_at)).where(
                OutboxMessageRecord.dead_lettered_at.is_not(None)
            )
        )
        return OutboxOperationsMetrics(
            total=total,
            pending_ready=pending_ready,
            retry_scheduled=retry_scheduled,
            in_flight=in_flight,
            published=published,
            dead_lettered=dead_lettered,
            inbox_receipts=await self._count(session, InboxDeliveryRecord),
            oldest_pending_at=self._as_utc_or_none(oldest_pending),
            oldest_dead_lettered_at=self._as_utc_or_none(oldest_dlq),
        )

    async def _graph_control_samples(
        self,
        session: AsyncSession,
        limit: int,
    ) -> tuple[GraphControlOperationsRead, ...]:
        records = tuple(
            (
                await session.scalars(
                    select(ToolEffectGraphControlRecord)
                    .order_by(
                        ToolEffectGraphControlRecord.updated_at.desc(),
                        ToolEffectGraphControlRecord.control_id,
                    )
                    .limit(limit)
                )
            ).all()
        )
        return tuple(
            GraphControlOperationsRead(
                control_id=record.control_id,
                task_id=record.task_id,
                graph_id=record.graph_id,
                command=record.command,
                request_digest=record.request_digest,
                status=record.status,
                revision=record.revision,
                attempt_count=record.attempt_count,
                target_owner_id=record.target_owner_id,
                target_fencing_token=record.target_fencing_token,
                claim_owner_id=record.claim_owner_id,
                claim_fencing_token=record.claim_fencing_token,
                claim_expires_at=self._as_utc_or_none(record.claim_expires_at),
                last_error_code=record.last_error_code,
                updated_at=self._as_utc(record.updated_at),
            )
            for record in records
        )

    async def _admission_samples(
        self,
        session: AsyncSession,
        limit: int,
    ) -> tuple[AdmissionOperationsRead, ...]:
        records = tuple(
            (
                await session.scalars(
                    select(ToolEffectDagAdmissionRecord)
                    .order_by(
                        ToolEffectDagAdmissionRecord.updated_at.desc(),
                        ToolEffectDagAdmissionRecord.admission_id,
                    )
                    .limit(limit)
                )
            ).all()
        )
        return tuple(
            AdmissionOperationsRead(
                admission_id=record.admission_id,
                batch_id=record.batch_id,
                graph_id=record.graph_id,
                node_id=record.node_id,
                tool_name=record.tool_name,
                owner_id=record.owner_id,
                status=record.status,
                revision=record.revision,
                fencing_token=record.fencing_token,
                grant_sequence=record.grant_sequence,
                expires_at=self._as_utc(record.expires_at),
                updated_at=self._as_utc(record.updated_at),
            )
            for record in records
        )

    async def _ready_samples(
        self,
        session: AsyncSession,
        limit: int,
    ) -> tuple[ReadyProjectionOperationsRead, ...]:
        rows = tuple(
            (
                await session.execute(
                    select(
                        ToolEffectDagReadyStateRecord,
                        ToolEffectGraphRecord.status,
                        ToolEffectGraphRecord.last_event_seq,
                        func.count(ToolEffectDagReadyNodeRecord.node_id),
                        func.sum(
                            func.cast(
                                (
                                    (ToolEffectDagReadyNodeRecord.branch_rejected.is_(False))
                                    & (ToolEffectDagReadyNodeRecord.remaining_predecessors == 0)
                                    & (ToolEffectDagReadyNodeRecord.unresolved_branches == 0)
                                ),
                                ToolEffectDagReadyNodeRecord.revision.type,
                            )
                        ),
                    )
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectDagReadyStateRecord.graph_id,
                    )
                    .outerjoin(
                        ToolEffectDagReadyNodeRecord,
                        ToolEffectDagReadyNodeRecord.graph_id
                        == ToolEffectDagReadyStateRecord.graph_id,
                    )
                    .group_by(
                        ToolEffectDagReadyStateRecord.graph_id,
                        ToolEffectGraphRecord.status,
                        ToolEffectGraphRecord.last_event_seq,
                    )
                    .order_by(
                        ToolEffectDagReadyStateRecord.updated_at.desc(),
                        ToolEffectDagReadyStateRecord.graph_id,
                    )
                    .limit(limit)
                )
            ).all()
        )
        return tuple(
            ReadyProjectionOperationsRead(
                graph_id=state.graph_id,
                graph_status=graph_status,
                graph_event_seq=graph_event_seq,
                projection_revision=state.revision,
                projection_event_seq=state.event_seq,
                content_digest=state.content_digest,
                rebuild_count=state.rebuild_count,
                last_rebuild_duration_ms=state.last_rebuild_duration_ms,
                projected_nodes=int(projected_nodes or 0),
                dependency_ready_nodes=int(dependency_ready_nodes or 0),
                rebuilt_at=self._as_utc(state.rebuilt_at),
                updated_at=self._as_utc(state.updated_at),
            )
            for (
                state,
                graph_status,
                graph_event_seq,
                projected_nodes,
                dependency_ready_nodes,
            ) in rows
        )

    async def _outbox_samples(
        self,
        session: AsyncSession,
        limit: int,
        database_now: datetime,
    ) -> tuple[OutboxOperationsRead, ...]:
        records = tuple(
            (
                await session.scalars(
                    select(OutboxMessageRecord)
                    .order_by(
                        OutboxMessageRecord.dead_lettered_at.desc().nulls_last(),
                        OutboxMessageRecord.created_at.desc(),
                        OutboxMessageRecord.message_id,
                    )
                    .limit(limit)
                )
            ).all()
        )
        samples: list[OutboxOperationsRead] = []
        for record in records:
            state: Literal["pending", "in_flight", "published", "dead_lettered"]
            if record.published_at is not None:
                state = "published"
            elif record.dead_lettered_at is not None:
                state = "dead_lettered"
            elif (
                record.claim_owner_id is not None
                and record.claim_expires_at is not None
                and self._as_utc(record.claim_expires_at) > self._as_utc(database_now)
            ):
                state = "in_flight"
            else:
                state = "pending"
            error_text = record.dead_letter_reason or record.last_error
            samples.append(
                OutboxOperationsRead(
                    message_id=record.message_id,
                    task_id=record.task_id,
                    event_id=record.event_id,
                    event_seq=record.event_seq,
                    topic=record.topic,
                    state=state,
                    payload_digest=sha256_digest(record.payload),
                    attempt_count=record.attempt_count,
                    claim_owner_id=record.claim_owner_id,
                    claim_fencing_token=record.claim_fencing_token,
                    available_at=self._as_utc(record.available_at),
                    claim_expires_at=self._as_utc_or_none(record.claim_expires_at),
                    published_at=self._as_utc_or_none(record.published_at),
                    dead_lettered_at=self._as_utc_or_none(record.dead_lettered_at),
                    error_digest=(
                        sha256_digest({"error": error_text}) if error_text is not None else None
                    ),
                    created_at=self._as_utc(record.created_at),
                )
            )
        return tuple(samples)

    def _alerts(
        self,
        *,
        database_now: datetime,
        graph_controls: GraphControlOperationsMetrics,
        admissions: AdmissionOperationsMetrics,
        ready_projection: ReadyProjectionOperationsMetrics,
        outbox: OutboxOperationsMetrics,
    ) -> tuple[OperationsAlert, ...]:
        alerts: list[OperationsAlert] = []
        control_age = self._age_seconds(database_now, graph_controls.oldest_actionable_at)
        if graph_controls.claim_expired:
            alerts.append(
                OperationsAlert(
                    code="GRAPH_CONTROL_CLAIM_EXPIRED",
                    severity=OperationsAlertSeverity.CRITICAL,
                    domain="graph_control",
                    count=graph_controls.claim_expired,
                )
            )
        if graph_controls.actionable and control_age >= self._stalled_after_seconds:
            alerts.append(
                OperationsAlert(
                    code="GRAPH_CONTROL_STALLED",
                    severity=OperationsAlertSeverity.WARNING,
                    domain="graph_control",
                    count=graph_controls.actionable,
                )
            )
        if admissions.expired_leases:
            alerts.append(
                OperationsAlert(
                    code="ADMISSION_LEASE_EXPIRED",
                    severity=OperationsAlertSeverity.WARNING,
                    domain="cluster_admission",
                    count=admissions.expired_leases,
                )
            )
        projection_drift = (
            ready_projection.missing_live_graphs
            + ready_projection.event_drift_graphs
            + ready_projection.row_count_drift_graphs
        )
        if projection_drift:
            alerts.append(
                OperationsAlert(
                    code="READY_PROJECTION_REPAIR_REQUIRED",
                    severity=OperationsAlertSeverity.CRITICAL,
                    domain="ready_projection",
                    count=projection_drift,
                )
            )
        if outbox.dead_lettered:
            alerts.append(
                OperationsAlert(
                    code="OUTBOX_DEAD_LETTER_PRESENT",
                    severity=OperationsAlertSeverity.CRITICAL,
                    domain="outbox",
                    count=outbox.dead_lettered,
                )
            )
        outbox_age = self._age_seconds(database_now, outbox.oldest_pending_at)
        pending = outbox.pending_ready + outbox.retry_scheduled + outbox.in_flight
        if pending and outbox_age >= self._stalled_after_seconds:
            alerts.append(
                OperationsAlert(
                    code="OUTBOX_DELIVERY_STALLED",
                    severity=OperationsAlertSeverity.WARNING,
                    domain="outbox",
                    count=pending,
                )
            )
        return tuple(alerts)

    async def _apply_retention(
        self,
        session: AsyncSession,
        *,
        cutoff: datetime,
    ) -> tuple[RetentionCounts, str]:
        limit = self._retention_batch_size
        manifest: dict[str, list[dict[str, object]]] = {}

        controls = tuple(
            (
                await session.scalars(
                    select(ToolEffectGraphControlRecord)
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectGraphControlRecord.graph_id,
                    )
                    .where(
                        ToolEffectGraphControlRecord.status.in_(("applied", "superseded")),
                        ToolEffectGraphControlRecord.updated_at < cutoff,
                        ToolEffectGraphRecord.status.in_(_TERMINAL_RETENTION_GRAPH_STATUSES),
                    )
                    .order_by(ToolEffectGraphControlRecord.updated_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["graph_controls"] = [
            {
                "control_id": row.control_id,
                "request_digest": row.request_digest,
                "status": row.status,
                "revision": row.revision,
                "claim_fencing_token": row.claim_fencing_token,
            }
            for row in controls
        ]
        if controls:
            await session.execute(
                delete(ToolEffectGraphControlRecord).where(
                    ToolEffectGraphControlRecord.control_id.in_(
                        tuple(row.control_id for row in controls)
                    )
                )
            )

        admissions = tuple(
            (
                await session.scalars(
                    select(ToolEffectDagAdmissionRecord)
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectDagAdmissionRecord.graph_id,
                    )
                    .where(
                        ToolEffectDagAdmissionRecord.status.in_(_TERMINAL_ADMISSION_STATUSES),
                        ToolEffectDagAdmissionRecord.updated_at < cutoff,
                        ToolEffectGraphRecord.status.in_(_TERMINAL_RETENTION_GRAPH_STATUSES),
                    )
                    .order_by(ToolEffectDagAdmissionRecord.updated_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["admissions"] = [
            {
                "admission_id": row.admission_id,
                "status": row.status,
                "revision": row.revision,
                "fencing_token": row.fencing_token,
                "grant_sequence": row.grant_sequence,
            }
            for row in admissions
        ]
        if admissions:
            await session.execute(
                delete(ToolEffectDagAdmissionRecord).where(
                    ToolEffectDagAdmissionRecord.admission_id.in_(
                        tuple(row.admission_id for row in admissions)
                    )
                )
            )

        checkpoints = tuple(
            (
                await session.scalars(
                    select(ToolEffectReadySetCheckpointRecord)
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id
                        == ToolEffectReadySetCheckpointRecord.graph_id,
                    )
                    .where(
                        ToolEffectReadySetCheckpointRecord.created_at < cutoff,
                        ToolEffectGraphRecord.status.in_(_TERMINAL_RETENTION_GRAPH_STATUSES),
                    )
                    .order_by(ToolEffectReadySetCheckpointRecord.created_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["ready_checkpoints"] = [
            {
                "checkpoint_id": row.checkpoint_id,
                "graph_id": row.graph_id,
                "proof_digest": row.proof_digest,
                "event_seq": row.event_seq,
            }
            for row in checkpoints
        ]
        if checkpoints:
            await session.execute(
                delete(ToolEffectReadySetCheckpointRecord).where(
                    ToolEffectReadySetCheckpointRecord.checkpoint_id.in_(
                        tuple(row.checkpoint_id for row in checkpoints)
                    )
                )
            )

        ready_nodes = tuple(
            (
                await session.scalars(
                    select(ToolEffectDagReadyNodeRecord)
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectDagReadyNodeRecord.graph_id,
                    )
                    .where(
                        ToolEffectGraphRecord.updated_at < cutoff,
                        ToolEffectGraphRecord.status.in_(_TERMINAL_RETENTION_GRAPH_STATUSES),
                    )
                    .order_by(ToolEffectDagReadyNodeRecord.updated_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["ready_nodes"] = [
            {
                "node_id": row.node_id,
                "graph_id": row.graph_id,
                "revision": row.revision,
                "proof_digest": row.proof_digest,
            }
            for row in ready_nodes
        ]
        if ready_nodes:
            await session.execute(
                delete(ToolEffectDagReadyNodeRecord).where(
                    ToolEffectDagReadyNodeRecord.node_id.in_(
                        tuple(row.node_id for row in ready_nodes)
                    )
                )
            )
            await session.flush()

        ready_states = tuple(
            (
                await session.scalars(
                    select(ToolEffectDagReadyStateRecord)
                    .join(
                        ToolEffectGraphRecord,
                        ToolEffectGraphRecord.graph_id == ToolEffectDagReadyStateRecord.graph_id,
                    )
                    .where(
                        ToolEffectGraphRecord.updated_at < cutoff,
                        ToolEffectGraphRecord.status.in_(_TERMINAL_RETENTION_GRAPH_STATUSES),
                        ~exists(
                            select(1).where(
                                ToolEffectDagReadyNodeRecord.graph_id
                                == ToolEffectDagReadyStateRecord.graph_id
                            )
                        ),
                    )
                    .order_by(ToolEffectDagReadyStateRecord.updated_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["ready_states"] = [
            {
                "graph_id": row.graph_id,
                "revision": row.revision,
                "event_seq": row.event_seq,
                "content_digest": row.content_digest,
                "rebuild_count": row.rebuild_count,
            }
            for row in ready_states
        ]
        if ready_states:
            await session.execute(
                delete(ToolEffectDagReadyStateRecord).where(
                    ToolEffectDagReadyStateRecord.graph_id.in_(
                        tuple(row.graph_id for row in ready_states)
                    )
                )
            )

        outbox = tuple(
            (
                await session.scalars(
                    select(OutboxMessageRecord)
                    .where(
                        OutboxMessageRecord.published_at.is_not(None),
                        OutboxMessageRecord.published_at < cutoff,
                    )
                    .order_by(OutboxMessageRecord.published_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["published_outbox"] = [
            {
                "message_id": row.message_id,
                "event_id": row.event_id,
                "event_seq": row.event_seq,
                "payload_digest": sha256_digest(row.payload),
                "attempt_count": row.attempt_count,
                "claim_fencing_token": row.claim_fencing_token,
                "published_at": self._iso(row.published_at),
            }
            for row in outbox
        ]
        if outbox:
            await session.execute(
                delete(OutboxMessageRecord).where(
                    OutboxMessageRecord.message_id.in_(tuple(row.message_id for row in outbox))
                )
            )

        inbox = tuple(
            (
                await session.scalars(
                    select(InboxDeliveryRecord)
                    .where(InboxDeliveryRecord.processed_at < cutoff)
                    .order_by(InboxDeliveryRecord.processed_at)
                    .limit(limit)
                )
            ).all()
        )
        manifest["inbox_receipts"] = [
            {
                "inbox_id": row.inbox_id,
                "consumer_name": row.consumer_name,
                "message_id": row.message_id,
                "delivery_id": row.delivery_id,
                "payload_digest": row.payload_digest,
            }
            for row in inbox
        ]
        if inbox:
            await session.execute(
                delete(InboxDeliveryRecord).where(
                    InboxDeliveryRecord.inbox_id.in_(tuple(row.inbox_id for row in inbox))
                )
            )
        counts = RetentionCounts(
            graph_controls=len(controls),
            admissions=len(admissions),
            ready_checkpoints=len(checkpoints),
            ready_nodes=len(ready_nodes),
            ready_states=len(ready_states),
            published_outbox=len(outbox),
            inbox_receipts=len(inbox),
        )
        return counts, sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-retention-manifest.v1",
                "cutoff": cutoff.isoformat(),
                "records": manifest,
            }
        )

    async def _reconcile_alerts(
        self,
        session: AsyncSession,
        *,
        snapshot: EffectRuntimeOperationsSnapshot,
        audit_event: EffectRuntimeAuditEventRead,
    ) -> tuple[OperationsAlertNotificationRead, ...]:
        records = tuple(
            (
                await session.scalars(
                    select(EffectRuntimeAlertStateRecord)
                    .order_by(EffectRuntimeAlertStateRecord.alert_code)
                    .with_for_update()
                )
            ).all()
        )
        states = {record.alert_code: record for record in records}
        current = {alert.code: alert for alert in snapshot.alerts}
        occurred_at = audit_event.occurred_at
        notifications: list[OperationsAlertNotificationRead] = []
        for alert_code in sorted(current):
            alert = current[alert_code]
            state = states.get(alert_code)
            transition: OperationsAlertTransition | None = None
            if state is None:
                state = EffectRuntimeAlertStateRecord(
                    alert_code=alert.code,
                    domain=alert.domain,
                    severity=alert.severity.value,
                    active=True,
                    count=alert.count,
                    revision=1,
                    first_seen_at=occurred_at,
                    last_seen_at=occurred_at,
                    resolved_at=None,
                    last_snapshot_digest=snapshot.snapshot_digest,
                    updated_at=occurred_at,
                )
                session.add(state)
                states[alert_code] = state
                transition = OperationsAlertTransition.OPENED
            elif not state.active:
                state.active = True
                state.count = alert.count
                state.domain = alert.domain
                state.severity = alert.severity.value
                state.revision += 1
                state.last_seen_at = occurred_at
                state.resolved_at = None
                state.last_snapshot_digest = snapshot.snapshot_digest
                state.updated_at = occurred_at
                transition = OperationsAlertTransition.OPENED
            else:
                changed = (
                    state.count != alert.count
                    or state.domain != alert.domain
                    or state.severity != alert.severity.value
                )
                state.count = alert.count
                state.domain = alert.domain
                state.severity = alert.severity.value
                state.last_seen_at = occurred_at
                state.last_snapshot_digest = snapshot.snapshot_digest
                state.updated_at = occurred_at
                if changed:
                    state.revision += 1
                    transition = OperationsAlertTransition.UPDATED
            if transition is not None:
                notifications.append(
                    await self._append_alert_notification(
                        session,
                        state=state,
                        transition=transition,
                        snapshot_digest=snapshot.snapshot_digest,
                        audit_event=audit_event,
                    )
                )
        for alert_code in sorted(set(states) - set(current)):
            state = states[alert_code]
            if not state.active:
                continue
            state.active = False
            state.count = 0
            state.revision += 1
            state.resolved_at = occurred_at
            state.last_snapshot_digest = snapshot.snapshot_digest
            state.updated_at = occurred_at
            notifications.append(
                await self._append_alert_notification(
                    session,
                    state=state,
                    transition=OperationsAlertTransition.RESOLVED,
                    snapshot_digest=snapshot.snapshot_digest,
                    audit_event=audit_event,
                )
            )
        await session.flush()
        return tuple(notifications)

    async def _append_alert_notification(
        self,
        session: AsyncSession,
        *,
        state: EffectRuntimeAlertStateRecord,
        transition: OperationsAlertTransition,
        snapshot_digest: str,
        audit_event: EffectRuntimeAuditEventRead,
    ) -> OperationsAlertNotificationRead:
        operations_state = await self._lock_audit_state(session)
        if operations_state.next_alert_sequence == 1:
            if operations_state.last_alert_event_digest is not None:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        else:
            previous = await session.scalar(
                select(EffectRuntimeAlertNotificationRecord).where(
                    EffectRuntimeAlertNotificationRecord.sequence
                    == operations_state.next_alert_sequence - 1
                )
            )
            if (
                previous is None
                or previous.event_digest != operations_state.last_alert_event_digest
            ):
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            self._validate_alert_notification_record(
                previous,
                expected_sequence=operations_state.next_alert_sequence - 1,
                expected_previous=previous.previous_event_digest,
            )
        sequence = operations_state.next_alert_sequence
        previous_digest = operations_state.last_alert_event_digest
        material: dict[str, object] = {
            "schema_version": "deskpilot.effect-runtime-alert-notification.v1",
            "sequence": sequence,
            "alert_code": state.alert_code,
            "transition": transition.value,
            "severity": state.severity,
            "domain": state.domain,
            "count": state.count,
            "alert_revision": state.revision,
            "snapshot_digest": snapshot_digest,
            "audit_event_id": audit_event.event_id,
            "audit_sequence": audit_event.sequence,
            "previous_event_digest": previous_digest,
            "occurred_at": audit_event.occurred_at.isoformat(),
        }
        record = EffectRuntimeAlertNotificationRecord(
            notification_id=f"opn_{uuid4().hex}",
            sequence=sequence,
            alert_code=state.alert_code,
            transition=transition.value,
            severity=state.severity,
            domain=state.domain,
            count=state.count,
            alert_revision=state.revision,
            snapshot_digest=snapshot_digest,
            audit_event_id=audit_event.event_id,
            audit_sequence=audit_event.sequence,
            previous_event_digest=previous_digest,
            event_digest=sha256_digest(material),
            occurred_at=audit_event.occurred_at,
        )
        session.add(record)
        operations_state.revision += 1
        operations_state.next_alert_sequence += 1
        operations_state.last_alert_event_digest = record.event_digest
        operations_state.updated_at = audit_event.occurred_at
        await session.flush()
        return self._to_alert_notification(record)

    async def _append_audit(
        self,
        session: AsyncSession,
        *,
        action: str,
        actor_id: str,
        request_digest: str,
        result_digest: str,
        details: dict[str, object],
        idempotency_key_digest: str | None = None,
        retention_at: datetime | None = None,
    ) -> EffectRuntimeAuditEventRead:
        state = await self._lock_audit_state(session)
        if state.next_sequence == 1:
            if state.last_event_digest is not None:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        else:
            previous = await session.scalar(
                select(EffectRuntimeOperationsAuditRecord).where(
                    EffectRuntimeOperationsAuditRecord.sequence == state.next_sequence - 1
                )
            )
            if previous is None or previous.event_digest != state.last_event_digest:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            self._validate_audit_record(
                previous,
                expected_sequence=state.next_sequence - 1,
                expected_previous=previous.previous_event_digest,
            )
        occurred_at = await database_utc_now(session)
        sequence = state.next_sequence
        previous_digest = state.last_event_digest
        event_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-audit-event.v1",
                "sequence": sequence,
                "action": action,
                "actor_id": actor_id,
                "request_digest": request_digest,
                "result_digest": result_digest,
                "previous_event_digest": previous_digest,
                "details": details,
                "occurred_at": occurred_at.isoformat(),
            }
        )
        record = EffectRuntimeOperationsAuditRecord(
            event_id=f"opa_{uuid4().hex}",
            sequence=sequence,
            action=action,
            actor_id=actor_id,
            idempotency_key_digest=idempotency_key_digest,
            request_digest=request_digest,
            result_digest=result_digest,
            previous_event_digest=previous_digest,
            event_digest=event_digest,
            details=details,
            occurred_at=occurred_at,
        )
        session.add(record)
        state.revision += 1
        state.next_sequence += 1
        state.last_event_digest = event_digest
        state.updated_at = occurred_at
        if retention_at is not None:
            state.last_retention_at = retention_at
        await session.flush()
        return self._to_audit_event(record)

    @staticmethod
    async def _lock_audit_state(
        session: AsyncSession,
    ) -> EffectRuntimeOperationsStateRecord:
        """Serialize idempotency lookup, mutation, and audit-head advancement."""
        state = await session.get(
            EffectRuntimeOperationsStateRecord,
            _OPERATIONS_SCOPE,
            with_for_update=True,
            populate_existing=True,
        )
        if state is None:
            raise RuntimeError("Effect-runtime operations state is missing")
        return state

    async def _idempotent_event(
        self,
        session: AsyncSession,
        *,
        action: str,
        idempotency_key_digest: str,
        request_digest: str,
    ) -> EffectRuntimeAuditEventRead | None:
        record = await session.scalar(
            select(EffectRuntimeOperationsAuditRecord).where(
                EffectRuntimeOperationsAuditRecord.action == action,
                EffectRuntimeOperationsAuditRecord.idempotency_key_digest == idempotency_key_digest,
            )
        )
        if record is None:
            return None
        if record.request_digest != request_digest:
            raise EffectRuntimeOperationsIdempotencyConflictError(action)
        self._validate_audit_record(
            record,
            expected_sequence=record.sequence,
            expected_previous=record.previous_event_digest,
        )
        return self._to_audit_event(record)

    async def _validate_audit_page(
        self,
        session: AsyncSession,
        *,
        records: tuple[EffectRuntimeOperationsAuditRecord, ...],
        after_sequence: int,
        limit: int,
        through_sequence: int,
        through_digest: str | None,
    ) -> None:
        if through_sequence == 0:
            if through_digest is not None or after_sequence != 0 or records:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            return
        if through_digest is None or after_sequence > through_sequence:
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        page = records[:limit]
        expected_previous: str | None = None
        if after_sequence:
            preceding = await session.scalar(
                select(EffectRuntimeOperationsAuditRecord).where(
                    EffectRuntimeOperationsAuditRecord.sequence == after_sequence
                )
            )
            if preceding is None:
                raise EffectRuntimeOperationsAuditRejectedError(str(after_sequence))
            self._validate_audit_record(
                preceding,
                expected_sequence=after_sequence,
                expected_previous=preceding.previous_event_digest,
            )
            expected_previous = preceding.event_digest
        expected_sequence = after_sequence + 1
        for record in page:
            self._validate_audit_record(
                record,
                expected_sequence=expected_sequence,
                expected_previous=expected_previous,
            )
            expected_sequence += 1
            expected_previous = record.event_digest
        through = await session.scalar(
            select(EffectRuntimeOperationsAuditRecord).where(
                EffectRuntimeOperationsAuditRecord.sequence == through_sequence
            )
        )
        if through is None or through.event_digest != through_digest:
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        self._validate_audit_record(
            through,
            expected_sequence=through_sequence,
            expected_previous=through.previous_event_digest,
        )
        if len(records) <= limit:
            tail_sequence = page[-1].sequence if page else after_sequence
            tail_digest = page[-1].event_digest if page else expected_previous
            if tail_sequence != through_sequence or tail_digest != through_digest:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)

    async def _validate_alert_notification_page(
        self,
        session: AsyncSession,
        *,
        records: tuple[EffectRuntimeAlertNotificationRecord, ...],
        after_sequence: int,
        limit: int,
        through_sequence: int,
        through_digest: str | None,
    ) -> None:
        if through_sequence == 0:
            if through_digest is not None or after_sequence != 0 or records:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            return
        if through_digest is None or after_sequence > through_sequence:
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        expected_previous: str | None = None
        linked_records: list[EffectRuntimeAlertNotificationRecord] = []
        if after_sequence:
            preceding = await session.scalar(
                select(EffectRuntimeAlertNotificationRecord).where(
                    EffectRuntimeAlertNotificationRecord.sequence == after_sequence
                )
            )
            if preceding is None:
                raise EffectRuntimeOperationsAuditRejectedError(str(after_sequence))
            self._validate_alert_notification_record(
                preceding,
                expected_sequence=after_sequence,
                expected_previous=preceding.previous_event_digest,
            )
            expected_previous = preceding.event_digest
            linked_records.append(preceding)
        expected_sequence = after_sequence + 1
        page = records[:limit]
        for record in page:
            self._validate_alert_notification_record(
                record,
                expected_sequence=expected_sequence,
                expected_previous=expected_previous,
            )
            expected_sequence += 1
            expected_previous = record.event_digest
        through = await session.scalar(
            select(EffectRuntimeAlertNotificationRecord).where(
                EffectRuntimeAlertNotificationRecord.sequence == through_sequence
            )
        )
        if through is None or through.event_digest != through_digest:
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        self._validate_alert_notification_record(
            through,
            expected_sequence=through_sequence,
            expected_previous=through.previous_event_digest,
        )
        linked_records.extend(page)
        linked_records.append(through)
        await self._validate_alert_notification_audit_links(
            session,
            records=tuple(linked_records),
        )
        if len(records) <= limit:
            tail_sequence = page[-1].sequence if page else after_sequence
            tail_digest = page[-1].event_digest if page else expected_previous
            if tail_sequence != through_sequence or tail_digest != through_digest:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)

    async def _validate_audit_export_page(
        self,
        session: AsyncSession,
        *,
        records: tuple[EffectRuntimeOperationsAuditRecord, ...],
        after_sequence: int,
        after_digest: str | None,
        through_sequence: int,
        through_digest: str | None,
        limit: int,
    ) -> None:
        if through_sequence == 0:
            if (
                through_digest is not None
                or after_sequence != 0
                or after_digest is not None
                or records
            ):
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
            return
        if (
            through_digest is None
            or after_sequence < 0
            or after_sequence > through_sequence
            or (after_sequence == 0) != (after_digest is None)
        ):
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        expected_previous: str | None = None
        if after_sequence:
            preceding = await session.scalar(
                select(EffectRuntimeOperationsAuditRecord).where(
                    EffectRuntimeOperationsAuditRecord.sequence == after_sequence
                )
            )
            if preceding is None or preceding.event_digest != after_digest:
                raise EffectRuntimeOperationsAuditRejectedError(str(after_sequence))
            self._validate_audit_record(
                preceding,
                expected_sequence=after_sequence,
                expected_previous=preceding.previous_event_digest,
            )
            expected_previous = preceding.event_digest
        expected_sequence = after_sequence + 1
        page = records[:limit]
        for record in page:
            self._validate_audit_record(
                record,
                expected_sequence=expected_sequence,
                expected_previous=expected_previous,
            )
            expected_sequence += 1
            expected_previous = record.event_digest
        through = await session.scalar(
            select(EffectRuntimeOperationsAuditRecord).where(
                EffectRuntimeOperationsAuditRecord.sequence == through_sequence
            )
        )
        if through is None or through.event_digest != through_digest:
            raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)
        self._validate_audit_record(
            through,
            expected_sequence=through_sequence,
            expected_previous=through.previous_event_digest,
        )
        if len(records) <= limit:
            tail_sequence = page[-1].sequence if page else after_sequence
            tail_digest = page[-1].event_digest if page else expected_previous
            if tail_sequence != through_sequence or tail_digest != through_digest:
                raise EffectRuntimeOperationsAuditRejectedError(_OPERATIONS_SCOPE)

    @classmethod
    def _validate_audit_record(
        cls,
        record: EffectRuntimeOperationsAuditRecord,
        *,
        expected_sequence: int,
        expected_previous: str | None,
    ) -> None:
        expected_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-audit-event.v1",
                "sequence": record.sequence,
                "action": record.action,
                "actor_id": record.actor_id,
                "request_digest": record.request_digest,
                "result_digest": record.result_digest,
                "previous_event_digest": record.previous_event_digest,
                "details": record.details,
                "occurred_at": cls._as_utc(record.occurred_at).isoformat(),
            }
        )
        if (
            record.sequence != expected_sequence
            or record.previous_event_digest != expected_previous
            or record.event_digest != expected_digest
        ):
            raise EffectRuntimeOperationsAuditRejectedError(record.event_id)

    @classmethod
    def _validate_alert_notification_record(
        cls,
        record: EffectRuntimeAlertNotificationRecord,
        *,
        expected_sequence: int,
        expected_previous: str | None,
    ) -> None:
        expected_digest = sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-alert-notification.v1",
                "sequence": record.sequence,
                "alert_code": record.alert_code,
                "transition": record.transition,
                "severity": record.severity,
                "domain": record.domain,
                "count": record.count,
                "alert_revision": record.alert_revision,
                "snapshot_digest": record.snapshot_digest,
                "audit_event_id": record.audit_event_id,
                "audit_sequence": record.audit_sequence,
                "previous_event_digest": record.previous_event_digest,
                "occurred_at": cls._as_utc(record.occurred_at).isoformat(),
            }
        )
        if (
            record.sequence != expected_sequence
            or record.previous_event_digest != expected_previous
            or record.event_digest != expected_digest
        ):
            raise EffectRuntimeOperationsAuditRejectedError(record.notification_id)

    async def _validate_alert_notification_audit_links(
        self,
        session: AsyncSession,
        *,
        records: tuple[EffectRuntimeAlertNotificationRecord, ...],
    ) -> None:
        unique_records = {record.notification_id: record for record in records}
        audit_ids = {record.audit_event_id for record in unique_records.values()}
        audits = {
            record.event_id: record
            for record in (
                await session.scalars(
                    select(EffectRuntimeOperationsAuditRecord).where(
                        EffectRuntimeOperationsAuditRecord.event_id.in_(audit_ids)
                    )
                )
            ).all()
        }
        for record in unique_records.values():
            audit = audits.get(record.audit_event_id)
            if (
                audit is None
                or audit.action != "metrics.sampled"
                or audit.sequence != record.audit_sequence
                or audit.result_digest != record.snapshot_digest
            ):
                raise EffectRuntimeOperationsAuditRejectedError(record.notification_id)
            self._validate_audit_record(
                audit,
                expected_sequence=audit.sequence,
                expected_previous=audit.previous_event_digest,
            )

    @classmethod
    def _audit_export_id(
        cls,
        *,
        database_time: datetime,
        through_sequence: int,
        through_digest: str | None,
    ) -> str:
        return "opx_" + sha256_digest(
            {
                "schema_version": _AUDIT_EXPORT_SCHEMA_VERSION,
                "database_time": cls._as_utc(database_time).isoformat(),
                "through_sequence": through_sequence,
                "through_event_digest": through_digest,
            }
        )

    @classmethod
    def _encode_audit_export_cursor(
        cls,
        *,
        export_id: str,
        database_time: datetime,
        through_sequence: int,
        through_digest: str | None,
        after_sequence: int,
        after_digest: str,
    ) -> str:
        material: dict[str, object] = {
            "schema_version": _AUDIT_EXPORT_CURSOR_SCHEMA_VERSION,
            "export_id": export_id,
            "database_time": cls._as_utc(database_time).isoformat(),
            "through_sequence": through_sequence,
            "through_event_digest": through_digest,
            "after_sequence": after_sequence,
            "after_event_digest": after_digest,
        }
        payload = {**material, "cursor_digest": sha256_digest(material)}
        return base64.urlsafe_b64encode(canonical_json_bytes(payload)).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_audit_export_cursor(cursor: str) -> dict[str, object]:
        try:
            if not 1 <= len(cursor) <= 2_048:
                raise ValueError("cursor length")
            padding = "=" * (-len(cursor) % 4)
            raw = base64.b64decode(
                cursor + padding,
                altchars=b"-_",
                validate=True,
            )
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("cursor object")
            expected_keys = {
                "schema_version",
                "export_id",
                "database_time",
                "through_sequence",
                "through_event_digest",
                "after_sequence",
                "after_event_digest",
                "cursor_digest",
            }
            if set(payload) != expected_keys:
                raise ValueError("cursor fields")
            cursor_digest = payload.pop("cursor_digest")
            if not isinstance(cursor_digest, str) or cursor_digest != sha256_digest(payload):
                raise ValueError("cursor digest")
            if payload["schema_version"] != _AUDIT_EXPORT_CURSOR_SCHEMA_VERSION:
                raise ValueError("cursor version")
            export_id = payload["export_id"]
            database_time = payload["database_time"]
            through_sequence = payload["through_sequence"]
            through_digest = payload["through_event_digest"]
            after_sequence = payload["after_sequence"]
            after_digest = payload["after_event_digest"]
            if (
                not isinstance(export_id, str)
                or not export_id.startswith("opx_")
                or len(export_id) != 68
                or not isinstance(database_time, str)
                or isinstance(through_sequence, bool)
                or not isinstance(through_sequence, int)
                or through_sequence < 0
                or isinstance(after_sequence, bool)
                or not isinstance(after_sequence, int)
                or after_sequence < 0
                or after_sequence > through_sequence
                or (through_sequence == 0) != (through_digest is None)
                or (after_sequence == 0) != (after_digest is None)
                or (
                    through_digest is not None
                    and (not isinstance(through_digest, str) or len(through_digest) != 64)
                )
                or (
                    after_digest is not None
                    and (not isinstance(after_digest, str) or len(after_digest) != 64)
                )
            ):
                raise ValueError("cursor values")
            datetime.fromisoformat(database_time)
        except (
            ValueError,
            TypeError,
            UnicodeDecodeError,
            binascii.Error,
            json.JSONDecodeError,
        ) as exc:
            raise EffectRuntimeOperationsAuditRejectedError("cursor") from exc
        return cast(dict[str, object], payload)

    @staticmethod
    def _retention_from_event(event: EffectRuntimeAuditEventRead) -> RetentionRunResult:
        return RetentionRunResult(
            cutoff=datetime.fromisoformat(cast(str, event.details["cutoff"])),
            counts=RetentionCounts.model_validate(event.details["counts"]),
            manifest_digest=cast(str, event.details["manifest_digest"]),
            audit_event=event,
        )

    @staticmethod
    def _requeue_from_event(event: EffectRuntimeAuditEventRead) -> OutboxRequeueResult:
        return OutboxRequeueResult(
            message_id=cast(str, event.details["message_id"]),
            attempt_count=cast(int, event.details["attempt_count"]),
            claim_fencing_token=cast(int, event.details["claim_fencing_token"]),
            available_at=datetime.fromisoformat(cast(str, event.details["available_at"])),
            audit_event=event,
        )

    async def _run_scheduler(self) -> None:
        loop = asyncio.get_running_loop()
        next_metrics = loop.time() + self._metrics_interval
        next_retention = loop.time() + self._retention_interval
        while not self._stopping:
            now = loop.time()
            try:
                if now >= next_metrics:
                    await self.sample_metrics(actor_id="scheduler", sample_limit=10)
                    next_metrics = loop.time() + self._metrics_interval
                if now >= next_retention:
                    bucket = int(loop.time() // self._retention_interval)
                    await self.run_retention(
                        actor_id="scheduler",
                        idempotency_key=f"scheduler-retention-{bucket:016d}",
                    )
                    next_retention = loop.time() + self._retention_interval
            except Exception:
                logger.exception("Effect-runtime operations scheduler iteration failed")
                next_metrics = loop.time() + self._metrics_interval
                next_retention = loop.time() + self._retention_interval
            timeout = max(0.1, min(next_metrics, next_retention) - loop.time())
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=timeout)
                self._wake.clear()
            except TimeoutError:
                pass

    @staticmethod
    async def _status_counts(session: AsyncSession, column: Any) -> dict[str, int]:
        rows = await session.execute(select(column, func.count()).group_by(column))
        return {str(status): int(count) for status, count in rows}

    @staticmethod
    async def _count(
        session: AsyncSession,
        model: type[Any],
        *conditions: Any,
    ) -> int:
        statement = select(func.count()).select_from(model)
        if conditions:
            statement = statement.where(*conditions)
        return int((await session.scalar(statement)) or 0)

    @staticmethod
    def _to_audit_event(
        record: EffectRuntimeOperationsAuditRecord,
    ) -> EffectRuntimeAuditEventRead:
        return EffectRuntimeAuditEventRead(
            event_id=record.event_id,
            sequence=record.sequence,
            action=record.action,
            actor_id=record.actor_id,
            request_digest=record.request_digest,
            result_digest=record.result_digest,
            previous_event_digest=record.previous_event_digest,
            event_digest=record.event_digest,
            details=dict(record.details),
            occurred_at=EffectRuntimeOperationsService._as_utc(record.occurred_at),
        )

    @staticmethod
    def _to_alert_notification(
        record: EffectRuntimeAlertNotificationRecord,
    ) -> OperationsAlertNotificationRead:
        return OperationsAlertNotificationRead(
            notification_id=record.notification_id,
            sequence=record.sequence,
            alert_code=record.alert_code,
            transition=OperationsAlertTransition(record.transition),
            severity=OperationsAlertSeverity(record.severity),
            domain=record.domain,
            count=record.count,
            alert_revision=record.alert_revision,
            snapshot_digest=record.snapshot_digest,
            audit_event_id=record.audit_event_id,
            audit_sequence=record.audit_sequence,
            previous_event_digest=record.previous_event_digest,
            event_digest=record.event_digest,
            occurred_at=EffectRuntimeOperationsService._as_utc(record.occurred_at),
        )

    @staticmethod
    def _validate_actor(actor_id: str) -> None:
        if not 1 <= len(actor_id) <= 80:
            raise ValueError("Effect-runtime operations actor ID is invalid")

    @staticmethod
    def _validate_limit(limit: int) -> None:
        if not 1 <= limit <= 200:
            raise ValueError("Effect-runtime sample limit is invalid")

    @staticmethod
    def _idempotency_digest(value: str) -> str:
        if not 16 <= len(value) <= 128:
            raise ValueError("Effect-runtime idempotency key is invalid")
        return sha256_digest(
            {
                "schema_version": "deskpilot.effect-runtime-idempotency-key.v1",
                "key": value,
            }
        )

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)

    @classmethod
    def _as_utc_or_none(cls, value: datetime | None) -> datetime | None:
        return cls._as_utc(value) if value is not None else None

    @classmethod
    def _iso(cls, value: datetime | None) -> str | None:
        normalized = cls._as_utc_or_none(value)
        return normalized.isoformat() if normalized is not None else None

    @classmethod
    def _age_seconds(cls, now: datetime, value: datetime | None) -> float:
        if value is None:
            return 0
        return max(0, (cls._as_utc(now) - cls._as_utc(value)).total_seconds())


__all__ = [
    "EffectRuntimeOperationsAuditRejectedError",
    "EffectRuntimeOperationsIdempotencyConflictError",
    "EffectRuntimeOperationsService",
    "OutboxDeadLetterNotFoundError",
]
