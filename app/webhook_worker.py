"""Replay durable GitHub webhook receipts after process failure or delayed linking.

Run as a separate process with ``uv run python -m app.webhook_worker``.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import quote

import httpx

from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.rabbitmq_run_publisher import (
    open_rabbitmq_run_publisher,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    app_id: int
    private_key: str
    bot_login: str
    rabbitmq_url: str
    github_api_url: str

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> WorkerConfig:
        def required(name: str) -> str:
            value = env.get(name)
            if not value:
                raise RuntimeError(f"{name} is required for the GitHub webhook worker")
            return value

        database_url = required("DATABASE_URL")
        raw_app_id = required("GITHUB_APP_ID")
        try:
            app_id = int(raw_app_id)
        except ValueError as exc:
            raise RuntimeError("GITHUB_APP_ID must be a positive integer") from exc
        if app_id <= 0:
            raise RuntimeError("GITHUB_APP_ID must be a positive integer")
        rabbitmq_url = env.get("RABBITMQ_URL")
        if not rabbitmq_url:
            user = env.get("RABBITMQ_USER")
            password = env.get("RABBITMQ_PASSWORD")
            if not user or not password:
                raise RuntimeError(
                    "RABBITMQ_URL or RABBITMQ_USER and RABBITMQ_PASSWORD are required"
                )
            rabbitmq_url = (
                f"amqp://{quote(user, safe='')}:{quote(password, safe='')}@rabbitmq:5672/"
            )
        return cls(
            database_url=database_url,
            app_id=app_id,
            private_key=required("GITHUB_APP_PRIVATE_KEY"),
            bot_login=required("GITHUB_APP_BOT_LOGIN"),
            rabbitmq_url=rabbitmq_url,
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
        )


def compose_worker(
    resources: ReviewsApiResources,
    client: httpx.AsyncClient,
    tokens: GitHubInstallationAccessTokenProvider,
    publisher: RunMessagePublisher,
    config: WorkerConfig,
) -> tuple[ReceiveGitHubDelivery, TryEnqueueWebhookRun]:
    receiver = resources.github_delivery_receiver(
        client=client,
        token_provider=tokens,
        bot_login=config.bot_login,
        run_publisher=publisher,
        app_id=config.app_id,
    )
    run_trigger = resources.webhook_run_trigger(
        client=client,
        token_provider=tokens,
        app_id=config.app_id,
        publisher=publisher,
    )
    return receiver, run_trigger


async def sweep_once(
    receiver: ReceiveGitHubDelivery, run_trigger: TryEnqueueWebhookRun
) -> tuple[int, int]:
    try:
        projected = await receiver.replay_pending()
    except Exception:
        _LOGGER.exception("GitHub webhook replay sweep failed")
        projected = 0
    try:
        published = await run_trigger.replay_pending_publications()
    except Exception:
        _LOGGER.exception("Pending review.run/v1 publication sweep failed")
        published = 0
    return projected, published


async def run_forever() -> None:
    config = WorkerConfig.from_environment(os.environ)
    resources = ReviewsApiResources.from_database_url(config.database_url)
    try:
        async with httpx.AsyncClient(
            base_url=config.github_api_url,
            timeout=10.0,
        ) as client:
            tokens = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id=str(config.app_id),
                private_key=config.private_key,
                cache=InMemoryInstallationAccessTokenCache(now=time.time),
                now=time.time,
            )
            async with open_rabbitmq_run_publisher(config.rabbitmq_url) as publisher:
                receiver, run_trigger = compose_worker(resources, client, tokens, publisher, config)
                while True:
                    projected, published = await sweep_once(receiver, run_trigger)
                    await asyncio.sleep(0 if projected == 100 or published == 100 else 30)
    finally:
        await resources.aclose()


if __name__ == "__main__":
    asyncio.run(run_forever())
