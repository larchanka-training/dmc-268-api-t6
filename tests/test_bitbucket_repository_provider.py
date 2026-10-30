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
                workspace_slug="NVDOVNAR", repository_slug="TEST-AI-REVIEW", token="secret"
            )
            assert repo.uuid == UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
            assert repo.workspace_uuid == UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
            assert repo.is_private
            assert repo.full_name == "nvdovnar/test-ai-review"

    asyncio.run(scenario())
    assert len(seen) == 1
    assert str(seen[0].url) == "https://api.bitbucket.org/2.0/repositories/nvdovnar/test-ai-review"
    assert seen[0].headers["Authorization"] == "Bearer secret"


def test_pr_scope_probe_uses_immutable_repository_identity_and_maps_missing_scope() -> None:
    workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    repository = UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer repository-only-token"
        return httpx.Response(403, json={"error": {"message": "scope denied"}})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="repository_inaccessible"):
                await provider.probe_pull_request_read(
                    workspace_uuid=workspace,
                    repository_uuid=repository,
                    token="repository-only-token",
                )

    asyncio.run(scenario())
    assert requested == [
        "https://api.bitbucket.org/2.0/repositories/"
        "%7Bb2d0d95e-1f8d-4990-9fb2-f09f87ae5f60%7D/"
        "%7B3791f4cc-f59a-4412-9a52-f34a7829ba1c%7D/pullrequests?pagelen=1"
    ]


def test_user_scope_probe_rejects_token_without_identity_access() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer repository-only-token"
        return httpx.Response(403, json={"error": {"message": "scope denied"}})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="repository_inaccessible"):
                await provider.probe_current_user(token="repository-only-token")

    asyncio.run(scenario())
    assert requested == ["https://api.bitbucket.org/2.0/user"]


def test_rotate_lookup_uses_workspace_and_repository_uuid_after_slug_change() -> None:
    workspace = UUID("b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60")
    repository = UUID("3791f4cc-f59a-4412-9a52-f34a7829ba1c")
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={
                "uuid": "{3791f4cc-f59a-4412-9a52-f34a7829ba1c}",
                "full_name": "new-space/renamed-repo",
                "slug": "renamed-repo",
                "name": "Renamed Repo",
                "is_private": True,
                "workspace": {
                    "uuid": "{b2d0d95e-1f8d-4990-9fb2-f09f87ae5f60}",
                    "slug": "new-space",
                },
                "links": {"html": {"href": "https://bitbucket.org/new-space/renamed-repo"}},
                "mainbranch": {"name": "main"},
            },
        )

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            found = await provider.fetch_repository_by_uuid(
                workspace_uuid=workspace, repository_uuid=repository, token="secret"
            )
            assert found.uuid == repository
            assert found.workspace_uuid == workspace
            assert found.full_name == "new-space/renamed-repo"

    asyncio.run(scenario())
    assert requested == [
        "https://api.bitbucket.org/2.0/repositories/"
        "%7Bb2d0d95e-1f8d-4990-9fb2-f09f87ae5f60%7D/"
        "%7B3791f4cc-f59a-4412-9a52-f34a7829ba1c%7D"
    ]


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


@pytest.mark.parametrize("token", ["", "trailing ", "bad\u00a0token", "x" * 4097])
def test_provider_rejects_invalid_token_before_http(token: str) -> None:
    calls = 0

    def respond(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="invalid_credential"):
                await provider.fetch_repository(
                    workspace_slug="team", repository_slug="repo", token=token
                )

    asyncio.run(scenario())
    assert calls == 0


def test_provider_does_not_follow_redirect_with_bearer_token() -> None:
    seen: list[httpx.Request] = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://attacker.example/steal"})

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(redirect), follow_redirects=True
        ) as client:
            provider = BitbucketCloudRepositoryProvider(client)
            with pytest.raises(BitbucketProviderError, match="provider_unavailable"):
                await provider.fetch_repository(
                    workspace_slug="team", repository_slug="repo", token="secret"
                )

    asyncio.run(scenario())
    assert len(seen) == 1
    assert seen[0].url.host == "api.bitbucket.org"
    assert seen[0].headers["Authorization"] == "Bearer secret"


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
