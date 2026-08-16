"""RabbitMQ transport with confirms, manual acknowledgements, and Inbox deduplication."""

import logging
from collections.abc import Awaitable, Callable

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from deskpilot.application.event_broker import EventBroker
from deskpilot.application.inbox_consumer import InboxConsumer
from deskpilot.core.canonical_json import sha256_digest
from deskpilot.domain.messaging import InboxConsumeResult, OutboxDeliveryEnvelope
from deskpilot.infrastructure.models import OutboxMessageRecord

logger = logging.getLogger(__name__)

AfterConfirmHook = Callable[[OutboxDeliveryEnvelope], Awaitable[None]]
BeforeAckHook = Callable[
    [OutboxDeliveryEnvelope, InboxConsumeResult, bool],
    Awaitable[None],
]


class PermanentRabbitMqDeliveryError(RuntimeError):
    """A valid envelope that cannot be bound to trusted database state."""


async def verify_task_event_delivery(
    delivery: OutboxDeliveryEnvelope,
    session: AsyncSession,
) -> None:
    """Bind an external envelope to its durable logical Outbox message and content."""
    outbox = await session.get(OutboxMessageRecord, delivery.message_id)
    if (
        outbox is None
        or outbox.topic != delivery.topic
        or sha256_digest(outbox.payload) != sha256_digest(delivery.payload)
    ):
        raise PermanentRabbitMqDeliveryError("RabbitMQ delivery does not match the durable Outbox")


