"""Persistent RabbitMQ review.run/v1 publisher with broker confirms."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import aio_pika
from aio_pika.abc import AbstractExchange

from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage

_ROUTES = {"fast": "review.run.fast", "deep": "review.run.deep"}


class RabbitMqRunPublisher:
    def __init__(self, exchange: AbstractExchange) -> None:
        self._exchange = exchange

    async def publish_confirmed(self, message: PendingRunMessage) -> None:
        try:
            routing_key = _ROUTES[message.engine]
        except KeyError as exc:
            raise ValueError("unsupported review.run engine") from exc
        body = json.dumps(message.as_payload(), separators=(",", ":")).encode("utf-8")
        confirmation = await self._exchange.publish(
            aio_pika.Message(
                body,
                content_type="application/json",
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                message_id=str(message.message_id),
                type="review.run/v1",
            ),
            routing_key=routing_key,
            mandatory=True,
            timeout=10.0,
        )
        if not confirmation:
            raise RuntimeError("review.run/v1 publish lacked broker confirmation")


@asynccontextmanager
async def open_rabbitmq_run_publisher(url: str) -> AsyncIterator[RabbitMqRunPublisher]:
    """Declare durable topology and return a publisher that requires broker confirms."""
    if not url:
        raise ValueError("RABBITMQ_URL is required")
    connection = await aio_pika.connect_robust(url, timeout=10.0)
    async with connection:
        channel = await connection.channel(publisher_confirms=True, on_return_raises=True)
        dead_letters = await channel.declare_exchange(
            "reviews.dlx", aio_pika.ExchangeType.FANOUT, durable=True
        )
        dead_letter_queue = await channel.declare_queue("reviews.dlq", durable=True)
        await dead_letter_queue.bind(dead_letters)
        exchange = await channel.declare_exchange(
            "reviews", aio_pika.ExchangeType.DIRECT, durable=True
        )
        for routing_key in _ROUTES.values():
            queue = await channel.declare_queue(
                routing_key,
                durable=True,
                arguments={"x-max-priority": 10, "x-dead-letter-exchange": "reviews.dlx"},
            )
            await queue.bind(exchange, routing_key=routing_key)
        yield RabbitMqRunPublisher(exchange)
