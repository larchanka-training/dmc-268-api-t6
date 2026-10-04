"""The webhook worker replays durable receipts without an AMQP dependency."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
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
from app.webhook_worker import WorkerConfig, compose_worker, sweep_once


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://app:app@postgres/app",
        "GITHUB_APP_ID": "17",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
    }


def test_worker_requires_github_configuration_and_numeric_app_id_before_startup() -> None:
    config = WorkerConfig.from_environment(_environment())
    assert config.app_id == 17

    invalid_app_id = _environment()
    invalid_app_id["GITHUB_APP_ID"] = "not-a-number"
    with pytest.raises(RuntimeError, match="GITHUB_APP_ID"):
        WorkerConfig.from_environment(invalid_app_id)


def test_worker_writes_a_heartbeat_only_when_configured() -> None:
    assert WorkerConfig.from_environment(_environment()).heartbeat_file is None

    environment = _environment()
    environment["WORKER_HEARTBEAT_FILE"] = "/tmp/webhook-worker.heartbeat"
    config = WorkerConfig.from_environment(environment)

    assert config.heartbeat_file == Path("/tmp/webhook-worker.heartbeat")


def test_worker_wires_receipt_projection_without_a_run_publisher() -> None:
    @dataclass
    class Receiver:
        calls: int = 0

        async def replay_pending(self) -> int:
            self.calls += 1
            return 1

    @dataclass
    class Resources:
        def __init__(self) -> None:
            self.receiver = Receiver()

        def github_delivery_receiver(self, **kwargs: object) -> Receiver:
            assert kwargs["bot_login"] == "reviewer[bot]"
            assert "run_publisher" not in kwargs
            assert "app_id" not in kwargs
            return self.receiver

    resources = Resources()
    receiver = compose_worker(
        cast(ReviewsApiResources, resources),
        cast(httpx.AsyncClient, object()),
        cast(GitHubInstallationAccessTokenProvider, object()),
        WorkerConfig.from_environment(_environment()),
    )

    assert asyncio.run(sweep_once(receiver)) == 1
    assert resources.receiver.calls == 1


def test_worker_returns_zero_when_receipt_sweep_fails() -> None:
    class FailingReceiver:
        async def replay_pending(self) -> int:
            raise RuntimeError("GitHub unavailable")

    assert asyncio.run(sweep_once(cast(ReceiveGitHubDelivery, FailingReceiver()))) == 0
