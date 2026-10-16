"""RabbitMQ topology, wake-ups and the delivery handler of `index.repo` (#142).

The worker declares this topology at startup on a confirm channel of its own, not on the
review channel; the #143 publishers use the same names and `AmqpIndexWakeups`. A
message only wakes the job of one repository (`index.repo/v1`), and the job's outcome
picks the queue action:

- an invalid body, a foreign `schema` or a `message_id` not derived from the fields →
  `nack(requeue=False)`, so `code_index.dlq`;
- PUBLISHED, UNCHANGED, EMPTY, STALE, FAILED, RELEASED, LEASE_LOST → ack;
- REWAKE → ack: the job already woke the repository with attempt 1;
- BUSY → the 30s queue with the same attempt;
- LEASED → ack; a redelivery, or a message already waiting for a lease, waits in the
  10m queue at most `MAX_LEASE_WAITS` times (the holder died with the lease);
- TRANSIENT → the next attempt as `retry_delay_key` chooses; on attempt 3, ack (the
  job already wrote `last_error_code`);
- an unexpected exception → the `x-attempt` republish of the review queues, the third
  goes to `code_index.dlq`.

A copy is always confirmed before the original is acked; a copy that is not confirmed
leaves the original to the broker (`nack(requeue=True)`).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID, uuid5

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage
from pydantic import BaseModel, ConfigDict, Field

from app.modules.code_index.application.build_index import BuildOutcome, BuildStatus
from app.modules.code_index.application.vcs import VCS_RATE_LIMITED
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.infrastructure.amqp import (
    RETRY_KEYS,
    forget_unexpected_attempts,
    republish_with_attempt,
    requeue_after_error,
    validate_message,
)

_LOGGER = logging.getLogger(__name__)

EXCHANGE = "code_index"
RETRY_EXCHANGE = "code_index.retry"
DEAD_LETTER_EXCHANGE = "code_index.dlx"
DEAD_LETTER_QUEUE = "code_index.dlq"
QUEUE = "index.repo"
SCHEMA = "index.repo.v1"
# SD §7.2: a wake-up's id is the uuid5 of `repository_id:generation:requested_at` here.
MESSAGE_ID_NAMESPACE = UUID("e03f49a6-717b-4702-886d-5b6805fe7d1a")
MAX_ATTEMPTS = 3
LEASE_WAIT_HEADER = "x-lease-wait"
# Two waits in the 10m queue outlast the default lease of 1200 s.
MAX_LEASE_WAITS = 2
_DLQ_RETENTION_MS = 7 * 24 * 60 * 60 * 1000


def retry_queue(delay_key: str) -> str:
    return f"{QUEUE}.retry.{delay_key}"


def message_id(repository_id: UUID, generation: int, requested_at: str) -> UUID:
    """The id of one wake-up; `requested_at` is its wire text."""
    return uuid5(MESSAGE_ID_NAMESPACE, f"{repository_id}:{generation}:{requested_at}")


async def declare_topology(channel: AbstractChannel, delays: RetryDelays) -> None:
    """Declare the `index.repo` topology; queue arguments are immutable once declared."""
    index = await channel.declare_exchange(EXCHANGE, aio_pika.ExchangeType.DIRECT, durable=True)
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
    queue = await channel.declare_queue(
        QUEUE, durable=True, arguments={"x-dead-letter-exchange": DEAD_LETTER_EXCHANGE}
    )
    await queue.bind(index, QUEUE)
    ttls = {"30s": delays.short, "2m": delays.medium, "10m": delays.long}
    for key in RETRY_KEYS:
        name = retry_queue(key)
        retry_q = await channel.declare_queue(
            name,
            durable=True,
            arguments={
                "x-message-ttl": int(ttls[key].total_seconds() * 1000),
                "x-dead-letter-exchange": EXCHANGE,
                # Without it the message would return with its retry key, unroutable.
                "x-dead-letter-routing-key": QUEUE,
            },
        )
        await retry_q.bind(retry, name)


class IndexRepoMessage(BaseModel):
    """Transport model of `contracts/schemas/index.repo.v1.schema.json`."""

    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    schema_name: Literal["index.repo/v1"] = Field(default="index.repo/v1", alias="schema")
    message_id: UUID
    repository_id: UUID
    generation: int
    attempt: int
    # The wire text: the id is derived from it, and a parsed timestamp could change its
    # fraction digits.
    requested_at: str

    @classmethod
    def wake(cls, repository_id: UUID, generation: int, now: datetime) -> IndexRepoMessage:
        requested_at = now.astimezone(UTC).isoformat(timespec="microseconds")
        requested_at = requested_at.replace("+00:00", "Z")
        return cls(
            message_id=message_id(repository_id, generation, requested_at),
            repository_id=repository_id,
            generation=generation,
            attempt=1,
            requested_at=requested_at,
        )

    @property
    def derived(self) -> bool:
        return self.message_id == message_id(self.repository_id, self.generation, self.requested_at)

    def wire(self) -> dict[str, Any]:
        body = self.model_dump(mode="json", by_alias=True)
        validate_message(SCHEMA, body)
        return body


def _amqp_message(
    message: IndexRepoMessage, headers: dict[str, Any] | None = None
) -> aio_pika.Message:
    return aio_pika.Message(
        json.dumps(message.wire(), separators=(",", ":")).encode(),
        headers=headers,
        content_type="application/json",
        delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        message_id=str(message.message_id),
    )


class AmqpIndexWakeups:
    """`IndexWakeups` on a confirm channel: `wake` returns after the publisher confirm."""

    def __init__(
        self, exchange: AbstractExchange, *, now: Callable[[], datetime] | None = None
    ) -> None:
        self._exchange = exchange
        self._now = now or (lambda: datetime.now(UTC))

    @classmethod
    async def open(cls, channel: AbstractChannel) -> AmqpIndexWakeups:
        return cls(await channel.get_exchange(EXCHANGE, ensure=False))

    async def wake(self, repository_id: UUID, generation: int) -> None:
        message = IndexRepoMessage.wake(repository_id, generation, self._now())
        await self._exchange.publish(_amqp_message(message), QUEUE)


class IndexQueuePublisher:
    """The retries and the unexpected-error republish of `index.repo`, each confirmed."""

    def __init__(
        self, index: AbstractExchange, retry: AbstractExchange, delays: RetryDelays
    ) -> None:
        self._index = index
        self._retry = retry
        self.delays = delays

    @classmethod
    async def open(cls, channel: AbstractChannel, delays: RetryDelays) -> IndexQueuePublisher:
        return cls(
            await channel.get_exchange(EXCHANGE, ensure=False),
            await channel.get_exchange(RETRY_EXCHANGE, ensure=False),
            delays,
        )

    async def publish_retry(
        self, message: IndexRepoMessage, delay_key: str, headers: dict[str, Any] | None = None
    ) -> None:
        await self._retry.publish(_amqp_message(message, headers), retry_queue(delay_key))

    async def republish_after_error(self, message: AbstractIncomingMessage, attempts: int) -> bool:
        return await republish_with_attempt(self._index, message, attempts)


def retry_delay_key(attempt: int, outcome: BuildOutcome, delays: RetryDelays) -> str:
    """The retry queue of a TRANSIENT outcome, as for review runs (`RetryDelays`):
    attempt 1 → 30s, 2 → 2m; a rate limit or a `Retry-After` → at least 2m, and a
    `Retry-After` over the 2m delay → 10m. The consumer never sleeps on it."""
    limited = outcome.error_code == VCS_RATE_LIMITED or outcome.retry_after is not None
    if limited and outcome.retry_after is not None and outcome.retry_after > delays.medium:
        return "10m"
    if limited or attempt >= 2:
        return "2m"
    return "30s"


def _decode(message: AbstractIncomingMessage) -> IndexRepoMessage | None:
    """Contract schema first (an unknown major goes to the DLQ), then the transport model."""
    try:
        body = json.loads(message.body)
        if not isinstance(body, dict):
            raise ValueError("message body is not a JSON object")
        validate_message(SCHEMA, body)
        decoded = IndexRepoMessage.model_validate(body)
    except Exception:
        _LOGGER.exception("Rejecting message %s to %s", message.message_id, DEAD_LETTER_QUEUE)
        return None
    if not decoded.derived:
        _LOGGER.error(
            "Rejecting message %s to %s: message_id is not derived from its fields",
            message.message_id,
            DEAD_LETTER_QUEUE,
        )
        return None
    return decoded


def _lease_waits(message: AbstractIncomingMessage) -> int:
    headers = getattr(message, "headers", None) or {}
    try:
        return int(headers.get(LEASE_WAIT_HEADER, 0))
    except (TypeError, ValueError):
        _LOGGER.warning(
            "Invalid header %s=%r on message %s",
            LEASE_WAIT_HEADER,
            headers.get(LEASE_WAIT_HEADER),
            message.message_id,
        )
        return 0


def _next_delivery(
    decoded: IndexRepoMessage,
    outcome: BuildOutcome,
    message: AbstractIncomingMessage,
    delays: RetryDelays,
) -> tuple[IndexRepoMessage, str, dict[str, Any] | None] | None:
    """The copy to put on a retry queue, or `None` when the delivery is only acked."""
    if outcome.status is BuildStatus.BUSY:
        # Not a nack-requeue: it would spin while the admission is full.
        return decoded, "30s", None
    if outcome.status is BuildStatus.TRANSIENT and decoded.attempt < MAX_ATTEMPTS:
        retry = decoded.model_copy(update={"attempt": decoded.attempt + 1})
        return retry, retry_delay_key(decoded.attempt, outcome, delays), None
    if outcome.status is BuildStatus.LEASED:
        waits = _lease_waits(message)
        if (getattr(message, "redelivered", False) or waits > 0) and waits < MAX_LEASE_WAITS:
            return decoded, "10m", {LEASE_WAIT_HEADER: waits + 1}
    return None


async def handle_index_delivery(
    message: AbstractIncomingMessage,
    handler: Callable[[UUID, int], Awaitable[BuildOutcome]],
    publisher: IndexQueuePublisher,
) -> None:
    """Run the job of one `index.repo/v1` wake-up and settle the delivery by its outcome."""
    decoded = _decode(message)
    if decoded is None:
        forget_unexpected_attempts(message)
        await message.nack(requeue=False)
        return
    try:
        outcome = await handler(decoded.repository_id, decoded.generation)
    except Exception:
        await requeue_after_error(message, publisher, dead_letter_queue=DEAD_LETTER_QUEUE)
        return
    forget_unexpected_attempts(message)
    retry = _next_delivery(decoded, outcome, message, publisher.delays)
    if retry is None:
        await message.ack()
        return
    copy, delay_key, headers = retry
    try:
        await publisher.publish_retry(copy, delay_key, headers)
    except Exception:
        _LOGGER.exception(
            "Failed to publish message %s to %s; it is redelivered",
            message.message_id,
            retry_queue(delay_key),
        )
        try:
            await message.nack(requeue=True)
        except Exception:
            _LOGGER.exception("Failed to nack message %s after a publish error", message.message_id)
        return
    _LOGGER.info(
        "code_index delivery message_id=%s status=%s retry=%s attempt=%d",
        decoded.message_id,
        outcome.status.value,
        retry_queue(delay_key),
        copy.attempt,
    )
    await message.ack()
