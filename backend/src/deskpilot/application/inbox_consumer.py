"""Transactional consumer idempotency and bounded Inbox receipt retention."""

from collections.abc import Awaitable, Callable
from datetime import datetime
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.messaging import InboxConsumeResult, OutboxDeliveryEnvelope
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import InboxDeliveryRecord

InboxHandler = Callable[[OutboxDeliveryEnvelope, AsyncSession], Awaitable[None]]


class InboxConsumer:
    """Runs one handler at most once per consumer and logical Outbox message."""

    def __init__(
        self,
        database: Database,
        *,
        consumer_name: str,
        handler: InboxHandler,
    ) -> None:
        if not 1 <= len(consumer_name) <= 96:
            raise ValueError("Inbox consumer name is invalid")
        self._database = database
        self._consumer_name = consumer_name
        self._handler = handler

    async def consume(self, delivery: OutboxDeliveryEnvelope) -> InboxConsumeResult:
        try:
            async with self._database.session() as session:
                async with session.begin():
                    existing = await session.scalar(
                        select(InboxDeliveryRecord).where(
                            InboxDeliveryRecord.consumer_name == self._consumer_name,
                            InboxDeliveryRecord.message_id == delivery.message_id,
                        )
                    )
                    if existing is not None:
                        return InboxConsumeResult(
                            processed=False,
                            duplicate=True,
                            inbox_id=existing.inbox_id,
                        )
                    timestamp = await database_utc_now(session)
                    record = InboxDeliveryRecord(
                        inbox_id=f"inb_{uuid4().hex}",
                        consumer_name=self._consumer_name,
                        message_id=delivery.message_id,
                        delivery_id=delivery.delivery_id,
                        topic=delivery.topic,
                        payload_digest=sha256_digest(delivery.payload),
                        processed_at=timestamp,
                    )
                    session.add(record)
                    await session.flush()
                    await self._handler(delivery, session)
                    return InboxConsumeResult(
                        processed=True,
                        duplicate=False,
                        inbox_id=record.inbox_id,
                    )
        except IntegrityError:
            async with self._database.session() as session:
                existing = await session.scalar(
                    select(InboxDeliveryRecord).where(
                        InboxDeliveryRecord.consumer_name == self._consumer_name,
                        InboxDeliveryRecord.message_id == delivery.message_id,
                    )
                )
                if existing is None:
                    raise
                return InboxConsumeResult(
                    processed=False,
                    duplicate=True,
                    inbox_id=existing.inbox_id,
                )

    async def cleanup(self, *, older_than: datetime) -> int:
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(InboxDeliveryRecord).where(
                        InboxDeliveryRecord.consumer_name == self._consumer_name,
                        InboxDeliveryRecord.processed_at < older_than,
                    )
                )
                return int(getattr(result, "rowcount", 0))
