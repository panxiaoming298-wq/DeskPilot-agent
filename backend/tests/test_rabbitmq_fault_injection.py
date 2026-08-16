import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.event_broker import EventBroker
from deskpilot.application.inbox_consumer import InboxConsumer
from deskpilot.application.outbox_publisher import ClaimedOutboxMessage, OutboxPublisher
from deskpilot.application.task_service import TaskService
from deskpilot.domain.messaging import (
    InboxConsumeResult,
    OutboxDeliveryEnvelope,
)
from deskpilot.domain.schemas import TaskCreate
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import InboxDeliveryRecord, OutboxMessageRecord
from deskpilot.infrastructure.rabbitmq_transport import (
    RabbitMqDeliveryPublisher,
    RabbitMqInboxWorker,
    verify_task_event_delivery,
)
from deskpilot.infrastructure.rabbitmq_verification import (
    load_rabbitmq_verification_url,
)


class RecordingEventBroker(EventBroker):
    def __init__(self) -> None:
        super().__init__()
        self.deliveries: list[OutboxDeliveryEnvelope] = []

    async def publish_delivery(self, delivery: OutboxDeliveryEnvelope) -> None:
        self.deliveries.append(delivery)
        await super().publish_delivery(delivery)


def _rabbitmq_test_url() -> str:
    raw_url = load_rabbitmq_verification_url(os.environ)
    if raw_url is None:
        pytest.skip("DESKPILOT_TEST_RABBITMQ_URL is not configured")
    return raw_url


def _delivery(claim: ClaimedOutboxMessage) -> OutboxDeliveryEnvelope:
    return OutboxDeliveryEnvelope(
        delivery_id=claim.delivery_id,
        message_id=claim.message_id,
        topic=claim.topic,
        attempt=claim.attempt_count + 1,
        attempted_at=claim.attempted_at,
        payload=claim.payload,
    )