class RabbitMqDeliveryPublisher:
    """Publishes persistent Outbox envelopes and waits for broker confirms."""

    def __init__(
        self,
        url: str,
        *,
        exchange_name: str,
        queue_name: str,
        routing_key: str,
        connection_timeout_seconds: float = 10,
        publish_timeout_seconds: float = 10,
        after_confirm_hook: AfterConfirmHook | None = None,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._queue_name = queue_name
        self._routing_key = routing_key
        self._connection_timeout_seconds = connection_timeout_seconds
        self._publish_timeout_seconds = publish_timeout_seconds
        self._after_confirm_hook = after_confirm_hook
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def start(self) -> None:
        if self._connection is not None:
            raise RuntimeError("RabbitMQ publisher already started")
        connection: AbstractRobustConnection | None = None
        try:
            connection = await aio_pika.connect_robust(
                self._url,
                timeout=self._connection_timeout_seconds,
            )
            channel = await connection.channel(
                publisher_confirms=True,
                on_return_raises=True,
            )
            exchange = await channel.declare_exchange(
                self._exchange_name,
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            queue = await channel.declare_queue(self._queue_name, durable=True)
            await queue.bind(exchange, routing_key=self._routing_key)
        except BaseException:
            if connection is not None:
                await connection.close()
            raise
        self._connection = connection
        self._channel = channel
        self._exchange = exchange

    async def publish_delivery(self, delivery: OutboxDeliveryEnvelope) -> None:
        exchange = self._exchange
        if exchange is None:
            raise RuntimeError("RabbitMQ publisher is not started")
        await exchange.publish(
            aio_pika.Message(
                body=delivery.model_dump_json().encode("utf-8"),
                content_type="application/json",
                content_encoding="utf-8",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=delivery.message_id,
                correlation_id=delivery.delivery_id,
                type=delivery.topic,
            ),
            routing_key=self._routing_key,
            mandatory=True,
            timeout=self._publish_timeout_seconds,
        )
        if self._after_confirm_hook is not None:
            await self._after_confirm_hook(delivery)

    async def shutdown(self) -> None:
        connection = self._connection
        self._exchange = None
        self._channel = None
        self._connection = None
        if connection is not None and not connection.is_closed:
            await connection.close()


class RabbitMqInboxWorker:
    """Consumes broker deliveries transactionally and acknowledges only after commit."""

    def __init__(
        self,
        url: str,
        *,
        exchange_name: str,
        queue_name: str,
        routing_key: str,
        inbox_consumer: InboxConsumer,
        live_broker: EventBroker,
        prefetch_count: int = 32,
        connection_timeout_seconds: float = 10,
        before_ack_hook: BeforeAckHook | None = None,
    ) -> None:
        self._url = url
        self._exchange_name = exchange_name
        self._queue_name = queue_name
        self._routing_key = routing_key
        self._inbox_consumer = inbox_consumer
        self._live_broker = live_broker
        self._prefetch_count = prefetch_count
        self._connection_timeout_seconds = connection_timeout_seconds
        self._before_ack_hook = before_ack_hook
        self._connection: AbstractRobustConnection | None = None
        self._channel: AbstractChannel | None = None
        self._queue: AbstractQueue | None = None
        self._consumer_tag: str | None = None

    async def start(self) -> None:
        if self._connection is not None:
            raise RuntimeError("RabbitMQ Inbox worker already started")
        connection: AbstractRobustConnection | None = None
        try:
            connection = await aio_pika.connect_robust(
                self._url,
                timeout=self._connection_timeout_seconds,
            )
            channel = await connection.channel(publisher_confirms=False)
            await channel.set_qos(prefetch_count=self._prefetch_count)
            exchange = await channel.declare_exchange(
                self._exchange_name,
                aio_pika.ExchangeType.DIRECT,
                durable=True,
            )
            queue = await channel.declare_queue(self._queue_name, durable=True)
            await queue.bind(exchange, routing_key=self._routing_key)
            consumer_tag = await queue.consume(self._consume_message, no_ack=False)
        except BaseException:
            if connection is not None:
                await connection.close()
            raise
        self._connection = connection
        self._channel = channel
        self._queue = queue
        self._consumer_tag = consumer_tag

    async def _consume_message(self, message: AbstractIncomingMessage) -> None:
        try:
            delivery = OutboxDeliveryEnvelope.model_validate_json(message.body)
            if delivery.topic != "task.event":
                raise ValueError(f"Unsupported delivery topic: {delivery.topic}")
        except (ValidationError, ValueError):
            logger.exception(
                "Rejecting invalid RabbitMQ delivery message_id=%s",
                message.message_id,
            )
            await message.reject(requeue=False)
            return

        try:
            result = await self._inbox_consumer.consume(delivery)
            if result.processed:
                await self._live_broker.publish_delivery(delivery)
            if self._before_ack_hook is not None:
                await self._before_ack_hook(
                    delivery,
                    result,
                    bool(message.redelivered),
                )
            await message.ack()
        except PermanentRabbitMqDeliveryError:
            logger.warning(
                "Rejecting untrusted RabbitMQ delivery message_id=%s delivery_id=%s",
                delivery.message_id,
                delivery.delivery_id,
            )
            if not message.processed and not message.channel.is_closed:
                await message.reject(requeue=False)
        except Exception:
            logger.exception(
                "RabbitMQ delivery failed before ack message_id=%s delivery_id=%s",
                delivery.message_id,
                delivery.delivery_id,
            )
            if not message.processed and not message.channel.is_closed:
                await message.reject(requeue=True)

    async def close_connection_before_ack(self) -> None:
        """Fault-injection seam: close the consumer connection without acknowledging."""
        connection = self._connection
        if connection is None:
            raise RuntimeError("RabbitMQ Inbox worker is not started")
        await connection.close()

    async def shutdown(self) -> None:
        connection = self._connection
        self._consumer_tag = None
        self._queue = None
        self._channel = None
        self._connection = None
        if connection is not None and not connection.is_closed:
            await connection.close()


class RabbitMqEventTransport:
    """Lifecycle facade combining one publisher and one Inbox consumer worker."""

    def __init__(
        self,
        url: str,
        *,
        exchange_name: str,
        queue_name: str,
        routing_key: str,
        inbox_consumer: InboxConsumer,
        live_broker: EventBroker,
        prefetch_count: int = 32,
        connection_timeout_seconds: float = 10,
        publish_timeout_seconds: float = 10,
    ) -> None:
        self.publisher = RabbitMqDeliveryPublisher(
            url,
            exchange_name=exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            connection_timeout_seconds=connection_timeout_seconds,
            publish_timeout_seconds=publish_timeout_seconds,
        )
        self.consumer = RabbitMqInboxWorker(
            url,
            exchange_name=exchange_name,
            queue_name=queue_name,
            routing_key=routing_key,
            inbox_consumer=inbox_consumer,
            live_broker=live_broker,
            prefetch_count=prefetch_count,
            connection_timeout_seconds=connection_timeout_seconds,
        )

    async def start(self) -> None:
        await self.publisher.start()
        try:
            await self.consumer.start()
        except BaseException:
            await self.publisher.shutdown()
            raise

    async def shutdown(self) -> None:
        await self.consumer.shutdown()
        await self.publisher.shutdown()


__all__ = [
    "AfterConfirmHook",
    "BeforeAckHook",
    "PermanentRabbitMqDeliveryError",
    "RabbitMqDeliveryPublisher",
    "RabbitMqEventTransport",
    "RabbitMqInboxWorker",
    "verify_task_event_delivery",
]
