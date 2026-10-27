"""Bitbucket repository discovery never leaks a token across origins (#333)."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
    BitbucketProviderError,
)


def test_fetch_private_repository_uses_bearer_and_immutable_uuid() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "uuid": "{3791f4cc-f59a-4412-9a52-f34a7829ba1c}",
                "full_name": "nvdovnar/test-ai-review",
                "slug": "test-ai-review",
                "name": "Test AI Review",
                "is_private": True,
                "workspace": {
                    "uuid": "{b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60}",
                    "slug": "nvdovnar",
                },
                "links": {"html": {"href": "https://bitbucket.org/nvdovnar/test-ai-review"}},
                "mainbranch": {"name": "main"},
                "language": "Python",
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            repo = await provider.fetch_repository(
                workspace_slug="nvdovnar", repository_slug="test-ai-review", token="secret"
            )
            assert repo.uuid == UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
            assert repo.workspace_uuid == UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
            assert repo.is_private
            assert repo.full_name == "nvdovnar/test-ai-review"

    asyncio.run(scenario())
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.bitbucket.org/2.0/repositories/nvdovnar/test-ai-review"
    assert seen[0].headers["Authorization"] == "Bearer secret"


def test_discovery_lists_private_repositories_and_rejects_foreign_next_url() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "values": [
                    {
                        "uuid": "{3791f4cc-f59a-4412-9a52-f34a7829ba1c}",
                        "full_name": "nvdovnar/test-ai-review",
                        "slug": "test-ai-review",
                        "name": "Test AI Review",
                        "is_private": True,
                        "workspace": {
                            "uuid": "{b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60}",
                            "slug": "nvdovnar",
                        },
                        "links": {
                            "html": {"href": "https://bitbucket.org/nvdovnar/test-ai-review"}
                        },
                        "mainbranch": {"name": "main"},
                        "language": "Python",
                    }
                ],
                "next": "https://api.bitbucket.org/2.0/repositories/nvdovnar?page=2",
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            page = await provider.discover(workspace_slug="nvdovnar", token="secret")
            assert len(page.repositories) == 1
            assert page.repositories[0].is_private
            assert page.next_cursor == (
                "https://api.bitbucket.org/2.0/repositories/nvdovnar?page=2"
            )
            await provider.discover(
                workspace_slug="nvdovnar", token="secret", cursor=page.next_cursor
            )
            with pytest.raises(BitbucketProviderError, match="invalid_cursor"):
                await provider.discover(
                    workspace_slug="nvdovnar",
                    token="secret",
                    cursor="https://evil.example/2.0/repositories/nvdovnar?page=2",
                )

    asyncio.run(scenario())
    assert requested == [
        "https://api.bitbucket.org/2.0/repositories/nvdovnar?pagelen=50",
        "https://api.bitbucket.org/2.0/repositories/nvdovnar?page=2",
    ]


def test_provider_network_failure_has_sanitized_error() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("internal proxy details", request=request)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(fail)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="provider_unavailable") as error:
                await provider.fetch_repository(
                    workspace_slug="nvdovnar", repository_slug="test-ai-review", token="secret"
                )
            assert "internal proxy details" not in str(error.value)

    asyncio.run(scenario())


def test_provider_bounds_discovery_cursor_and_response_body() -> None:
    def oversized(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b" " * (2 * 1024 * 1024 + 1))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(oversized)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            for query in ("pagelen=1000000&page=2", "q=private&page=2"):
                with pytest.raises(BitbucketProviderError, match="invalid_cursor"):
                    await provider.discover(
                        workspace_slug="nvdovnar",
                        token="secret",
                        cursor=f"https://api.bitbucket.org/2.0/repositories/nvdovnar?{query}",
                    )
            with pytest.raises(BitbucketProviderError, match="invalid_provider_response"):
                await provider.discover(workspace_slug="nvdovnar", token="secret")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("header", "expected"),
    [("37", 37), ("999999", 3600), ("not-a-date", None)],
)
def test_provider_preserves_bounded_retry_after(header: str, expected: int | None) -> None:
    def limited(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": header})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(limited)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="provider_rate_limited") as error:
                await provider.discover(workspace_slug="nvdovnar", token="secret")
            assert error.value.retry_after == expected

    asyncio.run(scenario())
