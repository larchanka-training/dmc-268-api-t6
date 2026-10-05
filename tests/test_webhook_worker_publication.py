"""The webhook worker replays durable receipts and publishes the Runs they create."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import httpx
import pytest

from app.bootstrap.reviews_api import ReviewsApiResources
from app.common.infrastructure.heartbeat import is_fresh
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.try_enqueue_webhook_run import RunMessagePublisher
from app.webhook_worker import WorkerConfig, compose_worker, run_forever, sweep_once


def _environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://app:app@postgres/app",
        "GITHUB_APP_ID": "17",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
        "RABBITMQ_URL": "amqp://app:app@rabbitmq:5672/",
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


def test_running_worker_beats_even_when_its_sweep_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    heartbeat = tmp_path / "webhook-worker.heartbeat"
    heartbeat.touch()  # left by a previous process: must not count as a beat of this one
    os.utime(heartbeat, (1.0, 1.0))
    environment = _environment()
    # Nothing listens on port 1: the sweep fails and is retried, the process stays alive.
    environment["DATABASE_URL"] = "postgresql+psycopg://app:app@127.0.0.1:1/app"
    environment["WORKER_HEARTBEAT_FILE"] = str(heartbeat)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)

    async def scenario() -> bool:
        task = asyncio.create_task(run_forever())
        for _ in range(100):
            await asyncio.sleep(0.05)
            if task.done() or is_fresh(heartbeat, max_age=5, now=time.time()):
                break
        beating = not task.done() and is_fresh(heartbeat, max_age=5, now=time.time())
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return beating

    assert asyncio.run(scenario()) is True


def test_worker_requires_the_broker_for_run_publication() -> None:
    environment = _environment()
    del environment["RABBITMQ_URL"]
    with pytest.raises(RuntimeError, match="RABBITMQ_URL"):
        WorkerConfig.from_environment(environment)


def test_worker_wires_receipt_projection_with_the_run_publisher_and_app_id() -> None:
    publisher = cast(RunMessagePublisher, object())

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
            assert kwargs["run_publisher"] is publisher
            assert kwargs["app_id"] == 17
            return self.receiver

    resources = Resources()
    receiver = compose_worker(
        cast(ReviewsApiResources, resources),
        cast(httpx.AsyncClient, object()),
        cast(GitHubInstallationAccessTokenProvider, object()),
        WorkerConfig.from_environment(_environment()),
        publisher,
    )

    assert asyncio.run(sweep_once(receiver)) == 1
    assert resources.receiver.calls == 1


def test_worker_returns_zero_when_receipt_sweep_fails() -> None:
    class FailingReceiver:
        async def replay_pending(self) -> int:
            raise RuntimeError("GitHub unavailable")

    assert asyncio.run(sweep_once(cast(ReceiveGitHubDelivery, FailingReceiver()))) == 0
