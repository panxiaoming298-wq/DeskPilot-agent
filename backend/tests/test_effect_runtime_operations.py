from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, update

from deskpilot.application.effect_runtime_operations import (
    EffectRuntimeOperationsAuditRejectedError,
    EffectRuntimeOperationsIdempotencyConflictError,
    EffectRuntimeOperationsService,
)
from deskpilot.application.task_service import TaskService
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.effect_graph import CompensationStrategy, EffectDagNodeDefinition
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    EffectRuntimeAlertNotificationRecord,
    EffectRuntimeOperationsAuditRecord,
    InboxDeliveryRecord,
    OutboxMessageRecord,
    TaskEventRecord,
    ToolEffectDagAdmissionRecord,
    ToolEffectDagReadyNodeRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectGraphControlRecord,
    ToolEffectGraphRecord,
    ToolEffectReadySetCheckpointRecord,
    utc_now,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _database_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _node() -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key="root",
        step_id="root",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


def test_operations_api_is_authenticated_secret_free_and_audit_chained(
    client: TestClient,
    raw_client: TestClient,
    session_token: str,
) -> None:
    assert raw_client.get("/api/v1/operations/effect-runtime").status_code == 401
    forbidden = raw_client.post(
        "/api/v1/operations/effect-runtime:sample",
        headers={"Authorization": f"Bearer {session_token}"},
    )
    assert forbidden.status_code == 403

    secret_goal = "never-expose-this-goal-in-operations"
    assert client.post("/api/v1/tasks", json={"goal": secret_goal}).status_code == 201
    snapshot = client.get("/api/v1/operations/effect-runtime?sample_limit=10")
    assert snapshot.status_code == 200
    assert snapshot.headers["cache-control"] == "no-store"
    body = snapshot.json()
    assert body["schema_version"] == "deskpilot.effect-runtime-operations.v1"
    assert len(body["snapshot_digest"]) == 64
    assert secret_goal not in snapshot.text
    assert '"payload":' not in snapshot.text

    first = client.post("/api/v1/operations/effect-runtime:sample?sample_limit=5")
    second = client.post("/api/v1/operations/effect-runtime:sample?sample_limit=5")
    assert first.status_code == second.status_code == 200
    assert first.json()["audit_event"]["sequence"] + 1 == second.json()["audit_event"]["sequence"]
    assert (
        second.json()["audit_event"]["previous_event_digest"]
        == first.json()["audit_event"]["event_digest"]
    )
    audit = client.get("/api/v1/operations/effect-runtime/audit?limit=1")
    assert audit.status_code == 200
    assert len(audit.json()["events"]) == 1
    assert audit.json()["has_more"] is True
    notifications = client.get("/api/v1/operations/effect-runtime/alerts?limit=1")
    assert notifications.status_code == 200
    assert notifications.headers["cache-control"] == "no-store"
    assert notifications.json()["notifications"] == []
    exported = client.get("/api/v1/operations/effect-runtime/audit/export?limit=1")
    assert exported.status_code == 200
    assert exported.headers["cache-control"] == "no-store"
    assert exported.headers["x-content-type-options"] == "nosniff"
    assert exported.json()["through_sequence"] == 2
    assert len(exported.json()["events"]) == 1
    assert exported.json()["next_cursor"] is not None
    assert secret_goal not in exported.text
    rejected_cursor = client.get(
        "/api/v1/operations/effect-runtime/audit/export",
        params={"cursor": "not-a-valid-cursor"},
    )
    assert rejected_cursor.status_code == 409
    assert rejected_cursor.json()["code"] == "EFFECT_RUNTIME_OPERATIONS_AUDIT_REJECTED"


