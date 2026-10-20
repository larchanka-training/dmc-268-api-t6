"""Review delivery settlement failures stay inside their consumer (#185)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage, AbstractQueue
from aiormq.abc import AbstractChannel, DeliveredMessage
from pamqp.commands import Basic
from pamqp.header import ContentHeader

from app.modules.reviews.application.handle_review_run import DeliveryOutcome
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.infrastructure import amqp

_EXAMPLES = Path(__file__).resolve().parents[1] / "contracts" / "examples"


@dataclass
class Channel:
    is_closed: bool = False
    connection: Any = field(default_factory=lambda: SimpleNamespace(basic_nack=True))
    acked: list[int] = field(default_factory=list)
    nacked: list[bool] = field(default_factory=list)
    nack_error: bool = False
    ack_event: asyncio.Event | None = None

    async def basic_ack(self, delivery_tag: int, multiple: bool = False) -> None:
        self.acked.append(delivery_tag)
        if self.ack_event:
            self.ack_event.set()

    async def basic_nack(
        self, delivery_tag: int, multiple: bool = False, requeue: bool = True
    ) -> None:
        self.nacked.append(requeue)
        if self.nack_error:
            raise RuntimeError("nack unavailable")


def delivery(
    queue: str,
    channel: Channel,
    *,
    tag: int = 1,
    body: bytes | None = None,
    headers: dict[str, Any] | None = None,
) -> aio_pika.IncomingMessage:
    kind = "review.publish" if queue == amqp.PUBLISH_QUEUE else "review.run"
    return aio_pika.IncomingMessage(
        DeliveredMessage(
            delivery=Basic.Deliver(delivery_tag=tag, exchange="reviews", routing_key=queue),
            header=ContentHeader(
                properties=Basic.Properties(
                    message_id=f"{queue}-{tag}-{uuid4()}", headers=headers or {}
                )
            ),
            body=body if body is not None else (_EXAMPLES / f"{kind}.v1.json").read_bytes(),
            channel=cast(AbstractChannel, channel),
        )
    )


@dataclass
class Queue:
    name: str
    deliveries: list[AbstractIncomingMessage]

    @asynccontextmanager
    async def iterator(self) -> AsyncIterator[AsyncIterator[AbstractIncomingMessage]]:
        async def messages() -> AsyncIterator[AbstractIncomingMessage]:
            for message in self.deliveries:
                yield message

        yield messages()


@dataclass
class Exchange:
    published: list[aio_pika.Message] = field(default_factory=list)
    fail: bool = False

    async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
        if self.fail:
            raise RuntimeError("publish unavailable")
        self.published.append(message)


def publisher(exchange: Exchange | None = None) -> amqp.AmqpQueuePublisher:
    target = cast(AbstractExchange, exchange or Exchange())
    return amqp.AmqpQueuePublisher(target, target)


def handle(
    queue: str,
    pub: amqp.AmqpQueuePublisher,
    *,
    outcome: DeliveryOutcome = DeliveryOutcome.ACK,
    error: Exception | None = None,
) -> Callable[[AbstractIncomingMessage], Awaitable[None]]:
    async def run_handler(run_id: UUID) -> DeliveryOutcome:
        if error:
            raise error
        return outcome

    async def publish_handler(pointer: ReviewPublishPointer) -> None:
        if error:
            raise error

    async def dispatch(message: AbstractIncomingMessage) -> None:
        if queue == amqp.PUBLISH_QUEUE:
            await amqp.handle_publish_delivery(message, publish_handler, pub)
        else:
            await amqp.handle_run_delivery(message, run_handler, pub)

    return dispatch


@pytest.mark.parametrize("queue", ["review.run.fast", "review.publish"])
def test_closed_channel_ack_does_not_stop_next_delivery_or_sibling(
    queue: str, caplog: pytest.LogCaptureFixture
) -> None:
    failed_channel, healthy_channel, sibling_channel = Channel(is_closed=True), Channel(), Channel()
    failed = delivery(queue, failed_channel)
    next_message = delivery(queue, healthy_channel, tag=2)
    sibling = delivery("review.run.deep", sibling_channel)

    async def consume_both() -> None:
        next_completed = asyncio.Event()
        healthy_channel.ack_event = next_completed
        sibling_handle = handle("review.run.deep", publisher())

        async def sibling_after_next(message: AbstractIncomingMessage) -> None:
            await next_completed.wait()
            await sibling_handle(message)

        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(
                amqp.consume(
                    cast(AbstractQueue, Queue(queue, [failed, next_message])),
                    handle(queue, publisher()),
                )
            )
            tasks.create_task(
                amqp.consume(
                    cast(AbstractQueue, Queue("review.run.deep", [sibling])),
                    sibling_after_next,
                )
            )

    asyncio.run(consume_both())

    assert healthy_channel.acked == [2]
    assert sibling_channel.acked == [1]
    assert not failed.processed
    assert "Failed to handle" in caplog.text
    assert failed.message_id is not None
    assert queue in caplog.text and failed.message_id in caplog.text
    assert "Failed to nack" in caplog.text


@pytest.mark.parametrize(
    ("queue", "failure"),
    [
        ("review.run.fast", "invalid"),
        ("review.publish", "invalid"),
        ("review.run.fast", "dead_letter"),
        ("review.run.fast", "exhausted"),
        ("review.publish", "exhausted"),
    ],
)
def test_failed_dlq_nack_is_best_effort_and_next_delivery_completes(
    queue: str, failure: str, caplog: pytest.LogCaptureFixture
) -> None:
    failed_channel, healthy_channel = Channel(nack_error=True), Channel()
    failed = delivery(
        queue,
        failed_channel,
        body=b"invalid" if failure == "invalid" else None,
        headers={"x-attempt": 2} if failure == "exhausted" else None,
    )
    next_message = delivery(queue, healthy_channel, tag=2)
    failed_handle = handle(
        queue,
        publisher(),
        outcome=DeliveryOutcome.DEAD_LETTER,
        error=RuntimeError("handler unavailable") if failure == "exhausted" else None,
    )
    healthy_handle = handle(queue, publisher())

    async def dispatch(message: AbstractIncomingMessage) -> None:
        await (failed_handle if message is failed else healthy_handle)(message)

    asyncio.run(amqp.consume(cast(AbstractQueue, Queue(queue, [failed, next_message])), dispatch))

    assert failed_channel.nacked == [False, False]
    assert healthy_channel.acked == [2]
    assert not failed.processed
    assert "Failed to handle" in caplog.text and "Failed to nack" in caplog.text


class BrokenRepublisher(amqp.AmqpQueuePublisher):
    async def republish_after_error(self, message: AbstractIncomingMessage, attempts: int) -> bool:
        raise RuntimeError("unexpected retry helper failure")


@pytest.mark.parametrize("queue", ["review.run.fast", "review.publish"])
def test_unexpected_retry_helper_error_dead_letters_without_requeue_and_keeps_consuming(
    queue: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(amqp, "REQUEUE_ERROR_DELAY_SECONDS", 0.0)
    failed_channel, healthy_channel = Channel(), Channel()
    failed = delivery(queue, failed_channel, tag=10)
    next_message = delivery(queue, healthy_channel, tag=11)
    exchange = cast(AbstractExchange, Exchange())
    broken_handle = handle(
        queue, BrokenRepublisher(exchange, exchange), error=RuntimeError("handler unavailable")
    )
    healthy_handle = handle(queue, publisher())

    async def dispatch(message: AbstractIncomingMessage) -> None:
        await (broken_handle if message is failed else healthy_handle)(message)

    asyncio.run(amqp.consume(cast(AbstractQueue, Queue(queue, [failed, next_message])), dispatch))

    assert failed_channel.nacked == [False]
    assert failed.processed
    assert healthy_channel.acked == [11]


def test_processed_delivery_is_never_settled_again_after_handler_error() -> None:
    channel = Channel()
    message = delivery("review.run.fast", channel)

    async def settled_then_failed(message: AbstractIncomingMessage) -> None:
        await message.ack()
        raise RuntimeError("failure after settlement")

    asyncio.run(
        amqp.consume(cast(AbstractQueue, Queue("review.run.fast", [message])), settled_then_failed)
    )

    assert message.processed
    assert channel.acked == [1] and channel.nacked == []


@pytest.mark.parametrize("during_nack", [False, True])
def test_delivery_cancellation_propagates_without_new_settlement(during_nack: bool) -> None:
    class CancelledChannel(Channel):
        async def basic_nack(
            self, delivery_tag: int, multiple: bool = False, requeue: bool = True
        ) -> None:
            self.nacked.append(requeue)
            raise asyncio.CancelledError

    channel = CancelledChannel()
    message = delivery("review.run.fast", channel)

    async def interrupted(message: AbstractIncomingMessage) -> None:
        if during_nack:
            raise RuntimeError("unsettled delivery failure")
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            amqp.consume(cast(AbstractQueue, Queue("review.run.fast", [message])), interrupted)
        )

    assert not message.processed
    assert channel.acked == []
    assert channel.nacked == ([False] if during_nack else [])


@pytest.mark.parametrize("queue", ["review.run.fast", "review.publish"])
@pytest.mark.parametrize("publish_fails", [False, True])
def test_guard_preserves_republish_ack_and_fallback_nack_failure_behavior(
    queue: str, publish_fails: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(amqp, "REQUEUE_ERROR_DELAY_SECONDS", 0.0)
    failed_channel = Channel(is_closed=not publish_fails, nack_error=publish_fails)
    healthy_channel = Channel()
    failed = delivery(queue, failed_channel, tag=20)
    next_message = delivery(queue, healthy_channel, tag=21)
    exchange = Exchange(fail=publish_fails)
    error_handle = handle(queue, publisher(exchange), error=RuntimeError("handler unavailable"))
    healthy_handle = handle(queue, publisher())

    async def dispatch(message: AbstractIncomingMessage) -> None:
        await (error_handle if message is failed else healthy_handle)(message)

    asyncio.run(amqp.consume(cast(AbstractQueue, Queue(queue, [failed, next_message])), dispatch))

    assert healthy_channel.acked == [21]
    assert not failed.processed
    if publish_fails:
        # Existing helper owns NACK(true); the guard must not add a second settle.
        assert failed_channel.nacked == [True]
        assert exchange.published == []
    else:
        # A confirmed copy followed by a failed ACK is not a second handler error.
        assert failed_channel.nacked == []
        assert len(exchange.published) == 1
        assert exchange.published[0].headers == {"x-attempt": 1}
