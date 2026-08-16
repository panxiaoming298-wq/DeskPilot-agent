"""Reliable at-least-once delivery from the database outbox to the live broker."""

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol
from uuid import uuid4

from sqlalchemy import delete, exists, select, update
from sqlalchemy.orm import aliased

from deskpilot.domain.messaging import OutboxDeliveryEnvelope
from deskpilot.infrastructure.database import Database
from deskpilot.infrastructure.database_clock import database_utc_now
from deskpilot.infrastructure.models import OutboxMessageRecord
from deskpilot.infrastructure.postgresql_claims import (
    build_postgresql_outbox_claim_statement,
)

logger = logging.getLogger(__name__)


class DeliveryPublisher(Protocol):
    """Transport boundary required by the durable database Outbox."""

    async def publish_delivery(self, delivery: OutboxDeliveryEnvelope) -> None: ...


@dataclass(frozen=True, slots=True)
class PublishBatchResult:
    attempted: int = 0
    published: int = 0
    failed: int = 0
    fenced: int = 0
    dead_lettered: int = 0


@dataclass(frozen=True, slots=True)
class ClaimedOutboxMessage:
    message_id: str
    topic: str
    payload: dict[str, Any]
    attempt_count: int
    fencing_token: int
    delivery_id: str
    attempted_at: datetime


