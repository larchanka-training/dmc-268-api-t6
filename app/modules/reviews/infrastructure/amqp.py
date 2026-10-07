"""RabbitMQ topology, confirmed publishing and consumers (SD §7, PIPELINE_SPEC §4.3)."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import UUID

import aio_pika
from aio_pika.abc import (
    AbstractChannel,
    AbstractExchange,
    AbstractIncomingMessage,
    AbstractQueue,
    AbstractRobustConnection,
)
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from app.modules.reviews.application.handle_review_run import DeliveryOutcome
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, message_trigger
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.amqp_messages import (
    ReviewPublishMessage,
    ReviewRunMessage,
)

_LOGGER = logging.getLogger(__name__)

EXCHANGE = "reviews"
RETRY_EXCHANGE = "reviews.retry"
DEAD_LETTER_EXCHANGE = "reviews.dlx"
DEAD_LETTER_QUEUE = "reviews.dlq"
PUBLISH_QUEUE = "review.publish"
ENGINES = ("fast", "deep")
RETRY_KEYS = ("30s", "2m", "10m")
# SD §7.1: rerun and the T6 close signal jump the queue.
HIGH_PRIORITY = 9
_MAX_PRIORITY = 10
_DLQ_RETENTION_MS = 7 * 24 * 60 * 60 * 1000
_SCHEMAS = Path(__file__).resolve().parents[4] / "contracts" / "schemas"


def run_queue(engine: str) -> str:
    return f"review.run.{engine}"


def retry_queue(delay_key: str, engine: str) -> str:
    return f"retry.{delay_key}.{engine}"


@cache
def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((_SCHEMAS / f"{name}.schema.json").read_text(encoding="utf-8"))
    return Draft202012Validator(schema, format_checker=Draft202012Validator.FORMAT_CHECKER)


def validate_message(name: str, body: dict[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` for a message outside ``contracts/schemas``."""
    _validator(name).validate(body)


def run_message_body(message: PendingRunMessage) -> dict[str, Any]:
    body = ReviewRunMessage.from_pending(message).wire()
    validate_message("review.run.v1", body)
    return body


def publish_message_body(pointer: ReviewPublishPointer) -> dict[str, Any]:
    body = ReviewPublishMessage.from_pointer(pointer).wire()
    validate_message("review.publish.v1", body)
    return body


async def declare_topology(channel: AbstractChannel, delays: RetryDelays) -> None:
    """Declare SD §7.1 in full; queue arguments are immutable once declared."""
    reviews = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
    retry = await channel.declare_exchange(
        RETRY_EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True
    )
    dlx = await channel.declare_exchange(
        DEAD_LETTER_EXCHANGE, aio_pika.ExchangeType.FANOUT, durable=True
    )
    dlq = await channel.declare_queue(
        DEAD_LETTER_QUEUE, durable=True, arguments={"x-message-ttl": _DLQ_RETENTION_MS}
    )
    await dlq.bind(dlx)
    for engine in ENGINES:
        queue = await channel.declare_queue(
            run_queue(engine),
            durable=True,
            arguments={
                "x-max-priority": _MAX_PRIORITY,
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
            },
        )
        await queue.bind(reviews, run_queue(engine))
        ttls = {"30s": delays.short, "2m": delays.medium, "10m": delays.long}
        for key in RETRY_KEYS:
            name = retry_queue(key, engine)
            retry_q = await channel.declare_queue(
                name,
                durable=True,
                arguments={
                    "x-message-ttl": int(ttls[key].total_seconds() * 1000),
                    "x-dead-letter-exchange": EXCHANGE,
                    # Without it the message would return with its retry key and be lost (§4.3).
                    "x-dead-letter-routing-key": run_queue(engine),
                },
            )
            await retry_q.bind(retry, name)
    publish = await channel.declare_queue(
        PUBLISH_QUEUE,
        durable=True,
        arguments={
            "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE,
        },
    )
    await publish.bind(reviews, PUBLISH_QUEUE)


