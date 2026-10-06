"""Concurrent callers of one installation share a single token mint (api#73)."""

from __future__ import annotations

import asyncio
import gc
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    InMemoryInstallationAccessTokenCache,
)

_NOW = 1_800_000_000


@dataclass
class _GitHub:
    """The token endpoint: every POST waits on ``gate``, then answers with ``status``."""

    gate: asyncio.Event = field(default_factory=asyncio.Event)
    status: int = 201
    posts: list[int] = field(default_factory=list)

    async def handler(self, request: httpx.Request) -> httpx.Response:
        installation = int(request.url.path.split("/")[3])
        self.posts.append(installation)
        await self.gate.wait()
        if self.status != 201:
            return httpx.Response(self.status, request=request, json={"message": "no"})
        return httpx.Response(
            201,
            json={
                "token": f"token-for-{installation}",
                "expires_at": datetime.fromtimestamp(_NOW + 3600, UTC).isoformat(),
            },
        )


def _provider(client: httpx.AsyncClient) -> GitHubAppInstallationAccessTokenProvider:
    return GitHubAppInstallationAccessTokenProvider(
        client=client,
        app_id="123",
        private_key="test-private-key",
        jwt_encoder=lambda claims, private_key: "app-jwt",
        cache=InMemoryInstallationAccessTokenCache(now=lambda: _NOW),
        now=lambda: _NOW,
    )


async def _settle() -> None:
    """Let every started task run to its next suspension point."""
    for _ in range(10):
        await asyncio.sleep(0)


def _client(github: _GitHub) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(github.handler), base_url="https://api.github.com"
    )


def test_concurrent_callers_of_one_installation_share_a_single_mint() -> None:
    async def scenario() -> tuple[list[str], list[int]]:
        github = _GitHub()
        async with _client(github) as client:
            provider = _provider(client)
            callers = [
                asyncio.create_task(provider.get_installation_access_token(17)) for _ in range(5)
            ]
            await _settle()
            github.gate.set()
            return list(await asyncio.gather(*callers)), github.posts

    tokens, posts = asyncio.run(scenario())

    assert tokens == ["token-for-17"] * 5
    assert posts == [17]


def test_different_installations_mint_independently() -> None:
    async def scenario() -> tuple[list[str], list[int]]:
        github = _GitHub()
        async with _client(github) as client:
            provider = _provider(client)
            callers = [
                asyncio.create_task(provider.get_installation_access_token(installation))
                for installation in (17, 18, 17, 18, 19)
            ]
            await _settle()
            github.gate.set()
            return list(await asyncio.gather(*callers)), sorted(github.posts)

    tokens, posts = asyncio.run(scenario())

    assert tokens == [
        "token-for-17",
        "token-for-18",
        "token-for-17",
        "token-for-18",
        "token-for-19",
    ]
    assert posts == [17, 18, 19]


def test_concurrent_callers_share_the_failure_of_the_single_mint() -> None:
    async def scenario() -> tuple[list[BaseException | str], list[int]]:
        github = _GitHub(status=401)
        async with _client(github) as client:
            provider = _provider(client)
            callers = [
                asyncio.create_task(provider.get_installation_access_token(17)) for _ in range(4)
            ]
            await _settle()
            github.gate.set()
            return list(await asyncio.gather(*callers, return_exceptions=True)), github.posts

    outcomes, posts = asyncio.run(scenario())

    assert posts == [17]
    assert len(outcomes) == 4
    assert all(isinstance(outcome, httpx.HTTPStatusError) for outcome in outcomes)
    assert all(
        isinstance(outcome, httpx.HTTPStatusError) and outcome.response.status_code == 401
        for outcome in outcomes
    )


def test_a_failed_mint_is_not_cached_so_a_later_call_mints_again() -> None:
    async def scenario() -> tuple[str, list[int]]:
        github = _GitHub(status=502)
        github.gate.set()
        async with _client(github) as client:
            provider = _provider(client)
            with pytest.raises(httpx.HTTPStatusError):
                await provider.get_installation_access_token(17)
            github.status = 201
            token = await provider.get_installation_access_token(17)
            return token, github.posts

    token, posts = asyncio.run(scenario())

    assert token == "token-for-17"
    assert posts == [17, 17]


def test_a_cached_token_is_served_without_a_new_mint_after_the_shared_one() -> None:
    async def scenario() -> list[int]:
        github = _GitHub()
        github.gate.set()
        async with _client(github) as client:
            provider = _provider(client)
            await asyncio.gather(*(provider.get_installation_access_token(17) for _ in range(3)))
            await provider.get_installation_access_token(17)
            return github.posts

    assert asyncio.run(scenario()) == [17]


def test_cancelling_one_waiter_does_not_cancel_the_mint_of_the_others() -> None:
    async def scenario() -> tuple[list[BaseException | str], list[int]]:
        github = _GitHub()
        async with _client(github) as client:
            provider = _provider(client)
            callers = [
                asyncio.create_task(provider.get_installation_access_token(17)) for _ in range(3)
            ]
            await _settle()
            callers[0].cancel()
            await _settle()
            github.gate.set()
            return list(await asyncio.gather(*callers, return_exceptions=True)), github.posts

    outcomes, posts = asyncio.run(scenario())

    assert isinstance(outcomes[0], asyncio.CancelledError)
    assert outcomes[1:] == ["token-for-17", "token-for-17"]
    assert posts == [17]


def test_a_mint_that_fails_after_every_waiter_left_logs_no_unretrieved_exception() -> None:
    reported: list[dict[str, Any]] = []

    async def scenario() -> list[int]:
        asyncio.get_running_loop().set_exception_handler(
            lambda loop, context: reported.append(context)
        )
        github = _GitHub(status=500)
        async with _client(github) as client:
            provider = _provider(client)
            waiter = asyncio.create_task(provider.get_installation_access_token(17))
            await _settle()
            waiter.cancel()
            await _settle()
            github.gate.set()
            await _settle()
            gc.collect()
            return github.posts

    posts = asyncio.run(scenario())
    gc.collect()

    assert posts == [17]
    assert reported == []


def test_a_mint_cancelled_before_its_first_step_does_not_poison_the_next_call() -> None:
    async def scenario() -> tuple[str, list[int]]:
        github = _GitHub()
        async with _client(github) as client:
            provider = _provider(client)
            first = asyncio.create_task(provider.get_installation_access_token(17))
            await asyncio.sleep(0)  # the first call has started the mint task, which has not run
            mint = next(
                task for task in asyncio.all_tasks() if task not in {first, asyncio.current_task()}
            )
            mint.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            github.gate.set()
            return await provider.get_installation_access_token(17), github.posts

    token, posts = asyncio.run(scenario())

    assert token == "token-for-17"
    assert posts == [17]
