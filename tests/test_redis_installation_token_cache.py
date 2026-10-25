"""Installation tokens in Redis: one token per installation for every process (#62)."""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
    InstallationAccessTokenCache,
)
from app.modules.integrations.webhooks.infrastructure.redis_installation_token_cache import (
    RedisInstallationAccessTokenCache,
    installation_token_cache,
)
from app.webhook_worker import WorkerConfig

_NOW = 1_800_000_000
_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FakeRedis:
    values: dict[str, str | bytes] = field(default_factory=dict)
    lifetimes: dict[str, int | None] = field(default_factory=dict)
    broken: bool = False
    write_broken: bool = False

    async def get(self, name: str) -> bytes | str | None:
        if self.broken:
            raise ConnectionError("redis is down")
        return self.values.get(name)

    async def set(self, name: str, value: str, ex: int | None = None) -> object:
        if self.broken or self.write_broken:
            # A real client error may quote the command; the cache must not log it.
            raise ConnectionError(f"redis is down while writing {value}")
        self.values[name] = value
        self.lifetimes[name] = ex
        return True


@dataclass
class _GitHub:
    """The token endpoint: every POST mints the next token of the installation."""

    posts: list[int] = field(default_factory=list)
    lifetime_seconds: int = 3600
    now: Callable[[], float] = field(default=lambda: _NOW, repr=False)

    def handler(self, request: httpx.Request) -> httpx.Response:
        installation = int(request.url.path.split("/")[3])
        self.posts.append(installation)
        return httpx.Response(
            201,
            json={
                "token": f"secret-{installation}-{len(self.posts)}",
                "expires_at": datetime.fromtimestamp(
                    self.now() + self.lifetime_seconds, UTC
                ).isoformat(),
            },
        )


@asynccontextmanager
async def _process(
    github: _GitHub,
    cache: InstallationAccessTokenCache,
    *,
    now: Callable[[], float] = lambda: _NOW,
) -> AsyncIterator[GitHubAppInstallationAccessTokenProvider]:
    """Own one process's provider client for the duration of an async scenario."""
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(github.handler), base_url="https://api.github.com"
    ) as client:
        yield GitHubAppInstallationAccessTokenProvider(
            client=client,
            app_id="123",
            private_key="test-private-key",
            jwt_encoder=lambda claims, private_key: "app-jwt",
            cache=cache,
            now=now,
        )


def _redis_cache(redis: FakeRedis) -> RedisInstallationAccessTokenCache:
    return RedisInstallationAccessTokenCache(redis, now=lambda: _NOW)


def test_two_processes_with_one_redis_share_the_token_of_an_installation() -> None:
    redis = FakeRedis()
    github = _GitHub()

    async def scenario() -> list[str]:
        async with (
            _process(github, _redis_cache(redis)) as worker,
            _process(github, _redis_cache(redis)) as webhook_worker,
        ):
            return [
                await worker.get_installation_access_token(17),
                await webhook_worker.get_installation_access_token(17),
                await worker.get_installation_access_token(18),
                await webhook_worker.get_installation_access_token(18),
            ]

    tokens = asyncio.run(scenario())

    shared_tokens_match = tokens == ["secret-17-1", "secret-17-1", "secret-18-2", "secret-18-2"]
    assert shared_tokens_match, "both processes must receive the shared installation credentials"
    assert github.posts == [17, 18]
    stored_tokens_match = redis.values == {"token:17": "secret-17-1", "token:18": "secret-18-2"}
    assert stored_tokens_match, "Redis must store each issued installation credential"


def test_processes_without_redis_keep_separate_in_memory_tokens() -> None:
    github = _GitHub()
    first_cache = installation_token_cache(None, now=lambda: _NOW)
    second_cache = installation_token_cache(None, now=lambda: _NOW)

    async def scenario() -> list[str]:
        async with (
            _process(github, first_cache) as first,
            _process(github, second_cache) as second,
        ):
            return [
                await first.get_installation_access_token(17),
                await first.get_installation_access_token(17),
                await second.get_installation_access_token(17),
            ]

    tokens = asyncio.run(scenario())

    assert isinstance(first_cache, InMemoryInstallationAccessTokenCache)
    assert isinstance(second_cache, InMemoryInstallationAccessTokenCache)
    separate_tokens_match = tokens == ["secret-17-1", "secret-17-1", "secret-17-2"]
    assert separate_tokens_match, "process-local caches must keep separate credentials"
    assert github.posts == [17, 17]


