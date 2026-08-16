from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import func, select

from deskpilot.application.event_broker import EventBroker
from deskpilot.application.inbox_consumer import InboxConsumer
from deskpilot.application.outbox_publisher import OutboxPublisher
from deskpilot.application.task_service import TaskService
from deskpilot.domain.messaging import OutboxDeliveryEnvelope
from deskpilot.domain.schemas import TaskCreate, TaskEventRead
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import InboxDeliveryRecord, OutboxMessageRecord
from deskpilot.infrastructure.rabbitmq_transport import (
    PermanentRabbitMqDeliveryError,
    verify_task_event_delivery,
)


class AlwaysFailingBroker(EventBroker):
    async def publish(self, event: TaskEventRead) -> None:
        raise RuntimeError("poison delivery")


@pytest.mark.asyncio
async def test_inbox_commits_handler_and_logical_message_deduplication_together(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'inbox.db').as_posix()}")
    await database.migrate()
    handled: list[str] = []

    async def handler(delivery: OutboxDeliveryEnvelope, _: object) -> None:
        handled.append(delivery.delivery_id)

    consumer = InboxConsumer(database, consumer_name="projection", handler=handler)
    first = OutboxDeliveryEnvelope(
        delivery_id="dlv_first",
        message_id="msg_logical",
        topic="task.event",
        attempt=1,
        attempted_at=datetime.now(UTC),
        payload={"value": 1},
    )
    redelivery = first.model_copy(
        update={"delivery_id": "dlv_second", "attempt": 2}
    )
    try:
        accepted = await consumer.consume(first)
        duplicate = await consumer.consume(redelivery)

        assert accepted.processed and not accepted.duplicate
        assert duplicate.duplicate and not duplicate.processed
        assert handled == ["dlv_first"]
        async with database.session() as session:
            count = await session.scalar(
                select(func.count()).select_from(InboxDeliveryRecord)
            )
        assert count == 1
        assert await consumer.cleanup(
            older_than=datetime.now(UTC) + timedelta(seconds=1)
        ) == 1
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_external_delivery_must_match_durable_outbox_content(tmp_path: Path) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'trusted-inbox.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="trusted broker envelope"))
    async with database.session() as session:
        outbox = await session.scalar(
            select(OutboxMessageRecord).where(OutboxMessageRecord.task_id == task.task_id)
        )
        assert outbox is not None
        delivery = OutboxDeliveryEnvelope(
            delivery_id="dlv_trusted",
            message_id=outbox.message_id,
            topic=outbox.topic,
            attempt=1,
            attempted_at=datetime.now(UTC),
            payload=outbox.payload,
        )
    consumer = InboxConsumer(
        database,
        consumer_name="trusted-rabbitmq",
        handler=verify_task_event_delivery,
    )
    forged = delivery.model_copy(update={"payload": {"forged": True}})
    try:
        with pytest.raises(PermanentRabbitMqDeliveryError, match="durable Outbox"):
            await consumer.consume(forged)
        accepted = await consumer.consume(delivery)
        assert accepted.processed and not accepted.duplicate
    finally:
        await database.dispose()


@pytest.mark.asyncio
async def test_outbox_moves_poison_message_to_dlq_and_explicitly_requeues(
    tmp_path: Path,
) -> None:
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'dlq.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    task = await service.create_task(TaskCreate(goal="poison message"))
    publisher = OutboxPublisher(
        database,
        AlwaysFailingBroker(),
        retry_base_seconds=0,
        retry_max_seconds=0,
        max_attempts=2,
    )
    try:
        first = await publisher.publish_pending()
        async with database.session() as session:
            after_first = await session.scalar(
                select(OutboxMessageRecord).where(
                    OutboxMessageRecord.task_id == task.task_id
                )
            )
            assert after_first is not None
            first_delivery_id = after_first.delivery_id
        second = await publisher.publish_pending()

        assert (first.failed, first.dead_lettered) == (1, 0)
        assert (second.failed, second.dead_lettered) == (0, 1)
        assert (await publisher.publish_pending()).attempted == 0
        async with database.session() as session:
            dead = await session.scalar(
                select(OutboxMessageRecord).where(
                    OutboxMessageRecord.task_id == task.task_id
                )
            )
            assert dead is not None
            assert dead.dead_lettered_at is not None
            message_id = dead.message_id

        assert await publisher.requeue_dead_letter(message_id)
        await publisher.publish_pending()
        async with database.session() as session:
            reattempted = await session.get(OutboxMessageRecord, message_id)
            assert reattempted is not None
            assert reattempted.delivery_id != first_delivery_id
    finally:
        await database.dispose()