@pytest.mark.asyncio
@pytest.mark.rabbitmq_integration
async def test_real_rabbitmq_redelivery_inbox_dedupe_dlq_and_publisher_fence(
    tmp_path: Path,
) -> None:
    rabbitmq_url = _rabbitmq_test_url()
    suffix = uuid4().hex
    exchange_name = f"deskpilot.test.events.{suffix}"
    queue_name = f"deskpilot.test.task-events.{suffix}"
    routing_key = "task.event"
    database = Database(f"sqlite+aiosqlite:///{(tmp_path / 'rabbitmq.db').as_posix()}")
    await database.migrate()
    service = TaskService(database, "/api/v1")
    handled: list[str] = []

    async def handler(
        delivery: OutboxDeliveryEnvelope,
        session: AsyncSession,
    ) -> None:
        await verify_task_event_delivery(delivery, session)
        handled.append(delivery.message_id)

    inbox = InboxConsumer(
        database,
        consumer_name=f"rabbitmq-test-{suffix}",
        handler=handler,
    )
    live_broker = RecordingEventBroker()
    confirm_count = 0

    async def lose_first_confirm_response(_delivery: OutboxDeliveryEnvelope) -> None:
        nonlocal confirm_count
        confirm_count += 1
        if confirm_count == 1:
            raise RuntimeError("simulated publisher response loss after broker confirm")

    broker_publisher = RabbitMqDeliveryPublisher(
        rabbitmq_url,
        exchange_name=exchange_name,
        queue_name=queue_name,
        routing_key=routing_key,
        after_confirm_hook=lose_first_confirm_response,
    )
    workers: list[RabbitMqInboxWorker] = []
    try:
        await broker_publisher.start()
        await service.create_task(TaskCreate(goal="RabbitMQ confirm loss and redelivery"))
        stale = OutboxPublisher(
            database,
            broker_publisher,
            instance_id="rabbitmq_stale_publisher",
            claim_ttl_seconds=1,
        )
        current = OutboxPublisher(
            database,
            broker_publisher,
            instance_id="rabbitmq_current_publisher",
            claim_ttl_seconds=1,
        )
        stale_claim = (await stale._claim_batch())[0]
        with pytest.raises(RuntimeError, match="response loss"):
            await broker_publisher.publish_delivery(_delivery(stale_claim))

        await asyncio.sleep(1.1)
        current_claim = (await current._claim_batch())[0]
        assert current_claim.message_id == stale_claim.message_id
        assert current_claim.delivery_id != stale_claim.delivery_id
        assert current_claim.fencing_token == stale_claim.fencing_token + 1
        assert not await stale._mark_published(stale_claim)
        assert await stale._mark_failed(stale_claim, RuntimeError("late failure")) == "fenced"
        await broker_publisher.publish_delivery(_delivery(current_claim))
        assert await current._mark_published(current_claim)

        first_seen = asyncio.Event()
        first_observations: list[tuple[str, InboxConsumeResult, bool]] = []
        first_worker: RabbitMqInboxWorker

        async def disconnect_before_first_ack(
            delivery: OutboxDeliveryEnvelope,
            result: InboxConsumeResult,
            redelivered: bool,
        ) -> None:
            first_observations.append((delivery.delivery_id, result, redelivered))
            first_seen.set()
            await first_worker.close_connection_before_ack()

        first_worker = RabbitMqInboxWorker(
            rabbitmq_url,
            exchange_name=exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            inbox_consumer=inbox,
            live_broker=live_broker,
            prefetch_count=1,
            before_ack_hook=disconnect_before_first_ack,
        )
        workers.append(first_worker)
        await first_worker.start()
        await asyncio.wait_for(first_seen.wait(), timeout=10)
        await first_worker.shutdown()

        duplicates_drained = asyncio.Event()
        duplicate_observations: list[tuple[str, InboxConsumeResult, bool]] = []

        async def observe_duplicates(
            delivery: OutboxDeliveryEnvelope,
            result: InboxConsumeResult,
            redelivered: bool,
        ) -> None:
            duplicate_observations.append((delivery.delivery_id, result, redelivered))
            if len(duplicate_observations) == 2:
                duplicates_drained.set()

        second_worker = RabbitMqInboxWorker(
            rabbitmq_url,
            exchange_name=exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            inbox_consumer=inbox,
            live_broker=live_broker,
            prefetch_count=1,
            before_ack_hook=observe_duplicates,
        )
        workers.append(second_worker)
        await second_worker.start()
        await asyncio.wait_for(duplicates_drained.wait(), timeout=10)
        await second_worker.shutdown()

        assert len(first_observations) == 1
        assert first_observations[0][1].processed
        assert not first_observations[0][1].duplicate
        assert {item[0] for item in duplicate_observations} == {
            stale_claim.delivery_id,
            current_claim.delivery_id,
        }
        assert all(item[1].duplicate and not item[1].processed for item in duplicate_observations)
        assert any(item[2] for item in duplicate_observations)
        assert handled == [stale_claim.message_id]
        assert [item.message_id for item in live_broker.deliveries] == [stale_claim.message_id]
        async with database.session() as session:
            inbox_count = await session.scalar(
                select(func.count()).select_from(InboxDeliveryRecord)
            )
        assert inbox_count == 1

        await broker_publisher.shutdown()
        poison_task = await service.create_task(TaskCreate(goal="RabbitMQ transport DLQ requeue"))
        dead_letter_publisher = OutboxPublisher(
            database,
            broker_publisher,
            instance_id="rabbitmq_dlq_publisher",
            retry_base_seconds=0,
            retry_max_seconds=0,
            max_attempts=2,
        )
        first_failure = await dead_letter_publisher.publish_pending()
        second_failure = await dead_letter_publisher.publish_pending()
        assert (first_failure.failed, first_failure.dead_lettered) == (1, 0)
        assert (second_failure.failed, second_failure.dead_lettered) == (0, 1)
        async with database.session() as session:
            dead = await session.scalar(
                select(OutboxMessageRecord).where(
                    OutboxMessageRecord.task_id == poison_task.task_id
                )
            )
            assert dead is not None
            assert dead.dead_lettered_at is not None
            poison_message_id = dead.message_id
            dead_delivery_id = dead.delivery_id
            dead_fence = dead.claim_fencing_token

        assert await dead_letter_publisher.requeue_dead_letter(poison_message_id)
        await broker_publisher.start()
        poison_seen = asyncio.Event()

        async def observe_requeued_message(
            delivery: OutboxDeliveryEnvelope,
            result: InboxConsumeResult,
            _redelivered: bool,
        ) -> None:
            if delivery.message_id == poison_message_id and result.processed:
                poison_seen.set()

        third_worker = RabbitMqInboxWorker(
            rabbitmq_url,
            exchange_name=exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            inbox_consumer=inbox,
            live_broker=live_broker,
            prefetch_count=1,
            before_ack_hook=observe_requeued_message,
        )
        workers.append(third_worker)
        await third_worker.start()
        requeued_result = await dead_letter_publisher.publish_pending()
        assert requeued_result.published == 1
        await asyncio.wait_for(poison_seen.wait(), timeout=10)
        await third_worker.shutdown()
        async with database.session() as session:
            published = await session.get(OutboxMessageRecord, poison_message_id)
            assert published is not None
            assert published.published_at is not None
            assert published.delivery_id != dead_delivery_id
            assert published.claim_fencing_token > dead_fence
    finally:
        for worker in reversed(workers):
            await worker.shutdown()
        await broker_publisher.shutdown()
        await database.dispose()