def test_redis_is_chosen_when_it_is_configured() -> None:
    cache = installation_token_cache(FakeRedis(), now=lambda: _NOW)

    assert isinstance(cache, RedisInstallationAccessTokenCache)


def test_the_key_expires_at_the_token_expiry_minus_the_safety_skew() -> None:
    redis = FakeRedis()
    cache = _redis_cache(redis)

    async def scenario() -> None:
        await cache.set(17, "secret", datetime.fromtimestamp(_NOW + 3600, UTC))
        # A fractional lifetime is rounded down, never up.
        await cache.set(18, "secret", datetime.fromtimestamp(_NOW + 3600.9, UTC))

    asyncio.run(scenario())

    assert redis.lifetimes == {"token:17": 3540, "token:18": 3540}


def test_a_token_inside_the_safety_skew_is_not_written_to_redis() -> None:
    redis = FakeRedis()
    cache = _redis_cache(redis)

    async def scenario() -> str | None:
        await cache.set(17, "secret", datetime.fromtimestamp(_NOW + 60, UTC))
        return await cache.get(17)

    assert asyncio.run(scenario()) is None
    assert redis.values == {}


def test_a_token_stored_as_bytes_is_read_as_text() -> None:
    redis = FakeRedis(values={"token:17": b"secret"})

    assert asyncio.run(_redis_cache(redis).get(17)) == "secret"


def test_unreachable_redis_leaves_the_process_on_its_own_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis = FakeRedis(broken=True)
    github = _GitHub()

    async def scenario() -> list[str]:
        async with _process(github, _redis_cache(redis)) as process:
            return [
                await process.get_installation_access_token(17),
                await process.get_installation_access_token(17),
            ]

    with caplog.at_level(logging.WARNING):
        tokens = asyncio.run(scenario())

    # Minted once: the failed reads answer from the memory of the process.
    local_tokens_match = tokens == ["secret-17-1", "secret-17-1"]
    assert local_tokens_match, "unreachable Redis must preserve the local credential"
    assert github.posts == [17]
    assert "Installation token cache read failed installation=17" in caplog.text
    assert "Installation token cache write failed installation=17" in caplog.text
    credential_omitted = "secret-17-1" not in caplog.text
    assert credential_omitted, "logs must omit installation credentials"
    assert all(record.exc_info is None for record in caplog.records)


def test_redis_address_is_optional_for_the_webhook_worker() -> None:
    env = {
        "DATABASE_URL": "postgresql+psycopg://app:app@postgres/app",
        "GITHUB_APP_ID": "17",
        "GITHUB_APP_PRIVATE_KEY": "private-key",
        "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
        "RABBITMQ_URL": "amqp://app:app@rabbitmq:5672/",
    }

    assert WorkerConfig.from_environment(env).redis_url is None
    assert WorkerConfig.from_environment({**env, "REDIS_URL": ""}).redis_url is None
    configured = WorkerConfig.from_environment({**env, "REDIS_URL": "redis://r:6379/0"})
    assert configured.redis_url == "redis://r:6379/0"


@pytest.mark.parametrize("compose_file", ["docker-compose.yml", "deploy/compose/staging.yml"])
@pytest.mark.parametrize("service", ["worker", "webhook-worker"])
def test_both_workers_receive_the_redis_address(compose_file: str, service: str) -> None:
    compose = (_ROOT / compose_file).read_text(encoding="utf-8")
    block = re.search(rf"^  {service}:\n((?:    .*\n|\n)*)", compose, re.MULTILINE)

    assert block is not None
    assert "      REDIS_URL: redis://" in block.group(1)


