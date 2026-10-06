"""review.run/v1 and review.publish/v1 on the wire, priorities and DLQ routing (#34)."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID, uuid5

import aio_pika
import pytest
from aio_pika.abc import AbstractExchange, AbstractIncomingMessage
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.modules.reviews.infrastructure.amqp as amqp
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, StoredRunMessage
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunPublicationKind,
)
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.llm.gateway import LlmGateway
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmConfigError
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
class Delivery:
    body: bytes
    message_id: str = "m"
    acked: bool = False
    nacked: list[bool] = field(default_factory=list)

    async def ack(self) -> None:
        self.acked = True

    async def nack(self, requeue: bool = True) -> None:
        self.nacked.append(requeue)


def _deliver(body: bytes, outcome: DeliveryOutcome | Exception) -> tuple[Delivery, list[UUID]]:
    delivery = Delivery(body)
    seen: list[UUID] = []

    async def handler(run_id: UUID) -> DeliveryOutcome:
        seen.append(run_id)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    asyncio.run(amqp.handle_run_delivery(cast(AbstractIncomingMessage, delivery), handler))
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


def test_handler_error_requeues_the_message(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)
    delivery, _ = _deliver(VALID, RuntimeError("database is down"))
    assert delivery.nacked == [True]


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


@pytest.mark.parametrize(
    "model_env",
    [
        {"LLM_MODEL": "mistral-small-4", "LLM_API_KEYS": "k"},
        {
            "LLM_MODEL": "test-model",
            "LLM_BASE_URL": "https://llm.test/v1",
            "LLM_CONTEXT_WINDOW": "100000",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_FALLBACK_API_KEYS": "k",
        },
        {
            "LLM_MODEL": "test-model",
            "LLM_BASE_URL": "https://api.eurouter.ai。/api/v1",
            "LLM_CONTEXT_WINDOW": "100000",
            "LLM_API_KEYS": "k",
        },
    ],
)
def test_worker_rejects_configured_eurouter_route_without_eur_rate(
    model_env: dict[str, str],
) -> None:
    with pytest.raises(LlmConfigError, match="LLM_EUR_TO_USD_RATE"):
        WorkerSettings.from_environment(
            {
                "DATABASE_URL": "postgresql+psycopg://test",
                "RABBITMQ_URL": "amqp://test",
                **model_env,
            }
        )


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
    assert usd_only.llm.eur_to_usd_rate is None
    assert unconfigured.llm is None


def test_worker_accepts_eurouter_route_with_explicit_eur_rate() -> None:
    settings = WorkerSettings.from_environment(
        {
            "DATABASE_URL": "postgresql+psycopg://test",
            "RABBITMQ_URL": "amqp://test",
            "LLM_MODEL": "mistral-small-4",
            "LLM_API_KEYS": "k",
            "LLM_FALLBACK_MODEL": "mistral-small-3.2-24b",
            "LLM_EUR_TO_USD_RATE": "1.1204",
        }
    )

    assert settings.llm is not None
    assert settings.llm.fallback is not None
    assert settings.llm.eur_to_usd_rate == Decimal("1.1204")


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
