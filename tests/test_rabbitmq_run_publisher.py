"""Confirmed and routable RabbitMQ review.run/v1 publication."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Self, cast
from uuid import UUID

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange

from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure.rabbitmq_run_publisher import (
    RabbitMqRunPublisher,
    open_rabbitmq_run_publisher,
)

_RUN = UUID("11111111-1111-1111-1111-111111111111")
_WS = UUID("22222222-2222-2222-2222-222222222222")
_REPO = UUID("33333333-3333-3333-3333-333333333333")
_RULE = UUID("44444444-4444-4444-4444-444444444444")
_PROMPT = UUID("55555555-5555-5555-5555-555555555555")


def _message(engine: str = "fast") -> PendingRunMessage:
    return PendingRunMessage(
        run_id=_RUN,
        workspace_id=_WS,
        installation_id=17,
        repository_id=_REPO,
        repository_external_id=101,
        repository_full_name="octo/repo",
        pr_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        base_ref="main",
        engine=engine,
        rule_version_id=_RULE,
        prompt_version_id=_PROMPT,
        attempt=1,
        requested_at=datetime(2026, 9, 28, 12, 0, tzinfo=UTC),
    )


@dataclass
class Exchange:
    result: object = True
    sent: list[tuple[aio_pika.Message, str, bool]] = field(default_factory=list)

    async def publish(
        self, message: aio_pika.Message, routing_key: str, *, mandatory: bool, timeout: float
    ) -> object:
        self.sent.append((message, routing_key, mandatory))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


@pytest.mark.parametrize(
    ("engine", "route"), [("fast", "review.run.fast"), ("deep", "review.run.deep")]
)
def test_publisher_sends_persistent_pointer_and_waits_for_confirm(engine: str, route: str) -> None:
    exchange = Exchange()
    publisher = RabbitMqRunPublisher(cast(AbstractExchange, exchange))

    asyncio.run(publisher.publish_confirmed(_message(engine)))

    assert len(exchange.sent) == 1
    message, routing_key, mandatory = exchange.sent[0]
    assert routing_key == route
    assert mandatory is True
    assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
    assert message.message_id == str(_RUN)
    assert message.content_type == "application/json"
    assert json.loads(message.body) == _message(engine).as_payload()


@pytest.mark.parametrize("confirmation", [False, None, RuntimeError("broker rejected")])
def test_unconfirmed_or_failed_publish_raises(confirmation: object) -> None:
    exchange = Exchange(result=confirmation)
    publisher = RabbitMqRunPublisher(cast(AbstractExchange, exchange))

    with pytest.raises(RuntimeError, match="publish|broker"):
        asyncio.run(publisher.publish_confirmed(_message()))


def test_unknown_engine_cannot_use_an_unrouted_queue() -> None:
    exchange = Exchange()
    publisher = RabbitMqRunPublisher(cast(AbstractExchange, exchange))

    with pytest.raises(ValueError, match="engine"):
        asyncio.run(publisher.publish_confirmed(_message("unconfigured")))
    assert exchange.sent == []


def test_topology_uses_durable_routes_and_mandatory_return_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    declarations: list[tuple[str, object, object]] = []
    bindings: list[tuple[str, str, str]] = []
    exchange = Exchange()

    @dataclass
    class Queue:
        name: str

        async def bind(self, target: Exchange, routing_key: str = "") -> None:
            bindings.append(
                (self.name, "reviews.dlx" if not routing_key else "reviews", routing_key)
            )

    class Channel:
        async def declare_exchange(
            self, name: str, exchange_type: aio_pika.ExchangeType, *, durable: bool
        ) -> Exchange:
            declarations.append((name, exchange_type, durable))
            return exchange

        async def declare_queue(
            self, name: str, *, durable: bool, arguments: dict[str, object] | None = None
        ) -> Queue:
            declarations.append((name, durable, arguments))
            return Queue(name)

    class Connection:
        async def __aenter__(self) -> Self:
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def channel(self, **kwargs: object) -> Channel:
            assert kwargs == {"publisher_confirms": True, "on_return_raises": True}
            return Channel()

    async def connect(url: str, *, timeout: float) -> Connection:
        assert url == "amqp://guest:guest@rabbitmq/"
        assert timeout == 10.0
        return Connection()

    monkeypatch.setattr("aio_pika.connect_robust", connect)

    async def exercise() -> None:
        async with open_rabbitmq_run_publisher("amqp://guest:guest@rabbitmq/") as publisher:
            await publisher.publish_confirmed(_message())

    asyncio.run(exercise())

    assert declarations == [
        ("reviews.dlx", aio_pika.ExchangeType.FANOUT, True),
        ("reviews.dlq", True, None),
        ("reviews", aio_pika.ExchangeType.DIRECT, True),
        (
            "review.run.fast",
            True,
            {"x-max-priority": 10, "x-dead-letter-exchange": "reviews.dlx"},
        ),
        (
            "review.run.deep",
            True,
            {"x-max-priority": 10, "x-dead-letter-exchange": "reviews.dlx"},
        ),
    ]
    assert bindings == [
        ("reviews.dlq", "reviews.dlx", ""),
        ("review.run.fast", "reviews", "review.run.fast"),
        ("review.run.deep", "reviews", "review.run.deep"),
    ]
    assert exchange.sent[0][2] is True


def test_unroutable_mandatory_return_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    exchange = Exchange(result=RuntimeError("unroutable mandatory return"))
    publisher = RabbitMqRunPublisher(cast(AbstractExchange, exchange))

    with pytest.raises(RuntimeError, match="unroutable mandatory return"):
        asyncio.run(publisher.publish_confirmed(_message()))
    assert exchange.sent[0][2] is True