@pytest.mark.asyncio
async def test_ready_projection_metrics_detect_drift_and_audit_snapshot(
    tmp_path: Path,
) -> None:
    database = Database(_database_url(tmp_path / "operations-ready.db"))
    await database.migrate()
    task_service = TaskService(database, "/api/v1")
    operations = EffectRuntimeOperationsService(database)
    try:
        task = await task_service.create_task(TaskCreate(goal="ready metrics"))
        graph = await task_service.create_effect_dag(task.task_id, (_node(),))
        snapshot = await operations.snapshot(sample_limit=10)
        assert snapshot.ready_projection.projected_graphs == 1
        assert snapshot.ready_projection.projected_nodes == 1
        assert snapshot.ready_projection.ready_nodes == 1
        assert snapshot.ready_projection.rebuilds_observed == 1
        assert snapshot.ready_projection_samples[0].last_rebuild_duration_ms is not None

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(event_seq=1, ready_node_count=0)
                )
        drifted = await operations.snapshot(sample_limit=10)
        assert drifted.ready_projection.event_drift_graphs == 1
        assert drifted.ready_projection.row_count_drift_graphs == 1
        assert "READY_PROJECTION_REPAIR_REQUIRED" in {alert.code for alert in drifted.alerts}

        audited = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert audited.audit_event.result_digest == audited.snapshot.snapshot_digest
        page = await operations.audit_page()
        assert [event.action for event in page.events] == ["metrics.sampled"]
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(EffectRuntimeOperationsAuditRecord)
                    .where(
                        EffectRuntimeOperationsAuditRecord.event_id == audited.audit_event.event_id
                    )
                    .values(details={"tampered": True})
                )
        with pytest.raises(EffectRuntimeOperationsAuditRejectedError):
            await operations.audit_page()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_alert_notifications_track_open_update_resolve_and_reject_tamper(
    tmp_path: Path,
) -> None:
    database = Database(_database_url(tmp_path / "operations-alerts.db"))
    await database.migrate()
    task_service = TaskService(database, "/api/v1")
    wakes: list[tuple[int, ...]] = []
    operations = EffectRuntimeOperationsService(
        database,
        alert_notify=lambda notifications: wakes.append(
            tuple(notification.sequence for notification in notifications)
        ),
    )
    try:
        task = await task_service.create_task(TaskCreate(goal="alert lifecycle"))
        graph = await task_service.create_effect_dag(task.task_id, (_node(),))
        clean = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert clean.alert_notifications == ()

        async with database.session() as session:
            async with session.begin():
                graph_record = await session.get(ToolEffectGraphRecord, graph.graph_id)
                assert graph_record is not None
                graph_event_seq = graph_record.last_event_seq
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(event_seq=graph_event_seq - 1, ready_node_count=0)
                )
        opened = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert [item.transition.value for item in opened.alert_notifications] == ["opened"]
        assert opened.alert_notifications[0].alert_code == "READY_PROJECTION_REPAIR_REQUIRED"
        assert opened.alert_notifications[0].count == 2

        unchanged = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert unchanged.alert_notifications == ()

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(event_seq=graph_event_seq)
                )
        updated = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert [item.transition.value for item in updated.alert_notifications] == ["updated"]
        assert updated.alert_notifications[0].count == 1

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(ready_node_count=1)
                )
        resolved = await operations.sample_metrics(actor_id="test", sample_limit=10)
        assert [item.transition.value for item in resolved.alert_notifications] == ["resolved"]
        assert resolved.alert_notifications[0].count == 0
        assert wakes == [(1,), (2,), (3,)]

        page = await operations.alert_notification_page(limit=2)
        assert [item.sequence for item in page.notifications] == [1, 2]
        assert page.has_more is True
        tail = await operations.alert_notification_page(
            after_sequence=page.next_after_sequence,
            limit=2,
        )
        assert [item.sequence for item in tail.notifications] == [3]
        assert tail.notifications[0].previous_event_digest == page.notifications[-1].event_digest
        assert tail.has_more is False

        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(EffectRuntimeAlertNotificationRecord)
                    .where(EffectRuntimeAlertNotificationRecord.sequence == 2)
                    .values(count=99)
                )
        with pytest.raises(EffectRuntimeOperationsAuditRejectedError):
            await operations.alert_notification_page()
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_audit_export_cursor_freezes_head_and_rejects_tamper(tmp_path: Path) -> None:
    database = Database(_database_url(tmp_path / "operations-export.db"))
    await database.migrate()
    operations = EffectRuntimeOperationsService(database)
    try:
        for _ in range(3):
            await operations.sample_metrics(actor_id="test", sample_limit=1)
        first = await operations.audit_export_page(limit=1)
        assert first.through_sequence == 3
        assert [event.sequence for event in first.events] == [1]
        assert first.has_more is True
        assert first.next_cursor is not None

        await operations.sample_metrics(actor_id="test", sample_limit=1)
        cursor: str | None = first.next_cursor
        exported = list(first.events)
        export_id = first.export_id
        while cursor is not None:
            page = await operations.audit_export_page(cursor=cursor, limit=1)
            assert page.export_id == export_id
            assert page.through_sequence == 3
            exported.extend(page.events)
            cursor = page.next_cursor
        assert [event.sequence for event in exported] == [1, 2, 3]
        assert page.has_more is False

        fresh = await operations.audit_export_page(limit=500)
        assert fresh.through_sequence == 4
        assert [event.sequence for event in fresh.events] == [1, 2, 3, 4]
        assert fresh.export_id != export_id

        assert first.next_cursor is not None
        replacement = "A" if first.next_cursor[-1] != "A" else "B"
        tampered_cursor = first.next_cursor[:-1] + replacement
        with pytest.raises(EffectRuntimeOperationsAuditRejectedError):
            await operations.audit_export_page(cursor=tampered_cursor, limit=1)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_dead_letter_requeue_is_idempotent_audited_and_advances_fence(
    tmp_path: Path,
) -> None:
    database = Database(_database_url(tmp_path / "operations-dlq.db"))
    await database.migrate()
    task_service = TaskService(database, "/api/v1")
    notified = 0

    def notify() -> None:
        nonlocal notified
        notified += 1

    operations = EffectRuntimeOperationsService(database, outbox_notify=notify)
    try:
        first_task = await task_service.create_task(TaskCreate(goal="first poison"))
        second_task = await task_service.create_task(TaskCreate(goal="second poison"))
        dead_lettered_at = utc_now() - timedelta(days=2)
        async with database.session() as session:
            async with session.begin():
                await session.execute(
                    update(OutboxMessageRecord)
                    .where(
                        OutboxMessageRecord.task_id.in_((first_task.task_id, second_task.task_id))
                    )
                    .values(
                        attempt_count=8,
                        dead_lettered_at=dead_lettered_at,
                        dead_letter_reason="sensitive transport failure",
                        last_error="sensitive transport failure",
                        claim_fencing_token=7,
                    )
                )
        async with database.session() as session:
            messages = tuple(
                (
                    await session.scalars(
                        select(OutboxMessageRecord).order_by(OutboxMessageRecord.task_id)
                    )
                ).all()
            )
        first_message, second_message = messages
        result = await operations.requeue_dead_letter(
            first_message.message_id,
            actor_id="operator",
            idempotency_key="requeue-operation-0001",
        )
        assert result.attempt_count == 0
        assert result.claim_fencing_token == 8
        assert notified == 1
        replay = await operations.requeue_dead_letter(
            first_message.message_id,
            actor_id="operator",
            idempotency_key="requeue-operation-0001",
        )
        assert replay.audit_event.event_id == result.audit_event.event_id
        assert notified == 1
        with pytest.raises(EffectRuntimeOperationsIdempotencyConflictError):
            await operations.requeue_dead_letter(
                second_message.message_id,
                actor_id="operator",
                idempotency_key="requeue-operation-0001",
            )
        async with database.session() as session:
            requeued = await session.get(OutboxMessageRecord, first_message.message_id)
            untouched = await session.get(OutboxMessageRecord, second_message.message_id)
            assert requeued is not None and requeued.dead_lettered_at is None
            assert requeued.dead_letter_reason is None
            assert untouched is not None and untouched.dead_lettered_at is not None
            audits = tuple(
                (await session.scalars(select(EffectRuntimeOperationsAuditRecord))).all()
            )
            assert len(audits) == 1
            assert "sensitive transport failure" not in str(audits[0].details)
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_retention_prunes_only_safe_records_and_commits_manifest(
    tmp_path: Path,
) -> None:
    database = Database(_database_url(tmp_path / "operations-retention.db"))
    await database.migrate()
    task_service = TaskService(database, "/api/v1")
    operations = EffectRuntimeOperationsService(database, retention_batch_size=100)
    try:
        task = await task_service.create_task(TaskCreate(goal="terminal graph retention"))
        graph = await task_service.create_effect_dag(task.task_id, (_node(),))
        lease = await task_service.acquire_effect_graph_lease(
            task.task_id,
            owner_id="retention-owner",
            ttl_seconds=30,
        )
        checkpoint = await task_service.checkpoint_effect_dag_ready_set(
            task.task_id,
            lease_owner_id="retention-owner",
            fencing_token=lease.fencing_token,
        )
        dlq_task = await task_service.create_task(TaskCreate(goal="retained DLQ"))
        old = utc_now() - timedelta(days=90)
        async with database.session() as session:
            async with session.begin():
                graph_record = await session.get(ToolEffectGraphRecord, graph.graph_id)
                assert graph_record is not None
                graph_record.status = "succeeded"
                graph_record.updated_at = old
                checkpoint_record = await session.get(
                    ToolEffectReadySetCheckpointRecord,
                    checkpoint.checkpoint_id,
                )
                assert checkpoint_record is not None
                checkpoint_record.created_at = old
                session.add(
                    ToolEffectGraphControlRecord(
                        control_id=f"egc_{'1' * 64}",
                        task_id=task.task_id,
                        graph_id=graph.graph_id,
                        command="cancel",
                        reason="must not enter retention audit",
                        request_digest=sha256_digest({"control": "cancel"}),
                        requested_by="test",
                        target_owner_id=None,
                        target_fencing_token=None,
                        status="applied",
                        revision=2,
                        attempt_count=1,
                        available_at=old,
                        claim_fencing_token=3,
                        applied_graph_fencing_token=lease.fencing_token,
                        created_at=old,
                        updated_at=old,
                        applied_at=old,
                    )
                )
                session.add(
                    ToolEffectDagAdmissionRecord(
                        admission_id="adm_retention",
                        batch_id="bat_retention",
                        graph_id=graph.graph_id,
                        node_id=graph.nodes[0].node_id,
                        tool_name=graph.nodes[0].tool_name,
                        owner_id="test",
                        status="released",
                        lease_ttl_seconds=15,
                        revision=3,
                        fencing_token=2,
                        grant_sequence=1,
                        created_at=old,
                        updated_at=old,
                        granted_at=old,
                        heartbeat_at=old,
                        expires_at=old,
                        released_at=old,
                    )
                )
                await session.execute(
                    update(OutboxMessageRecord)
                    .where(OutboxMessageRecord.task_id == task.task_id)
                    .values(published_at=old)
                )
                await session.execute(
                    update(OutboxMessageRecord)
                    .where(OutboxMessageRecord.task_id == dlq_task.task_id)
                    .values(
                        dead_lettered_at=old,
                        dead_letter_reason="retained poison",
                        attempt_count=8,
                    )
                )
                event = await session.scalar(
                    select(TaskEventRecord).where(TaskEventRecord.task_id == task.task_id)
                )
                assert event is not None
                session.add(
                    InboxDeliveryRecord(
                        inbox_id="inb_retention",
                        consumer_name="test-consumer",
                        message_id="msg_retention",
                        delivery_id="dlv_retention",
                        topic="task.event",
                        payload_digest=sha256_digest({"event_id": event.event_id}),
                        processed_at=old,
                    )
                )

        result = await operations.run_retention(
            actor_id="operator",
            idempotency_key="retention-operation-0001",
            retention_days=30,
        )
        assert result.counts.graph_controls == 1
        assert result.counts.admissions == 1
        assert result.counts.ready_checkpoints == 1
        assert result.counts.ready_nodes == 1
        assert result.counts.ready_states == 1
        assert result.counts.published_outbox >= 1
        assert result.counts.inbox_receipts == 1
        replay = await operations.run_retention(
            actor_id="operator",
            idempotency_key="retention-operation-0001",
            retention_days=30,
        )
        assert replay.audit_event.event_id == result.audit_event.event_id

        async with database.session() as session:
            assert await session.get(ToolEffectDagReadyStateRecord, graph.graph_id) is None
            assert await session.get(ToolEffectDagReadyNodeRecord, graph.nodes[0].node_id) is None
            assert (
                await session.get(
                    ToolEffectReadySetCheckpointRecord,
                    checkpoint.checkpoint_id,
                )
                is None
            )
            dlq = await session.scalar(
                select(OutboxMessageRecord).where(OutboxMessageRecord.task_id == dlq_task.task_id)
            )
            assert dlq is not None and dlq.dead_lettered_at is not None
            assert (
                await session.scalar(
                    select(TaskEventRecord).where(TaskEventRecord.task_id == task.task_id)
                )
                is not None
            )
            audit = await session.get(
                EffectRuntimeOperationsAuditRecord,
                result.audit_event.event_id,
            )
            assert audit is not None
            assert audit.result_digest == result.audit_event.result_digest
            assert "must not enter retention audit" not in str(audit.details)
            assert "retained poison" not in str(audit.details)
    finally:
        await database.dispose()