class AmqpQueuePublisher:
    """Persistent messages on a publisher-confirms channel; each call returns after the confirm."""

    def __init__(self, reviews: AbstractExchange, retry: AbstractExchange) -> None:
        self._reviews = reviews
        self._retry = retry

    @classmethod
    async def open(cls, channel: AbstractChannel) -> AmqpQueuePublisher:
        return cls(
            await channel.get_exchange(EXCHANGE, ensure=False),
            await channel.get_exchange(RETRY_EXCHANGE, ensure=False),
        )

    @staticmethod
    def _message(body: dict[str, Any], priority: int) -> aio_pika.Message:
        return aio_pika.Message(
            json.dumps(body, separators=(",", ":")).encode(),
            content_type="application/json",
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            message_id=body["message_id"],
            priority=priority,
        )

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        rerun = message_trigger(message) == "rerun"
        priority = HIGH_PRIORITY if kind is not RunPublicationKind.QUEUED or rerun else 0
        await self._reviews.publish(
            self._message(run_message_body(message), priority), run_queue(message.engine)
        )

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        priority = HIGH_PRIORITY if message_trigger(message) == "rerun" else 0
        await self._retry.publish(
            self._message(run_message_body(message), priority),
            retry_queue(delay_key, message.engine),
        )

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        await self._reviews.publish(self._message(publish_message_body(pointer), 0), PUBLISH_QUEUE)

    async def republish_after_error(
        self,
        message: AbstractIncomingMessage,
        attempts: int,
    ) -> bool:
        """Report a completed handoff only after both publisher confirm and original ACK."""
        routing_key = getattr(message, "routing_key", "") or ""
        headers = dict(getattr(message, "headers", None) or {})
        headers["x-attempt"] = attempts
        try:
            await self._reviews.publish(
                aio_pika.Message(
                    body=message.body,
                    headers=headers,
                    priority=getattr(message, "priority", 0) or 0,
                    message_id=getattr(message, "message_id", None),
                    correlation_id=getattr(message, "correlation_id", None),
                    content_type=getattr(message, "content_type", None),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                ),
                routing_key=routing_key,
            )
        except Exception:
            _LOGGER.exception(
                "Failed to republish message %s with attempt header; falling back to nack",
                message.message_id,
            )
            try:
                await message.nack(requeue=True)
            except Exception:
                _LOGGER.exception(
                    "Failed to nack message %s after republish error",
                    message.message_id,
                )
            return False

        try:
            await message.ack()
        except Exception:
            _LOGGER.exception(
                "Failed to ack message %s after republish",
                message.message_id,
            )
            return False
        return True


def _decode[T: BaseModel](name: str, model: type[T], message: AbstractIncomingMessage) -> T | None:
    """Contract schema first (unknown major goes to the DLQ), then the transport model."""
    try:
        body = json.loads(message.body)
        if not isinstance(body, dict):
            raise ValueError("message body is not a JSON object")
        validate_message(name, body)
        return model.model_validate(body)
    except Exception:
        _LOGGER.exception("Rejecting message %s to %s", message.message_id, DEAD_LETTER_QUEUE)
        return None


MAX_UNEXPECTED_RETRIES: int = 3
REQUEUE_ERROR_DELAY_SECONDS: float = 1.0
MAX_LOCAL_RETRY_ENTRIES: int = 1024
LOCAL_RETRY_TTL_SECONDS: float = 300.0
# Only an unconfirmed handoff needs local fallback state. Insertion order is expiry order.
_unexpected_delivery_attempts: dict[str, tuple[int, float]] = {}


def _delivery_key(message: AbstractIncomingMessage) -> str:
    if message.message_id:
        return str(message.message_id)
    # Object ids change on broker redelivery. Keep a bounded, stable fallback identity.
    route = str(getattr(message, "routing_key", "") or "").encode()
    return "anonymous:" + hashlib.sha256(route + b"\0" + message.body).hexdigest()


def _prune_delivery_attempts() -> None:
    """Expire idle fallback entries lazily when another retry needs local state."""
    now = monotonic()
    while _unexpected_delivery_attempts:
        oldest = next(iter(_unexpected_delivery_attempts))
        if _unexpected_delivery_attempts[oldest][1] > now:
            break
        _unexpected_delivery_attempts.pop(oldest)


def _remember_delivery_attempts(key: str, attempts: int) -> None:
    _prune_delivery_attempts()
    _unexpected_delivery_attempts.pop(key, None)
    while len(_unexpected_delivery_attempts) >= MAX_LOCAL_RETRY_ENTRIES:
        _unexpected_delivery_attempts.pop(next(iter(_unexpected_delivery_attempts)))
    _unexpected_delivery_attempts[key] = (attempts, monotonic() + LOCAL_RETRY_TTL_SECONDS)


def _clear_delivery_attempts(key: str) -> None:
    _unexpected_delivery_attempts.pop(key, None)


def _get_delivery_attempts(message: AbstractIncomingMessage) -> int:
    _prune_delivery_attempts()
    key = _delivery_key(message)
    local_attempts = _unexpected_delivery_attempts.get(key, (0, 0.0))[0]
    broker_attempts = 0
    headers = getattr(message, "headers", None)
    if isinstance(headers, dict) and "x-attempt" in headers:
        try:
            broker_attempts = max(broker_attempts, int(headers["x-attempt"]))
        except (ValueError, TypeError):
            _LOGGER.warning(
                "Invalid retry header %s=%r on message %s",
                "x-attempt",
                headers["x-attempt"],
                message.message_id,
            )
    return max(broker_attempts, local_attempts) + 1


