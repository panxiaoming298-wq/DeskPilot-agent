"""Reliable at-least-once delivery from the database outbox to the live broker."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select

from deskpilot.application.event_broker import EventBroker
from deskpilot.domain.schemas import TaskEventRead
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.models import OutboxMessageRecord, utc_now

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    attempted: int = 0
    published: int = 0
    failed: int = 0


class OutboxPublisher:
    """Polls committed outbox rows and publishes each message at least once."""

    def __init__(
        self,
        database: Database,
        broker: EventBroker,
        *,
        poll_interval_seconds: float = 0.05,
        batch_size: int = 100,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 30.0,
    ) -> None:
        self._database = database
        self._broker = broker
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._wake = asyncio.Event()
        self._runner: asyncio.Task[None] | None = None
        self._stopping = False

    def start(self) -> None:
        if self._runner is not None:
            raise RuntimeError("Outbox publisher already started")
        self._stopping = False
        self._runner = asyncio.create_task(self._run(), name="outbox-publisher")
        self.notify()

    def notify(self) -> None:
        """Wake the publisher after a transaction commits; polling remains the fallback."""
        self._wake.set()

    async def shutdown(self) -> None:
        if self._runner is None:
            return
        self._stopping = True
        self.notify()
        await self._runner
        self._runner = None

    async def publish_pending(self) -> PublishBatchResult:
        """Publish one eligible batch. Exposed for deterministic recovery tests."""
        messages = await self._load_batch()
        published = 0
        failed = 0

        for message in messages:
            try:
                if message.topic != "task.event":
                    raise ValueError(f"Unsupported outbox topic: {message.topic}")
                event = TaskEventRead.model_validate(message.payload)
                await self._broker.publish(event)
            except Exception as error:
                failed += 1
                await self._mark_failed(message.message_id, error)
            else:
                published += 1
                await self._mark_published(message.message_id)

        return PublishBatchResult(
            attempted=len(messages),
            published=published,
            failed=failed,
        )

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                result = await self.publish_pending()
            except Exception:
                logger.exception("Unexpected outbox polling failure; polling will continue")
                result = PublishBatchResult()
            if self._stopping:
                return
            if result.attempted >= self._batch_size:
                continue
            try:
                await asyncio.wait_for(
                    self._wake.wait(),
                    timeout=self._poll_interval_seconds,
                )
            except TimeoutError:
                pass

    async def _load_batch(self) -> list[OutboxMessageRecord]:
        async with self._database.session() as session:
            statement = (
                select(OutboxMessageRecord)
                .where(
                    OutboxMessageRecord.published_at.is_(None),
                    OutboxMessageRecord.available_at <= utc_now(),
                )
                .order_by(
                    OutboxMessageRecord.created_at,
                    OutboxMessageRecord.task_id,
                    OutboxMessageRecord.event_seq,
                )
                .limit(self._batch_size)
            )
            return list((await session.scalars(statement)).all())

    async def _mark_published(self, message_id: str) -> None:
        async with self._database.session() as session:
            async with session.begin():
                message = await session.get(OutboxMessageRecord, message_id)
                if message is None or message.published_at is not None:
                    return
                message.attempt_count += 1
                message.published_at = utc_now()
                message.last_error = None

    async def _mark_failed(self, message_id: str, error: Exception) -> None:
        async with self._database.session() as session:
            async with session.begin():
                message = await session.get(OutboxMessageRecord, message_id)
                if message is None or message.published_at is not None:
                    return
                message.attempt_count += 1
                exponent = min(message.attempt_count - 1, 16)
                retry_seconds = min(
                    self._retry_base_seconds * (2**exponent),
                    self._retry_max_seconds,
                )
                message.available_at = utc_now() + timedelta(seconds=retry_seconds)
                message.last_error = f"{type(error).__name__}: {error}"[:1_000]
