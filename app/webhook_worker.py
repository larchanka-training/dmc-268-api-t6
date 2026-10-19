"""Replay durable provider-bound webhook receipts after process failure or delayed linking.

Run as a separate process with ``uv run python -m app.webhook_worker``.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, NoReturn, Protocol, cast

import httpx
from redis.asyncio import Redis

from app.bootstrap.gitlab import gitlab_webhook_replay_resources
from app.bootstrap.reviews_api import ReviewsApiResources
from app.bootstrap.webhook_work_listener import listen_forever
from app.common.infrastructure.heartbeat import beat, heartbeat_file, reset
from app.modules.integrations.gitlab.infrastructure.settings import (
    GitLabRuntimeSettings,
    GitLabSettingsError,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    ReplayBatchResult,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationAccessTokenProvider,
)
from app.modules.integrations.webhooks.infrastructure.redis_installation_token_cache import (
    RedisLike,
    installation_token_cache,
)
from app.modules.repositories.application.rules_status import (
    RULES_REFRESH_BATCH,
    RefreshRulesStatuses,
)
from app.modules.reviews.application.try_enqueue_webhook_run import RunMessagePublisher
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher

_LOGGER = logging.getLogger(__name__)
_MAINTENANCE_INTERVAL_SECONDS = 3600.0


@dataclass(frozen=True)
class WorkerConfig:
    database_url: str = field(repr=False)
    app_id: int | None
    private_key: str | None = field(repr=False)
    bot_login: str | None
    github_api_url: str
    rabbitmq_url: str = field(repr=False)
    heartbeat_file: Path | None = None
    redis_url: str | None = field(default=None, repr=False)
    gitlab: GitLabRuntimeSettings | None = field(default=None, repr=False)

    @property
    def github_configured(self) -> bool:
        return self.app_id is not None and bool(self.private_key) and bool(self.bot_login)

    @classmethod
    def from_environment(cls, env: Mapping[str, str]) -> WorkerConfig:
        def required(name: str) -> str:
            value = env.get(name)
            if not value:
                raise RuntimeError(f"{name} is required for the webhook worker")
            return value

        database_url, rabbitmq_url = required("DATABASE_URL"), required("RABBITMQ_URL")
        names = ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_BOT_LOGIN")
        values = tuple(env.get(name) for name in names)
        app_id = None
        if any(values):
            if not all(values):
                raise RuntimeError(
                    "GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY and GITHUB_APP_BOT_LOGIN "
                    "must be configured together"
                ) from None
            try:
                app_id = int(required("GITHUB_APP_ID"))
            except ValueError:
                raise RuntimeError("GITHUB_APP_ID must be a positive integer") from None
            if app_id <= 0:
                raise RuntimeError("GITHUB_APP_ID must be a positive integer") from None
        try:
            gitlab = GitLabRuntimeSettings.optional_from_environment(env)
        except GitLabSettingsError as error:
            if error.kind == "partial":
                raise RuntimeError(
                    "GITLAB_CREDENTIAL_KEYS, GITLAB_ACTIVE_KEY_ID and GITLAB_ALLOWED_ORIGINS "
                    "must be configured together"
                ) from None
            raise RuntimeError(
                "GitLab webhook replay configuration is invalid; check "
                "GITLAB_CREDENTIAL_KEYS, GITLAB_ACTIVE_KEY_ID, GITLAB_ALLOWED_ORIGINS, "
                "GITLAB_PRIVATE_ORIGINS and SSL_CERT_FILE"
            ) from None
        return cls(
            database_url=database_url,
            app_id=app_id,
            private_key=values[1] or None,
            bot_login=values[2] or None,
            github_api_url=env.get("GITHUB_API_URL", "https://api.github.com"),
            rabbitmq_url=rabbitmq_url,
            heartbeat_file=heartbeat_file(env),
            redis_url=env.get("REDIS_URL") or None,
            gitlab=gitlab,
        )


def compose_worker(
    resources: ReviewsApiResources,
    client: httpx.AsyncClient,
    tokens: GitHubInstallationAccessTokenProvider,
    config: WorkerConfig,
    run_publisher: RunMessagePublisher,
) -> ReceiveGitHubDelivery:
    if not config.github_configured:
        raise RuntimeError("GitHub webhook replay is not configured")
    return resources.github_delivery_receiver(
        client=client,
        token_provider=tokens,
        bot_login=config.bot_login,
        run_publisher=run_publisher,
        app_id=config.app_id,
    )


class WebhookReplayReceiver(Protocol):
    async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult: ...

    async def purge_finished(self) -> int: ...


@dataclass(frozen=True)
class WebhookReplayProvider:
    provider: Literal["github", "gitlab"]
    receiver: WebhookReplayReceiver
    reviver: ReviveDeferredInstallationDeliveries | None = None

    def __post_init__(self) -> None:
        if self.provider not in {"github", "gitlab"} or (
            self.provider != "github" and self.reviver is not None
        ):
            raise ValueError("Invalid webhook replay provider")


async def _scan_once(provider: WebhookReplayProvider) -> ReplayBatchResult | None:
    try:
        return await provider.receiver.replay_batch()
    except Exception as error:
        _LOGGER.error(
            "%s webhook replay sweep failed",
            "GitHub" if provider.provider == "github" else "GitLab",
            extra={"provider": provider.provider, "error_type": type(error).__name__},
        )
        return None


async def sweep_once(receiver: ReceiveGitHubDelivery) -> int:
    """Compatibility completed-handling count; never used to infer scan fullness."""
    result = await _scan_once(WebhookReplayProvider("github", receiver))
    return result.handled_count if result is not None else 0


async def purge_once(
    receiver: WebhookReplayReceiver, *, provider: Literal["github", "gitlab"] = "github"
) -> int:
    try:
        return await receiver.purge_finished()
    except Exception as error:
        _LOGGER.error(
            "%s webhook receipt purge failed",
            "GitHub" if provider == "github" else "GitLab",
            extra={"provider": provider, "error_type": type(error).__name__},
        )
        return 0


async def revive_once(reviver: ReviveDeferredInstallationDeliveries) -> int:
    try:
        return await reviver.execute()
    except Exception as error:
        _LOGGER.error(
            "GitHub webhook deferred-delivery revival failed",
            extra={"provider": "github", "error_type": type(error).__name__},
        )
        return 0


async def refresh_rules_once(refresher: RefreshRulesStatuses) -> int:
    """Answer pending rules status requests (#150); a failure waits for the next pass."""
    try:
        return await refresher.execute()
    except Exception:
        _LOGGER.exception("Rules status refresh sweep failed")
        return 0


async def hourly_maintenance(
    receiver: WebhookReplayReceiver, reviver: ReviveDeferredInstallationDeliveries
) -> None:
    await purge_once(receiver)
    await revive_once(reviver)


async def sweep_providers_forever(
    providers: Sequence[WebhookReplayProvider],
    wake: asyncio.Event,
    *,
    poll_interval: float = 30.0,
    rules: RefreshRulesStatuses | None = None,
) -> NoReturn:
    """One fair bounded round; error cooldowns cannot be bypassed by wake hints."""
    if not math.isfinite(poll_interval) or poll_interval <= 0:
        raise ValueError("poll_interval must be positive and finite")
    providers = tuple(providers)
    cooldowns = [0.0] * len(providers)
    next_maintenance = 0.0
    while True:
        # Once for the whole round: hints arriving during either provider survive.
        wake.clear()
        immediate = False
        for index, provider in enumerate(providers):
            if time.monotonic() < cooldowns[index]:
                continue
            result = await _scan_once(provider)
            if result is None or result.error_count:
                cooldowns[index] = time.monotonic() + poll_interval
            elif result.full:
                immediate = True
        # The same durable hint and poll answer GitHub rules-status requests (#150).
        # GitLab-only startup never constructs this GitHub-backed refresher.
        if rules is not None and await refresh_rules_once(rules) == RULES_REFRESH_BATCH:
            immediate = True
        if time.monotonic() >= next_maintenance:
            for provider in providers:
                await purge_once(provider.receiver, provider=provider.provider)
                if provider.reviver is not None:
                    await revive_once(provider.reviver)
            next_maintenance = time.monotonic() + _MAINTENANCE_INTERVAL_SECONDS
        if immediate:
            await asyncio.sleep(0)
        else:
            now = time.monotonic()
            deadlines = [deadline - now for deadline in cooldowns if deadline > now]
            timeout = min(poll_interval, next_maintenance - now, *deadlines)
            with suppress(TimeoutError):
                await asyncio.wait_for(wake.wait(), timeout=max(0, timeout))


async def sweep_forever(
    receiver: ReceiveGitHubDelivery,
    reviver: ReviveDeferredInstallationDeliveries,
    wake: asyncio.Event,
    *,
    poll_interval: float = 30.0,
    rules: RefreshRulesStatuses | None = None,
) -> NoReturn:
    """Keep the existing GitHub entrypoint on the same provider scheduler."""
    await sweep_providers_forever(
        (WebhookReplayProvider("github", receiver, reviver),),
        wake,
        poll_interval=poll_interval,
        rules=rules,
    )


async def run_forever() -> None:
    config = WorkerConfig.from_environment(os.environ)
    if config.heartbeat_file is not None:
        reset(config.heartbeat_file)
    stack = AsyncExitStack()
    preserve_primary = False

    async def close_owned(close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except Exception as error:
            _LOGGER.error(
                "Webhook worker resource cleanup failed",
                extra={"error_type": type(error).__name__},
            )
            if not preserve_primary:
                raise RuntimeError("Webhook worker resource cleanup failed") from None

    try:
        resources = ReviewsApiResources.from_database_url(config.database_url)
        stack.push_async_callback(close_owned, resources.aclose)
        # Connects on first Run; existing review-worker outbox owns recovery.
        publisher = LazyAmqpPublisher(config.rabbitmq_url)
        stack.push_async_callback(close_owned, publisher.aclose)
        # Shared installation-token cache; the pool connects only on first use.
        redis = Redis.from_url(config.redis_url) if config.redis_url is not None else None
        if redis is not None:
            stack.push_async_callback(close_owned, redis.aclose)
        providers: list[WebhookReplayProvider] = []
        rules: RefreshRulesStatuses | None = None
        if config.github_configured:
            assert config.app_id is not None and config.private_key is not None
            client = httpx.AsyncClient(base_url=config.github_api_url, timeout=10.0)
            stack.push_async_callback(close_owned, client.aclose)
            await client.__aenter__()
            tokens = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id=str(config.app_id),
                private_key=config.private_key,
                cache=installation_token_cache(cast(RedisLike | None, redis)),
                now=time.time,
            )
            receiver = compose_worker(resources, client, tokens, config, publisher)
            reviver = ReviveDeferredInstallationDeliveries(
                uow_factory=resources.github_webhook_receipts, now=lambda: datetime.now(UTC)
            )
            rules = resources.rules_status_refresher(client=client, token_provider=tokens)
            providers.append(WebhookReplayProvider("github", receiver, reviver))
        if config.gitlab is not None:
            gitlab = await stack.enter_async_context(
                gitlab_webhook_replay_resources(
                    resources.session_factory, settings=config.gitlab, run_publisher=publisher
                )
            )
            providers.append(WebhookReplayProvider("gitlab", gitlab.receiver))
        wake = asyncio.Event()
        # TaskGroup exits/awaits listener, scans and heartbeat BEFORE owned resources.
        async with asyncio.TaskGroup() as tasks:
            tasks.create_task(sweep_providers_forever(providers, wake, rules=rules))
            tasks.create_task(listen_forever(config.database_url, wake))
            if config.heartbeat_file is not None:
                tasks.create_task(beat(config.heartbeat_file))

    finally:
        preserve_primary = sys.exception() is not None
        # Pass the primary exception through nested contexts as well as callbacks.
        await stack.__aexit__(*sys.exc_info())


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run_forever())


if __name__ == "__main__":
    main()