class OutboxPublisher:
    """Polls committed outbox rows and publishes each message at least once."""

    def __init__(
        self,
        database: Database,
        broker: DeliveryPublisher,
        *,
        poll_interval_seconds: float = 0.05,
        batch_size: int = 100,
        retry_base_seconds: float = 0.25,
        retry_max_seconds: float = 30.0,
        instance_id: str | None = None,
        claim_ttl_seconds: float = 15.0,
        max_attempts: int = 8,
    ) -> None:
        if not 1 <= claim_ttl_seconds <= 3_600:
            raise ValueError("Outbox claim TTL must be between 1 and 3600 seconds")
        resolved_instance_id = instance_id or f"outbox_{uuid4().hex}"
        if not 1 <= len(resolved_instance_id) <= 96:
            raise ValueError("Outbox publisher instance ID is invalid")
        if not 1 <= max_attempts <= 1_000:
            raise ValueError("Outbox max attempts must be between 1 and 1000")
        self._database = database
        self._broker = broker
        self._poll_interval_seconds = poll_interval_seconds
        self._batch_size = batch_size
        self._retry_base_seconds = retry_base_seconds
        self._retry_max_seconds = retry_max_seconds
        self._instance_id = resolved_instance_id
        self._claim_ttl_seconds = claim_ttl_seconds
        self._max_attempts = max_attempts
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
        messages = await self._claim_batch()
        published = 0
        failed = 0
        fenced = 0
        dead_lettered = 0

        for message in messages:
            try:
                if message.topic != "task.event":
                    raise ValueError(f"Unsupported outbox topic: {message.topic}")
                delivery = OutboxDeliveryEnvelope(
                    delivery_id=message.delivery_id,
                    message_id=message.message_id,
                    topic=message.topic,
                    attempt=message.attempt_count + 1,
                    attempted_at=message.attempted_at,
                    payload=message.payload,
                )
                await self._broker.publish_delivery(delivery)
            except Exception as error:
                failure = await self._mark_failed(message, error)
                if failure == "retry":
                    failed += 1
                elif failure == "dead_lettered":
                    dead_lettered += 1
                else:
                    fenced += 1
            else:
                if await self._mark_published(message):
                    published += 1
                else:
                    fenced += 1

        return PublishBatchResult(
            attempted=len(messages),
            published=published,
            failed=failed,
            fenced=fenced,
            dead_lettered=dead_lettered,
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

    async def _claim_batch(self) -> list[ClaimedOutboxMessage]:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                expires_at = database_now + timedelta(seconds=self._claim_ttl_seconds)
                if session.bind is not None and session.bind.dialect.name == "postgresql":
                    rows = (
                        await session.execute(
                            build_postgresql_outbox_claim_statement(
                                owner_id=self._instance_id,
                                database_now=database_now,
                                expires_at=expires_at,
                                batch_size=self._batch_size,
                            )
                        )
                    ).mappings()
                    return [
                        ClaimedOutboxMessage(
                            message_id=str(row["message_id"]),
                            topic=str(row["topic"]),
                            payload=dict(row["payload"]),
                            attempt_count=int(row["attempt_count"]),
                            fencing_token=int(row["claim_fencing_token"]),
                            delivery_id=str(row["delivery_id"]),
                            attempted_at=row["delivery_attempted_at"],
                        )
                        for row in rows
                    ]
                earlier = aliased(OutboxMessageRecord)
                candidates = list(
                    (
                        await session.scalars(
                            select(OutboxMessageRecord)
                            .where(
                                OutboxMessageRecord.published_at.is_(None),
                                OutboxMessageRecord.dead_lettered_at.is_(None),
                                OutboxMessageRecord.available_at <= database_now,
                                (
                                    OutboxMessageRecord.claim_owner_id.is_(None)
                                    | OutboxMessageRecord.claim_expires_at.is_(None)
                                    | (OutboxMessageRecord.claim_expires_at <= database_now)
                                ),
                                ~exists(
                                    select(1).where(
                                        earlier.task_id == OutboxMessageRecord.task_id,
                                        earlier.published_at.is_(None),
                                        earlier.dead_lettered_at.is_(None),
                                        earlier.event_seq < OutboxMessageRecord.event_seq,
                                    )
                                ),
                            )
                            .order_by(
                                OutboxMessageRecord.created_at,
                                OutboxMessageRecord.task_id,
                                OutboxMessageRecord.event_seq,
                            )
                            .limit(self._batch_size)
                        )
                    ).all()
                )
                claimed: list[ClaimedOutboxMessage] = []
                for candidate in candidates:
                    next_fence = candidate.claim_fencing_token + 1
                    delivery_id = f"dlv_{uuid4().hex}"
                    result = await session.execute(
                        update(OutboxMessageRecord)
                        .where(
                            OutboxMessageRecord.message_id == candidate.message_id,
                            OutboxMessageRecord.published_at.is_(None),
                            OutboxMessageRecord.dead_lettered_at.is_(None),
                            OutboxMessageRecord.available_at <= database_now,
                            OutboxMessageRecord.claim_fencing_token
                            == candidate.claim_fencing_token,
                            (
                                OutboxMessageRecord.claim_owner_id.is_(None)
                                | OutboxMessageRecord.claim_expires_at.is_(None)
                                | (OutboxMessageRecord.claim_expires_at <= database_now)
                            ),
                        )
                        .values(
                            claim_owner_id=self._instance_id,
                            claim_acquired_at=database_now,
                            claim_expires_at=expires_at,
                            claim_fencing_token=next_fence,
                            delivery_id=delivery_id,
                            delivery_attempted_at=database_now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if int(getattr(result, "rowcount", 0)) != 1:
                        continue
                    claimed.append(
                        ClaimedOutboxMessage(
                            message_id=candidate.message_id,
                            topic=candidate.topic,
                            payload=dict(candidate.payload),
                            attempt_count=candidate.attempt_count,
                            fencing_token=next_fence,
                            delivery_id=delivery_id,
                            attempted_at=database_now,
                        )
                    )
                return claimed

    async def _mark_published(self, message: ClaimedOutboxMessage) -> bool:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(OutboxMessageRecord)
                    .where(
                        OutboxMessageRecord.message_id == message.message_id,
                        OutboxMessageRecord.published_at.is_(None),
                        OutboxMessageRecord.claim_owner_id == self._instance_id,
                        OutboxMessageRecord.claim_fencing_token == message.fencing_token,
                        OutboxMessageRecord.delivery_id == message.delivery_id,
                        OutboxMessageRecord.claim_expires_at > database_now,
                    )
                    .values(
                        attempt_count=OutboxMessageRecord.attempt_count + 1,
                        published_at=database_now,
                        last_error=None,
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                return int(getattr(result, "rowcount", 0)) == 1

    async def _mark_failed(
        self,
        message: ClaimedOutboxMessage,
        error: Exception,
    ) -> str:
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                exponent = min(message.attempt_count, 16)
                retry_seconds = min(
                    self._retry_base_seconds * (2**exponent),
                    self._retry_max_seconds,
                )
                next_attempt = message.attempt_count + 1
                dead_lettered = next_attempt >= self._max_attempts
                error_text = f"{type(error).__name__}: {error}"[:1_000]
                result = await session.execute(
                    update(OutboxMessageRecord)
                    .where(
                        OutboxMessageRecord.message_id == message.message_id,
                        OutboxMessageRecord.published_at.is_(None),
                        OutboxMessageRecord.dead_lettered_at.is_(None),
                        OutboxMessageRecord.claim_owner_id == self._instance_id,
                        OutboxMessageRecord.claim_fencing_token == message.fencing_token,
                        OutboxMessageRecord.delivery_id == message.delivery_id,
                        OutboxMessageRecord.claim_expires_at > database_now,
                    )
                    .values(
                        attempt_count=next_attempt,
                        available_at=(
                            database_now
                            if dead_lettered
                            else database_now + timedelta(seconds=retry_seconds)
                        ),
                        last_error=error_text,
                        dead_lettered_at=database_now if dead_lettered else None,
                        dead_letter_reason=error_text if dead_lettered else None,
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_expires_at=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if int(getattr(result, "rowcount", 0)) != 1:
                    return "fenced"
                return "dead_lettered" if dead_lettered else "retry"

    async def requeue_dead_letter(self, message_id: str) -> bool:
        """Explicitly requeue one poison message with a fresh delivery attempt."""
        async with self._database.session() as session:
            async with session.begin():
                database_now = await database_utc_now(session)
                result = await session.execute(
                    update(OutboxMessageRecord)
                    .where(
                        OutboxMessageRecord.message_id == message_id,
                        OutboxMessageRecord.published_at.is_(None),
                        OutboxMessageRecord.dead_lettered_at.is_not(None),
                    )
                    .values(
                        attempt_count=0,
                        available_at=database_now,
                        last_error=None,
                        dead_lettered_at=None,
                        dead_letter_reason=None,
                        delivery_id=None,
                        delivery_attempted_at=None,
                        claim_owner_id=None,
                        claim_acquired_at=None,
                        claim_expires_at=None,
                        claim_fencing_token=OutboxMessageRecord.claim_fencing_token + 1,
                    )
                    .execution_options(synchronize_session=False)
                )
                requeued = int(getattr(result, "rowcount", 0)) == 1
        if requeued:
            self.notify()
        return requeued

    async def cleanup_published(self, *, older_than: datetime) -> int:
        """Delete acknowledged Outbox rows older than an explicit retention boundary."""
        async with self._database.session() as session:
            async with session.begin():
                result = await session.execute(
                    delete(OutboxMessageRecord).where(
                        OutboxMessageRecord.published_at.is_not(None),
                        OutboxMessageRecord.published_at < older_than,
                    )
                )
                return int(getattr(result, "rowcount", 0))