async def _requeue_after_error(
    message: AbstractIncomingMessage,
    publisher: AmqpQueuePublisher,
    max_retries: int = MAX_UNEXPECTED_RETRIES,
) -> None:
    key = _delivery_key(message)
    attempts = _get_delivery_attempts(message)
    _remember_delivery_attempts(key, attempts)

    if attempts >= max_retries:
        _LOGGER.error(
            "Message %s exceeded maximum unexpected retry attempts (%d); routing to DLQ %s",
            message.message_id,
            max_retries,
            DEAD_LETTER_QUEUE,
            exc_info=True,
        )
        _clear_delivery_attempts(key)
        await message.nack(requeue=False)
        return

    _LOGGER.exception(
        "Message %s handling failed (attempt %d/%d); it is redelivered",
        message.message_id,
        attempts,
        max_retries,
    )
    # A short pause keeps a broken dependency from spinning redeliveries.
    if REQUEUE_ERROR_DELAY_SECONDS > 0:
        await asyncio.sleep(REQUEUE_ERROR_DELAY_SECONDS)

    if await publisher.republish_after_error(message, attempts):
        _clear_delivery_attempts(key)


async def handle_run_delivery(
    message: AbstractIncomingMessage,
    handler: Callable[[UUID], Awaitable[DeliveryOutcome]],
    publisher: AmqpQueuePublisher,
) -> None:
    """Unknown ``schema`` or an invalid body goes to ``reviews.dlq`` without processing (§4.4)."""
    decoded = _decode("review.run.v1", ReviewRunMessage, message)
    if decoded is None or decoded.message_id != decoded.run_id:
        _clear_delivery_attempts(_delivery_key(message))
        await message.nack(requeue=False)
        return
    try:
        outcome = await handler(decoded.run_id)
    except Exception:
        await _requeue_after_error(message, publisher)
        return
    _clear_delivery_attempts(_delivery_key(message))
    if outcome is DeliveryOutcome.DEAD_LETTER:
        await message.nack(requeue=False)
    else:
        await message.ack()


async def handle_publish_delivery(
    message: AbstractIncomingMessage,
    handler: Callable[[ReviewPublishPointer], Awaitable[None]],
    publisher: AmqpQueuePublisher,
) -> None:
    decoded = _decode("review.publish.v1", ReviewPublishMessage, message)
    if decoded is None:
        _clear_delivery_attempts(_delivery_key(message))
        await message.nack(requeue=False)
        return
    try:
        await handler(decoded.to_pointer())
    except Exception:
        await _requeue_after_error(message, publisher)
        return
    _clear_delivery_attempts(_delivery_key(message))
    await message.ack()


async def consume(
    queue: AbstractQueue, handle: Callable[[AbstractIncomingMessage], Awaitable[None]]
) -> None:
    """Handle deliveries one at a time; the channel's prefetch is 1."""
    async with queue.iterator() as messages:
        async for message in messages:
            await handle(message)


@dataclass(frozen=True)
class AmqpChannels:
    connection: AbstractRobustConnection
    publisher: AmqpQueuePublisher

    async def consumer_queue(self, name: str) -> AbstractQueue:
        """A queue on its own channel with ``prefetch_count=1`` (SD §7.1)."""
        channel = await self.connection.channel()
        await channel.set_qos(prefetch_count=1)
        return await channel.get_queue(name, ensure=False)


@asynccontextmanager
async def amqp_channels(url: str, delays: RetryDelays) -> AsyncIterator[AmqpChannels]:
    """Connect, declare the topology and open a confirmed publishing channel."""
    connection = await aio_pika.connect_robust(url)
    try:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        await declare_topology(channel, delays)
        yield AmqpChannels(connection, await AmqpQueuePublisher.open(channel))
    finally:
        await connection.close()


class LazyAmqpPublisher:
    """Connect on first publication, so that portal-api starts without a reachable broker.

    A failed publication raises; the caller leaves the Run for the outbox replay or the
    reconciler. The robust connection reconnects on its own once it was established.
    """

    def __init__(self, url: str, delays: RetryDelays | None = None) -> None:
        self._url = url
        self._delays = delays or RetryDelays()
        self._lock = asyncio.Lock()
        self._stack: AsyncExitStack | None = None
        self._publisher: AmqpQueuePublisher | None = None

    async def _connected(self) -> AmqpQueuePublisher:
        async with self._lock:
            if self._publisher is None:
                stack = AsyncExitStack()
                try:
                    channels = await stack.enter_async_context(
                        amqp_channels(self._url, self._delays)
                    )
                except BaseException:
                    await stack.aclose()
                    raise
                self._stack, self._publisher = stack, channels.publisher
            return self._publisher

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        await (await self._connected()).publish_confirmed(message, kind=kind)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        await (await self._connected()).publish_review(pointer)

    async def aclose(self) -> None:
        if self._stack is not None:
            await self._stack.aclose()
        self._stack, self._publisher = None, None