def test_clean_redis_misses_reuse_local_token_when_writes_fail(
    caplog: pytest.LogCaptureFixture,
) -> None:
    redis, github = FakeRedis(write_broken=True), _GitHub()

    async def scenario() -> list[str]:
        async with _process(github, _redis_cache(redis)) as provider:
            return [await provider.get_installation_access_token(17) for _ in range(3)]

    with caplog.at_level(logging.WARNING):
        tokens = asyncio.run(scenario())

    assert github.posts == [17]
    reused_one_token = len(set(tokens)) == 1
    assert reused_one_token, "valid local token must serve subsequent requests"
    assert len(caplog.records) == 1
    warning_is_sanitized = caplog.records[0].getMessage() == (
        "Installation token cache write failed installation=17 error=ConnectionError"
    )
    assert warning_is_sanitized, "cache warning must report only operation, installation and type"
    # The fake exception quotes the token. Neither rendered logs nor saved logging
    # metadata may preserve it, and no traceback may expose the Redis command.
    token_absent_from_logs = all(token not in caplog.text for token in tokens)
    token_absent_from_metadata = all(
        token not in repr(record.__dict__) for token in tokens for record in caplog.records
    )
    assert token_absent_from_logs, "logs must omit installation credentials"
    assert token_absent_from_metadata, "logging metadata must omit installation credentials"
    assert all(record.exc_info is None and record.exc_text is None for record in caplog.records)


def test_a_cold_clean_redis_miss_issues_a_token() -> None:
    redis, github = FakeRedis(), _GitHub()

    async def scenario() -> str:
        async with _process(github, _redis_cache(redis)) as provider:
            return await provider.get_installation_access_token(17)

    token = asyncio.run(scenario())

    assert github.posts == [17]
    received_issued_token = token == "secret-17-1"
    assert received_issued_token, "a cold miss must return the issued installation credential"
    assert redis.lifetimes == {"token:17": 3540}


@dataclass
class _Clock:
    timestamp: float = _NOW

    def __call__(self) -> float:
        return self.timestamp


@pytest.mark.parametrize(
    ("elapsed_seconds", "expected_posts", "expected_reused"),
    [(3539, [17], True), (3540, [17, 17], False), (3541, [17, 17], False), (3601, [17, 17], False)],
    ids=["before-skew", "at-skew", "inside-skew", "expired"],
)
def test_local_fallback_expires_at_the_safety_skew_boundary(
    elapsed_seconds: int, expected_posts: list[int], expected_reused: bool
) -> None:
    clock = _Clock()
    redis, github = FakeRedis(write_broken=True), _GitHub(now=clock)
    cache = RedisInstallationAccessTokenCache(redis, now=clock)

    async def scenario() -> bool:
        async with _process(github, cache, now=clock) as provider:
            first = await provider.get_installation_access_token(17)
            clock.timestamp += elapsed_seconds
            second = await provider.get_installation_access_token(17)
            return first == second

    reused = asyncio.run(scenario())

    assert github.posts == expected_posts
    assert reused is expected_reused


def test_a_redis_hit_takes_precedence_over_a_valid_local_token() -> None:
    redis, github = FakeRedis(write_broken=True), _GitHub()

    async def scenario() -> bool:
        async with _process(github, _redis_cache(redis)) as provider:
            local = await provider.get_installation_access_token(17)
            redis.values["token:17"] = "redis-shared-credential"
            selected = await provider.get_installation_access_token(17)
            return selected == "redis-shared-credential" and selected != local

    selected_redis_token = asyncio.run(scenario())

    assert github.posts == [17]
    assert selected_redis_token, "a Redis hit must take precedence over the local credential"


def test_clean_redis_misses_after_eviction_reuse_local_token() -> None:
    redis, github = FakeRedis(), _GitHub()

    async def scenario() -> bool:
        async with _process(github, _redis_cache(redis)) as provider:
            first = await provider.get_installation_access_token(17)
            stored_keys = list(redis.values)
            assert stored_keys == ["token:17"]
            assert redis.lifetimes == {"token:17": 3540}
            del redis.values["token:17"]
            second = await provider.get_installation_access_token(17)
            third = await provider.get_installation_access_token(17)
            return first == second == third

    reused_original_token = asyncio.run(scenario())

    assert github.posts == [17]
    assert reused_original_token, "eviction must preserve a valid process-local credential"
