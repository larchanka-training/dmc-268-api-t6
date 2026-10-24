"""Production worker startup selects a token cache without contacting infrastructure."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import NoReturn, cast

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from app import webhook_worker, worker
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
    InstallationAccessTokenCache,
)
from app.modules.integrations.webhooks.infrastructure.redis_installation_token_cache import (
    RedisInstallationAccessTokenCache,
)

_REDIS_URL = "redis://cache.example.test:6379/4"
_BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://unused/unused",
    "RABBITMQ_URL": "amqp://unused/unused",
    "GITHUB_APP_ID": "123",
    "GITHUB_APP_PRIVATE_KEY": "unused-test-key",
    "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
    "GITHUB_API_URL": "https://github.example.test",
}


class StartupCaptured(Exception):
    """Stop at the composition boundary, before consumers or background tasks start."""


class Closeable:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class RecordingRedis(Closeable):
    def __init__(self) -> None:
        super().__init__()
        self.read_keys: list[str] = []

    async def get(self, name: str) -> None:
        self.read_keys.append(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> None:
        raise AssertionError("startup must not write installation tokens")


class RedisFactory:
    def __init__(self) -> None:
        self.urls: list[str | None] = []
        self.client = RecordingRedis()

    def from_url(self, url: str | None, **options: object) -> RecordingRedis:
        # Accept absent URLs too, so unconditional construction fails the contract
        # assertion below rather than an unrelated redis-py URL parser error.
        self.urls.append(url)
        return self.client


class ProviderCapture:
    """Forward the public constructor unchanged; observe its cache argument."""

    def __init__(self) -> None:
        self.providers: list[GitHubAppInstallationAccessTokenProvider] = []
        self.caches: list[InstallationAccessTokenCache] = []
        self.clients: list[httpx.AsyncClient] = []

    def __call__(
        self,
        *,
        client: httpx.AsyncClient,
        app_id: str,
        private_key: str,
        cache: InstallationAccessTokenCache,
        now: Callable[[], float],
    ) -> GitHubAppInstallationAccessTokenProvider:
        provider = GitHubAppInstallationAccessTokenProvider(
            client=client, app_id=app_id, private_key=private_key, cache=cache, now=now
        )
        self.providers.append(provider)
        self.caches.append(cache)
        self.clients.append(client)
        return provider


def environment(redis_url: str | None) -> dict[str, str]:
    env = dict(_BASE_ENV)
    if redis_url is not None:
        env["REDIS_URL"] = redis_url
    return env


def assert_cache_selection(
    capture: ProviderCapture, redis: RedisFactory, redis_url: str | None
) -> None:
    assert len(capture.providers) == 1
    assert len(capture.clients) == 1
    assert capture.clients[0].is_closed
    # Literal constructor counts catch accidental Redis creation even when another
    # regression would discard that Redis client during cache composition.
    if redis_url:
        assert redis.urls == [_REDIS_URL]
        assert isinstance(capture.caches[0], RedisInstallationAccessTokenCache)
        # Exercise the captured public cache: it must use the exact factory client.
        asyncio.run(capture.caches[0].get(91))
        assert redis.client.read_keys == ["token:91"]
        assert redis.client.closed
    else:
        assert redis.urls == []
        assert isinstance(capture.caches[0], InMemoryInstallationAccessTokenCache)
        assert not redis.client.closed


@pytest.mark.parametrize("redis_url", [_REDIS_URL, None, ""], ids=["redis", "absent", "empty"])
def test_review_worker_startup_selects_installation_token_cache(
    monkeypatch: pytest.MonkeyPatch, redis_url: str | None
) -> None:
    settings = worker.WorkerSettings.from_environment(environment(redis_url))
    capture, redis, engine = ProviderCapture(), RedisFactory(), Closeable()
    released: list[str] = []

    def create_engine(*args: object, **kwargs: object) -> AsyncEngine:
        return cast(AsyncEngine, SimpleNamespace(dispose=engine.aclose))

    @asynccontextmanager
    async def channels(*args: object, **kwargs: object) -> AsyncIterator[SimpleNamespace]:
        try:
            yield SimpleNamespace(connection=object(), publisher=object())
        finally:
            released.append("amqp")

    @asynccontextmanager
    async def index(*args: object, **kwargs: object) -> AsyncIterator[None]:
        try:
            yield None
        finally:
            released.append("index")

    def stop_at_composition(**kwargs: object) -> NoReturn:
        assert isinstance(kwargs["github"], worker.GitHubAdapters)
        assert len(capture.providers) == 1
        raise StartupCaptured

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("startup must not call GitHub")

    monkeypatch.setattr(worker, "create_async_engine", create_engine)
    monkeypatch.setattr(worker, "amqp_channels", channels)
    monkeypatch.setattr(worker, "index_consumer", index)
    monkeypatch.setattr(worker, "compose_worker_process", stop_at_composition)
    monkeypatch.setattr(worker, "Redis", redis)
    monkeypatch.setattr(worker, "GitHubAppInstallationAccessTokenProvider", capture)
    with pytest.raises(StartupCaptured):
        asyncio.run(
            worker.run_worker(settings, github_transport=httpx.MockTransport(unexpected_request))
        )

    assert engine.closed
    assert sorted(released) == ["amqp", "index"]
    assert_cache_selection(capture, redis, redis_url)


@pytest.mark.parametrize("redis_url", [_REDIS_URL, None, ""], ids=["redis", "absent", "empty"])
def test_webhook_worker_startup_selects_installation_token_cache(
    monkeypatch: pytest.MonkeyPatch, redis_url: str | None
) -> None:
    # Use a complete isolated environment so ambient heartbeat/Redis/LLM settings
    # cannot create external resources or background tasks.
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    for name, value in environment(redis_url).items():
        monkeypatch.setenv(name, value)
    capture, redis = ProviderCapture(), RedisFactory()
    expected_resources, publisher = Closeable(), Closeable()

    def stop_at_composition(
        resources: object,
        client: httpx.AsyncClient,
        tokens: object,
        config: webhook_worker.WorkerConfig,
        run_publisher: object,
    ) -> NoReturn:
        assert resources is expected_resources
        assert tokens is capture.providers[0]
        assert run_publisher is publisher
        raise StartupCaptured

    def unexpected_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError("startup must not call GitHub")

    def client(*, base_url: str, timeout: float, **options: object) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=base_url, timeout=timeout, transport=httpx.MockTransport(unexpected_request)
        )

    monkeypatch.setattr(webhook_worker, "httpx", SimpleNamespace(AsyncClient=client))
    monkeypatch.setattr(
        webhook_worker,
        "ReviewsApiResources",
        SimpleNamespace(from_database_url=lambda url: expected_resources),
    )
    monkeypatch.setattr(webhook_worker, "LazyAmqpPublisher", lambda url: publisher)
    monkeypatch.setattr(webhook_worker, "compose_worker", stop_at_composition)
    monkeypatch.setattr(webhook_worker, "Redis", redis)
    monkeypatch.setattr(webhook_worker, "GitHubAppInstallationAccessTokenProvider", capture)
    with pytest.raises(StartupCaptured):
        asyncio.run(webhook_worker.run_forever())

    assert expected_resources.closed
    assert publisher.closed
    assert_cache_selection(capture, redis, redis_url)
