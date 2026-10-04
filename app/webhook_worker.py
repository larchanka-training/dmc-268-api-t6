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
from pathlib import Path
from typing import NoReturn

import httpx

from app.bootstrap.reviews_api import ReviewsApiResources
from app.common.infrastructure.heartbeat import beat, heartbeat_file, reset
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str
    app_id: int
    private_key: str
    bot_login: str
    github_api_url: str
    heartbeat_file: Path | None = None

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
        return cls(
            database_url=database_url,
            app_id=app_id,
            private_key=required("GITHUB_APP_PRIVATE_KEY"),
            bot_login=required("GITHUB_APP_BOT_LOGIN"),
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            heartbeat_file=heartbeat_file(env),
        )


def compose_worker(
    resources: ReviewsApiResources,
    client: httpx.AsyncClient,
    tokens: GitHubInstallationAccessTokenProvider,
    config: WorkerConfig,
) -> ReceiveGitHubDelivery:
    return resources.github_delivery_receiver(
        client=client,
        token_provider=tokens,
        bot_login=config.bot_login,
    )


async def sweep_once(receiver: ReceiveGitHubDelivery) -> int:
    try:
        projected = await receiver.replay_pending()
    except Exception:
        _LOGGER.exception("GitHub webhook replay sweep failed")
        return 0
    return projected


async def sweep_forever(receiver: ReceiveGitHubDelivery) -> NoReturn:
    while True:
        projected = await sweep_once(receiver)
        await asyncio.sleep(0 if projected == 100 else 30)


async def run_forever() -> None:
    config = WorkerConfig.from_environment(os.environ)
    if config.heartbeat_file is not None:
        reset(config.heartbeat_file)
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
            receiver = compose_worker(resources, client, tokens, config)
            async with asyncio.TaskGroup() as tasks:
                tasks.create_task(sweep_forever(receiver))
                if config.heartbeat_file is not None:
                    tasks.create_task(beat(config.heartbeat_file))
    finally:
        await resources.aclose()


if __name__ == "__main__":
    asyncio.run(run_forever())
