"""The running webhook worker composes and sweeps confirmed Run publication."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import cast

import httpx
import pytest

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.webhook_worker import WorkerConfig, compose_worker, sweep_once


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://app:app@postgres/app",
        "GITHUB_APP_ID": "17",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
        "RABBITMQ_URL": "amqp://guest:guest@rabbitmq/",
    }


def test_worker_requires_broker_and_numeric_app_id_before_startup() -> None:
    config = WorkerConfig.from_environment(_environment())
    assert config.app_id == 17
    assert config.rabbitmq_url == "amqp://guest:guest@rabbitmq/"

    missing_broker = _environment()
    del missing_broker["RABBITMQ_URL"]
    with pytest.raises(RuntimeError, match="RABBITMQ_URL"):
        WorkerConfig.from_environment(missing_broker)

    invalid_app_id = _environment()
    invalid_app_id["GITHUB_APP_ID"] = "not-a-number"
    with pytest.raises(RuntimeError, match="GITHUB_APP_ID"):
        WorkerConfig.from_environment(invalid_app_id)


def test_worker_encodes_broker_credentials_and_prefers_explicit_url() -> None:
    env = _environment()
    del env["RABBITMQ_URL"]
    env["RABBITMQ_USER"] = "review@team"
    env["RABBITMQ_PASSWORD"] = "slash/secret:#"

    config = WorkerConfig.from_environment(env)

    assert config.rabbitmq_url == "amqp://review%40team:slash%2Fsecret%3A%23@rabbitmq:5672/"

    env["RABBITMQ_URL"] = "amqp://external:sample@example.test:5672/custom"
    overridden = WorkerConfig.from_environment(env)

    assert overridden.rabbitmq_url == "amqp://external:sample@example.test:5672/custom"


def test_worker_wires_one_publisher_to_receipts_and_recovery_sweep() -> None:
    @dataclass
    class Receiver:
        calls: int = 0

        async def replay_pending(self) -> int:
            self.calls += 1
            return 1

    @dataclass
    class Enqueuer:
        calls: int = 0

        async def replay_pending_publications(self) -> int:
            self.calls += 1
            return 2

    class Resources:
        def __init__(self) -> None:
            self.receiver = Receiver()
            self.enqueuer = Enqueuer()
            self.publisher_ids: list[int] = []

        def github_delivery_receiver(self, **kwargs: object) -> Receiver:
            assert kwargs["app_id"] == 17
            assert kwargs["bot_login"] == "reviewer[bot]"
            self.publisher_ids.append(id(kwargs["run_publisher"]))
            return self.receiver

        def webhook_run_trigger(self, **kwargs: object) -> Enqueuer:
            assert kwargs["app_id"] == 17
            self.publisher_ids.append(id(kwargs["publisher"]))
            return self.enqueuer

    resources = Resources()
    publisher = cast(RunMessagePublisher, object())
    receiver, enqueuer = compose_worker(
        cast(ReviewsApiResources, resources),
        cast(httpx.AsyncClient, object()),
        cast(GitHubInstallationAccessTokenProvider, object()),
        publisher,
        WorkerConfig.from_environment(_environment()),
    )

    assert resources.publisher_ids == [id(publisher), id(publisher)]
    assert asyncio.run(sweep_once(receiver, enqueuer)) == (1, 2)
    assert resources.receiver.calls == 1
    assert resources.enqueuer.calls == 1


def test_publication_replay_still_runs_when_receipt_sweep_fails() -> None:
    class FailingReceiver:
        async def replay_pending(self) -> int:
            raise RuntimeError("GitHub unavailable")

    class Recovery:
        async def replay_pending_publications(self) -> int:
            return 1

    assert asyncio.run(
        sweep_once(
            cast(ReceiveGitHubDelivery, FailingReceiver()),
            cast(TryEnqueueWebhookRun, Recovery()),
        )
    ) == (0, 1)
