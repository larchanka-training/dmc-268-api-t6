"""review.run/v1 and review.publish/v1 on the wire, priorities and DLQ routing (#34)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid5

import aio_pika
import httpx
import pytest
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.modules.reviews.infrastructure.amqp as amqp
import app.worker as worker_module
from app.bootstrap.llm_gateway import build_gateway as original_build_gateway
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, StoredRunMessage
from app.modules.reviews.application.run_failures import RetryDelays, RunFailure
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.llm.ecb_fx import FxQuoteProvider
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmConfigError, is_eurouter_route
from app.worker import (
    AttemptReviewProvider,
    GitHubAdapters,
    WorkerProcess,
    WorkerSettings,
    attempt_provider_factory,
    compose_worker_process,
    github_adapters,
    run_worker,
)

RUN = UUID("4fabfd21-5acc-4351-a99c-349dffdc00cf")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def pending(**change: Any) -> PendingRunMessage:
    values: dict[str, Any] = {
        "run_id": RUN,
        "workspace_id": UUID(int=1),
        "installation_id": 17,
        "repository_id": UUID(int=2),
        "repository_external_id": 101,
        "repository_full_name": "octo/repo",
        "pr_number": 7,
        "head_sha": "a" * 40,
        "base_sha": "b" * 40,
        "base_ref": "main",
        "engine": "fast",
        "rule_version_id": UUID(int=3),
        "prompt_version_id": UUID(int=4),
        "attempt": 2,
        "requested_at": NOW,
    }
    values.update(change)
    if "trigger" in values:
        return StoredRunMessage(**values)
    return PendingRunMessage(**values)


def test_run_message_passes_the_contract_schema_with_message_id_equal_to_run_id() -> None:
    body = amqp.run_message_body(pending())
    assert body["schema"] == "review.run/v1"
    assert body["message_id"] == body["run_id"] == str(RUN)
    assert body["attempt"] == 2
    assert body["trigger"] == "webhook"
    assert body["requested_at"] == "2026-09-30T12:00:00Z"
    assert amqp.run_message_body(pending(trigger="rerun"))["trigger"] == "rerun"


def test_publish_message_passes_the_contract_schema_with_a_deterministic_id() -> None:
    pointer = ReviewPublishPointer(RUN, "a" * 40, "f" * 64, "REQUEST_CHANGES")
    body = amqp.publish_message_body(pointer)
    assert body["message_id"] == str(uuid5(RUN, f"{'a' * 40}:{'f' * 64}"))
    assert body == amqp.publish_message_body(pointer)


@dataclass
class Exchange:
    published: list[tuple[str, int | None, dict[str, Any]]] = field(default_factory=list)

    async def publish(self, message: aio_pika.Message, routing_key: str) -> None:
        assert message.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        body = json.loads(message.body)
        assert message.message_id == body["message_id"]
        self.published.append((routing_key, message.priority, body))


def test_priorities_and_routing_keys() -> None:
    reviews, retry = Exchange(), Exchange()
    publisher = amqp.AmqpQueuePublisher(
        cast(AbstractExchange, reviews), cast(AbstractExchange, retry)
    )

    async def publish_all() -> None:
        await publisher.publish_confirmed(pending())
        await publisher.publish_confirmed(pending(), kind=RunPublicationKind.CANCELLATION)
        await publisher.publish_retry(pending(trigger="rerun"), "30s")
        await publisher.publish_retry(pending(), "2m")
        await publisher.publish_review(ReviewPublishPointer(RUN, "a" * 40, "f" * 64, "COMMENT"))

    asyncio.run(publish_all())

    assert [(key, priority) for key, priority, _ in reviews.published] == [
        ("review.run.fast", 0),
        ("review.run.fast", 9),
        ("review.publish", 0),
    ]
    assert [(key, priority) for key, priority, _ in retry.published] == [
        ("retry.30s.fast", 9),
        ("retry.2m.fast", 0),
    ]


@dataclass
class FakeExchange:
    published: list[tuple[aio_pika.Message, str]] = field(default_factory=list)

    async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
        self.published.append((message, routing_key))


def fake_publisher(
    exchange: FakeExchange | None = None,
) -> tuple[amqp.AmqpQueuePublisher, FakeExchange]:
    ex = exchange or FakeExchange()
    pub = amqp.AmqpQueuePublisher(
        cast(aio_pika.abc.AbstractExchange, ex),
        cast(aio_pika.abc.AbstractExchange, ex),
    )
    return pub, ex


@dataclass
class Delivery:
    body: bytes
    message_id: str = "m"
    acked: bool = False
    nacked: list[bool] = field(default_factory=list)
    headers: dict[str, Any] = field(default_factory=dict)
    exchange: str = "reviews"
    routing_key: str = "review.run.fast"
    priority: int = 0

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = True) -> None:
        self.nacked.append(requeue)


def _deliver(
    body: bytes,
    outcome: DeliveryOutcome | Exception,
    publisher: amqp.AmqpQueuePublisher | None = None,
) -> tuple[Delivery, list[UUID]]:
    delivery = Delivery(body)
    seen: list[UUID] = []

    async def handler(run_id: UUID) -> DeliveryOutcome:
        seen.append(run_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    pub = publisher or fake_publisher()[0]
    asyncio.run(amqp.handle_run_delivery(cast(AbstractIncomingMessage, delivery), handler, pub))
    return delivery, seen


VALID = json.dumps(amqp.run_message_body(pending())).encode()


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({**amqp.run_message_body(pending()), "schema": "review.run/v2"}).encode(),
        json.dumps({**amqp.run_message_body(pending()), "attempt": 0}).encode(),
        json.dumps({**amqp.run_message_body(pending()), "message_id": str(UUID(int=9))}).encode(),
        b"not json",
    ],
)
def test_unknown_major_or_invalid_message_goes_to_the_dlq_without_processing(
    body: bytes,
) -> None:
    delivery, seen = _deliver(body, DeliveryOutcome.ACK)
    assert seen == []
    assert delivery.nacked == [False] and not delivery.acked


def test_valid_message_is_acked_after_the_handler() -> None:
    delivery, seen = _deliver(VALID, DeliveryOutcome.ACK)
    assert seen == [RUN] and delivery.acked and delivery.nacked == []


def test_exhausted_run_is_dead_lettered() -> None:
    delivery, _ = _deliver(VALID, DeliveryOutcome.DEAD_LETTER)
    assert delivery.nacked == [False] and not delivery.acked


def test_handler_error_republishes_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)
    amqp._clear_delivery_attempts("m")
    pub, exchange = fake_publisher()
    delivery, _ = _deliver(VALID, RuntimeError("database is down"), publisher=pub)
    assert delivery.acked is True and delivery.nacked == []
    assert len(exchange.published) == 1
    msg, rk = exchange.published[0]
    assert msg.headers["x-attempt"] == 1
    assert rk == "review.run.fast"


def test_handler_repeated_errors_route_to_dlq_with_log(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)
    amqp._clear_delivery_attempts("m")
    pub, exchange = fake_publisher()

    # Attempt 1: republished
    d1, _ = _deliver(VALID, RuntimeError("unexpected error 1"), publisher=pub)
    assert d1.acked is True and d1.nacked == []
    assert len(exchange.published) == 1

    # Attempt 2: republished
    d2, _ = _deliver(VALID, RuntimeError("unexpected error 2"), publisher=pub)
    assert d2.acked is True and d2.nacked == []
    assert len(exchange.published) == 2

    # Attempt 3: reached max (3) -> DLQ (nack requeue=False) with error log
    with caplog.at_level(logging.ERROR):
        d3, _ = _deliver(VALID, RuntimeError("unexpected error 3"), publisher=pub)
    assert d3.nacked == [False] and not d3.acked
    assert "exceeded maximum unexpected retry attempts (3); routing to DLQ" in caplog.text


def test_handler_repeated_errors_route_to_dlq_across_worker_restarts(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("worker crash simulation")

    pub, exchange = fake_publisher()

    # Worker 1 gets fresh message without broker retry headers -> fails ->
    # republishes copy with x-attempt=1 and ACKs original
    amqp._clear_delivery_attempts("m")
    d1 = Delivery(VALID, message_id="m")
    asyncio.run(amqp.handle_run_delivery(cast(AbstractIncomingMessage, d1), failing_handler, pub))
    assert d1.acked is True and d1.nacked == []
    assert len(exchange.published) == 1
    pub1, rk1 = exchange.published[0]
    assert pub1.headers["x-attempt"] == 1
    assert "x-delivery-count" not in pub1.headers
    assert rk1 == "review.run.fast"

    # Worker 2 restarts (empty process state), gets redelivery
    # instantiated from the republished message -> fails ->
    # republishes copy with x-attempt=2 and ACKs d2
    amqp._unexpected_delivery_attempts.clear()
    d2 = Delivery(
        body=pub1.body,
        message_id="m",
        headers=pub1.headers,
    )
    asyncio.run(amqp.handle_run_delivery(cast(AbstractIncomingMessage, d2), failing_handler, pub))
    assert d2.acked is True and d2.nacked == []
    assert len(exchange.published) == 2
    pub2, _ = exchange.published[1]
    assert pub2.headers["x-attempt"] == 2
    assert "x-delivery-count" not in pub2.headers

    # Worker 3 restarts (empty process state), gets redelivery
    # instantiated from the second republished message -> reaches attempt 3 -> DLQ!
    amqp._unexpected_delivery_attempts.clear()
    d3 = Delivery(
        body=pub2.body,
        message_id="m",
        headers=pub2.headers,
    )
    with caplog.at_level(logging.ERROR):
        asyncio.run(
            amqp.handle_run_delivery(cast(AbstractIncomingMessage, d3), failing_handler, pub)
        )
    assert d3.nacked == [False] and not d3.acked
    assert len(exchange.published) == 2
    assert "exceeded maximum unexpected retry attempts (3); routing to DLQ" in caplog.text


def test_handler_x_death_headers_do_not_deplete_unexpected_retries(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("retry error")

    # Message returned from planned retry queue has x-death count=1.
    # In the architecture (SD §6.8, PIPELINE_SPEC §4.1), x-death is diagnostic
    # and does not count toward unexpected crash retries.
    amqp._clear_delivery_attempts("m1")
    d1 = Delivery(VALID, message_id="m1", headers={"x-death": [{"count": 1}]})
    incoming1 = cast(AbstractIncomingMessage, d1)
    pub, exchange = fake_publisher()
    asyncio.run(amqp.handle_run_delivery(incoming1, failing_handler, pub))
    assert d1.acked is True and not d1.nacked
    assert len(exchange.published) == 1
    assert exchange.published[0][0].headers["x-attempt"] == 1


def test_corrupted_retry_headers_log_warning(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("corrupted header error")

    amqp._clear_delivery_attempts("bad")
    delivery = Delivery(VALID, message_id="bad", headers={"x-attempt": "not-an-int"})
    pub, _ = fake_publisher()
    with caplog.at_level(logging.WARNING):
        asyncio.run(
            amqp.handle_run_delivery(cast(AbstractIncomingMessage, delivery), failing_handler, pub)
        )

    assert "Invalid retry header x-attempt='not-an-int'" in caplog.text


def test_handler_routes_to_dlq_when_broker_delivery_count_reaches_limit(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Message redelivered 2 times previously per broker x-attempt header -> this attempt is 3
    delivery = Delivery(VALID, headers={"x-attempt": 2})
    amqp._clear_delivery_attempts("m")
    pub, _ = fake_publisher()

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("broken run")

    with caplog.at_level(logging.ERROR):
        incoming = cast(AbstractIncomingMessage, delivery)
        asyncio.run(amqp.handle_run_delivery(incoming, failing_handler, pub))
    assert delivery.nacked == [False] and not delivery.acked
    assert "exceeded maximum unexpected retry attempts (3); routing to DLQ" in caplog.text


def test_republish_failure_falls_back_to_nack_requeue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)

    class FailingExchange:
        async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
            raise RuntimeError("broker publish error")

    publisher = amqp.AmqpQueuePublisher(
        cast(aio_pika.abc.AbstractExchange, FailingExchange()),
        cast(aio_pika.abc.AbstractExchange, FailingExchange()),
    )
    delivery = Delivery(VALID, message_id="err-pub")

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("handler failed")

    asyncio.run(
        amqp.handle_run_delivery(
            cast(AbstractIncomingMessage, delivery), failing_handler, publisher=publisher
        )
    )

    assert delivery.acked is False
    assert delivery.nacked == [True]


def test_republish_ack_failure_logs_exception_and_does_not_nack(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)

    pub, exchange = fake_publisher()

    class AckFailingDelivery(Delivery):
        async def ack(self) -> None:
            raise RuntimeError("ack failed")

    delivery = AckFailingDelivery(VALID, message_id="err-ack")

    async def failing_handler(run_id: UUID) -> DeliveryOutcome:
        raise RuntimeError("handler failed")

    with caplog.at_level(logging.ERROR):
        asyncio.run(
            amqp.handle_run_delivery(
                cast(AbstractIncomingMessage, delivery), failing_handler, publisher=pub
            )
        )

    assert len(exchange.published) == 1
    assert delivery.nacked == []
    assert "Failed to ack message err-ack after republish" in caplog.text


def test_amqp_channels_configures_publisher_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_channel_kwargs: list[dict[str, Any]] = []

    class FakeRobustQueue:
        async def bind(self, *a: Any, **kw: Any) -> Any:
            return None

    class FakeRobustChannel:
        async def declare_exchange(self, *a: Any, **kw: Any) -> Any:
            return FakeExchange()

        async def declare_queue(self, *a: Any, **kw: Any) -> Any:
            return FakeRobustQueue()

        async def queue_bind(self, *a: Any, **kw: Any) -> Any:
            return None

        async def get_exchange(self, *a: Any, **kw: Any) -> Any:
            return FakeExchange()

    class FakeRobustConn:
        async def channel(self, **kwargs: Any) -> Any:
            captured_channel_kwargs.append(kwargs)
            return FakeRobustChannel()

        async def close(self) -> None:
            pass

    async def fake_connect_robust(url: str) -> Any:
        return FakeRobustConn()

    monkeypatch.setattr(aio_pika, "connect_robust", fake_connect_robust)

    async def run() -> None:
        async with amqp.amqp_channels("amqp://guest:guest@localhost/", RetryDelays()):
            pass

    asyncio.run(run())
    assert captured_channel_kwargs == [{"publisher_confirms": True, "on_return_raises": True}]


class _Queue:
    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        raise AssertionError("not published in this test")

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None:
        raise AssertionError("not published in this test")

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        raise AssertionError("not published in this test")


def test_worker_without_github_app_warns_and_disables_sweep_and_publication(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = WorkerSettings.from_environment(
        {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}
    )
    factory = cast(async_sessionmaker[AsyncSession], object())
    with caplog.at_level(logging.WARNING):
        github = github_adapters(settings, None, factory)
    process = compose_worker_process(
        settings=settings, session_factory=factory, queue=_Queue(), github=github
    )

    assert github is None
    assert "GITHUB_APP_ID or GITHUB_APP_PRIVATE_KEY is not set" in caplog.text
    assert process.sweep is None


def test_worker_settings_require_database_and_broker() -> None:
    with pytest.raises(RuntimeError, match="RABBITMQ_URL"):
        WorkerSettings.from_environment({"DATABASE_URL": "postgresql+psycopg://test"})


def test_worker_leader_tick_runs_the_sweep_then_the_outbox_replay_every_thirty_seconds() -> None:
    calls: list[str] = []

    class Sweep:
        async def execute(self, *, limit: int = 100) -> int:
            calls.append("sweep")
            return 0

    class Enqueue:
        async def replay_pending_publications(self, *, limit: int = 100) -> int:
            calls.append("replay")
            return 0

    process = WorkerProcess(
        handle_run=cast(Any, None),
        publish_review=cast(Any, None),
        enqueue=cast(Any, Enqueue()),
        sweep=cast(Any, Sweep()),
    )
    asyncio.run(process.leader_tick())
    without_app = WorkerProcess(
        handle_run=cast(Any, None),
        publish_review=cast(Any, None),
        enqueue=cast(Any, Enqueue()),
        sweep=None,
    )
    asyncio.run(without_app.leader_tick())

    assert calls == ["sweep", "replay", "replay"]
    assert inspect.signature(run_worker).parameters["leader_period"].default == 30.0


LLM_ENV = {
    "DATABASE_URL": "postgresql+psycopg://test",
    "RABBITMQ_URL": "amqp://test",
    "LLM_MODEL": "test-model",
    "LLM_BASE_URL": "https://llm.test/v1",
    "LLM_API_KEYS": "k",
    "LLM_CONTEXT_WINDOW": "100000",
}


def test_worker_settings_read_llm_and_reject_a_partial_llm_config() -> None:
    settings = WorkerSettings.from_environment(LLM_ENV)
    assert settings.llm is not None and settings.llm.primary.model == "test-model"
    assert (
        WorkerSettings.from_environment(
            {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}
        ).llm
        is None
    )
    with pytest.raises(LlmConfigError):
        WorkerSettings.from_environment({**LLM_ENV, "LLM_BASE_URL": ""})


def test_worker_with_an_empty_fallback_model_has_no_fallback_route() -> None:
    # .env.example ships `LLM_FALLBACK_MODEL=` for a local run.
    settings = WorkerSettings.from_environment({**LLM_ENV, "LLM_FALLBACK_MODEL": ""})

    assert settings.llm is not None
    assert settings.llm.fallback is None


@pytest.mark.parametrize(
    ("model_env", "eurouter"),
    [
        ({"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "k"}, "primary"),
        (
            {
                "LLM_MODEL": "test-model",
                "LLM_BASE_URL": "https://llm.test/v1",
                "LLM_CONTEXT_WINDOW": "100000",
                "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
                "LLM_FALLBACK_API_KEYS": "k",
            },
            "fallback",
        ),
        (
            {
                "LLM_MODEL": "test-model",
                "LLM_BASE_URL": "https://api.eurouter.ai。/api/v1",
                "LLM_CONTEXT_WINDOW": "100000",
                "LLM_API_KEYS": "k",
            },
            "primary",
        ),
    ],
)
def test_worker_starts_with_configured_eurouter_route_without_eur_rate(
    model_env: dict[str, str], eurouter: str
) -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://test",
            "RABBITMQ_URL": "amqp://test",
            **model_env,
        }
    )
    assert settings.llm is not None
    # the gateway's own runtime check decides which calls need an ECB quote
    route = settings.llm.primary if eurouter == "primary" else settings.llm.fallback
    assert route is not None and is_eurouter_route(route)


def test_worker_startup_creates_fx_provider_without_fetching_ecb(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://test:test@localhost:5432/test",
            "RABBITMQ_URL": "amqp://test:test@localhost:5672/",
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "sk-test",
        }
    )
    fx_requests: list[httpx.Request] = []
    llm_requests: list[httpx.Request] = []
    fx_provider: FxQuoteProvider | None = None

    def capture_gateway(*args: Any, **kwargs: Any) -> LlmGateway:
        nonlocal fx_provider
        fx_provider = kwargs.get("fx_provider")
        return original_build_gateway(*args, **kwargs)

    def respond_llm(request: httpx.Request) -> httpx.Response:
        llm_requests.append(request)
        raise AssertionError("LLM request during worker startup")

    def respond_fx(request: httpx.Request) -> httpx.Response:
        fx_requests.append(request)
        assert "authorization" not in request.headers
        return httpx.Response(
            200,
            text=(
                "KEY,FREQ,CURRENCY,CURRENCY_DENOM,EXR_TYPE,EXR_SUFFIX,TIME_PERIOD,OBS_VALUE\n"
                f"EXR.D.USD.EUR.SP00.A,D,USD,EUR,SP00,A,{datetime.now(UTC).date()},1.1204\n"
            ),
        )

    class FxTransport(httpx.MockTransport):
        closed = False

        async def aclose(self) -> None:
            self.closed = True
            await super().aclose()

    fx_transport = FxTransport(respond_fx)

    class BrokerReached(Exception):
        pass

    @asynccontextmanager
    async def stop_at_broker(*_: object) -> AsyncIterator[None]:
        assert fx_requests == []
        assert fx_provider is not None
        quote = (await fx_provider.get_quote()).quote
        assert quote is not None and quote.rate_usd_per_eur == Decimal("1.1204")
        raise BrokerReached
        yield

    monkeypatch.setattr(worker_module, "build_gateway", capture_gateway)
    monkeypatch.setattr(worker_module, "amqp_channels", stop_at_broker)
    with pytest.raises(BrokerReached):
        asyncio.run(
            run_worker(
                settings,
                llm_transport=httpx.MockTransport(respond_llm),
                fx_transport=fx_transport,
            )
        )
    assert llm_requests == []
    assert len(fx_requests) == 1
    assert fx_requests[0].url.host == "data-api.ecb.europa.eu"
    assert fx_transport.closed


def test_worker_allows_usd_only_route_and_no_model_without_eur_rate() -> None:
    base_env = {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}

    usd_only = WorkerSettings.from_environment(
        {
            **base_env,
            "LLM_MODEL": "mistral-small-4",
            "LLM_BASE_URL": "https://usd-only.test/v1",
            "LLM_API_KEYS": "k",
        }
    )
    unconfigured = WorkerSettings.from_environment(
        {**base_env, "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b"}
    )

    assert usd_only.llm is not None
    assert usd_only.llm.primary.base_url == "https://usd-only.test/v1"
    assert unconfigured.llm is None


def test_worker_ignores_invalid_legacy_eur_rate_at_startup() -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://test",
            "RABBITMQ_URL": "amqp://test",
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "k",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "invalid-legacy-value",
        }
    )

    assert settings.llm is not None
    assert settings.llm.fallback is not None


def _claimed() -> ClaimedAttempt:
    return ClaimedAttempt(
        run_id=RUN,
        workspace_id=UUID(int=1),
        attempt=2,
        engine="fast",
        deadline=NOW,
        prompt_version_id=UUID(int=4),
        rule_version_id=UUID(int=3),
        worker_id="w",
    )


def test_production_factory_binds_gateway_models_to_the_attempt() -> None:
    sources: list[UUID] = []
    source = cast(GitHubRunSource, object())

    def run_source(run_id: UUID) -> GitHubRunSource:
        sources.append(run_id)
        return source

    github = GitHubAdapters(
        vcs=cast(Any, None),
        check_runs=cast(Any, None),
        reviews=cast(Any, None),
        eligibility=cast(Any, None),
        run_source=run_source,
    )
    gateway = cast(LlmGateway, object())

    provider = attempt_provider_factory(gateway, github)(_claimed())

    assert isinstance(provider, AttemptReviewProvider)
    review = provider._review
    assert isinstance(review, GatewayReviewModel)
    assert isinstance(provider._conventions, GatewayConventionsModel)
    assert review._run.deadline == NOW and review._run.attempt == 2
    assert sources == [RUN]


def test_factory_without_llm_fails_the_model_call_and_names_the_missing_config() -> None:
    github = GitHubAdapters(
        vcs=cast(Any, None),
        check_runs=cast(Any, None),
        reviews=cast(Any, None),
        eligibility=cast(Any, None),
        run_source=lambda run_id: cast(GitHubRunSource, object()),
    )
    provider = attempt_provider_factory(None, github)(_claimed())

    with pytest.raises(RunFailure, match="not configured") as raised:
        asyncio.run(provider.draft_review(context=cast(Any, None)))
    assert raised.value.error_code == "llm_unavailable"
