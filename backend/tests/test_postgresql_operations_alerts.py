"""Opt-in PostgreSQL multi-engine alert and frozen audit export gates."""

import asyncio
import os

import pytest
from sqlalchemy import delete, update

from deskpilot.application.effect_runtime_operations import EffectRuntimeOperationsService
from deskpilot.application.task_service import TaskService
from deskpilot.domain.effect_graph import CompensationStrategy, EffectDagNodeDefinition
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import (
    TaskRecord,
    ToolEffectDagReadyStateRecord,
    ToolEffectGraphRecord,
)
from deskpilot.infrastructure.postgresql_verification import (
    PostgreSQLVerificationConfigurationError,
    load_postgresql_verification_url,
)
from deskpilot.tools.computer import DISK_USAGE_CONTRACT


def _postgresql_test_url() -> str:
    try:
        raw_url = load_postgresql_verification_url(os.environ)
    except PostgreSQLVerificationConfigurationError as exc:
        pytest.fail(str(exc))
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_POSTGRESQL_URL is not configured")
    return raw_url


def _node() -> EffectDagNodeDefinition:
    return EffectDagNodeDefinition(
        node_key="root",
        step_id="root",
        tool_name=DISK_USAGE_CONTRACT.name,
        tool_version=DISK_USAGE_CONTRACT.version,
        contract_digest=DISK_USAGE_CONTRACT.digest,
        compensation_strategy=CompensationStrategy.NONE,
    )


@pytest.mark.asyncio
@pytest.mark.postgresql_integration
async def test_postgresql_alert_open_is_unique_and_audit_export_head_is_frozen() -> None:
    database_url = _postgresql_test_url()
    databases = (Database(database_url), Database(database_url))
    services = tuple(EffectRuntimeOperationsService(database) for database in databases)
    task_id: str | None = None
    try:
        await databases[0].migrate()
        await services[0].sample_metrics(actor_id="postgresql-setup", sample_limit=10)
        notification_start = (
            await services[0].alert_notification_page(limit=500)
        ).next_after_sequence
        task_service = TaskService(databases[0], "/api/v1")
        task = await task_service.create_task(TaskCreate(goal="postgresql alert contention"))
        task_id = task.task_id
        graph = await task_service.create_effect_dag(task.task_id, (_node(),))
        async with databases[0].session() as session:
            async with session.begin():
                graph_record = await session.get(ToolEffectGraphRecord, graph.graph_id)
                assert graph_record is not None
                await session.execute(
                    update(ToolEffectDagReadyStateRecord)
                    .where(ToolEffectDagReadyStateRecord.graph_id == graph.graph_id)
                    .values(
                        event_seq=graph_record.last_event_seq - 1,
                        ready_node_count=0,
                    )
                )

        sampled = await asyncio.gather(
            services[0].sample_metrics(actor_id="postgresql-a", sample_limit=10),
            services[1].sample_metrics(actor_id="postgresql-b", sample_limit=10),
        )
        assert sum(len(result.alert_notifications) for result in sampled) == 1
        notifications = await services[0].alert_notification_page(after_sequence=notification_start)
        assert len(notifications.notifications) == 1
        assert notifications.notifications[0].transition.value == "opened"
        assert notifications.notifications[0].alert_code == "READY_PROJECTION_REPAIR_REQUIRED"

        first = await services[0].audit_export_page(limit=1)
        frozen_through = first.through_sequence
        assert first.next_cursor is not None
        await services[1].sample_metrics(actor_id="postgresql-c", sample_limit=10)
        cursor: str | None = first.next_cursor
        exported = list(first.events)
        while cursor is not None:
            page = await services[0].audit_export_page(cursor=cursor, limit=500)
            assert page.export_id == first.export_id
            assert page.through_sequence == frozen_through
            exported.extend(page.events)
            cursor = page.next_cursor
        assert exported[-1].sequence == frozen_through
        assert all(event.sequence <= frozen_through for event in exported)
        assert page.has_more is False
        fresh = await services[0].audit_export_page()
        assert fresh.through_sequence == frozen_through + 1
    finally:
        if task_id is not None:
            async with databases[0].session() as session:
                async with session.begin():
                    await session.execute(delete(TaskRecord).where(TaskRecord.task_id == task_id))
            await services[0].sample_metrics(actor_id="postgresql-cleanup", sample_limit=10)
        await asyncio.gather(*(database.dispose() for database in databases))
