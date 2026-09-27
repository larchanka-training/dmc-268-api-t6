"""Infrastructure adapters for verified GitHub installation deliveries."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_resolver import (
    SqlAlchemyGitHubInstallationResolver,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
    GitHubInstallationTreeProvider,
    GitHubTreeResponseIncompleteError,
)
from app.modules.repositories.application.installation_repositories import (
    RepositorySnapshot,
    RepositoryTreeBlob,
)


class FakeSession:
    def __init__(self, installation_id: UUID | None) -> None:
        self._installation_id = installation_id
        self.scalar_calls = 0

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def scalar(self, statement: object) -> UUID | None:
        self.scalar_calls += 1
        return self._installation_id


@dataclass
class FakeInstallationTokenProvider:
    token: str = "installation-token"
    calls: list[int] = field(default_factory=list)

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        return self.token


@dataclass
class FakeInstallationTokenCache:
    tokens: dict[int, str] = field(default_factory=dict)
    set_calls: list[tuple[int, str]] = field(default_factory=list)

    def get(self, installation_external_id: int) -> str | None:
        return self.tokens.get(installation_external_id)

    def set(self, installation_external_id: int, access_token: str, expires_at: object) -> None:
        del expires_at
        self.tokens[installation_external_id] = access_token
        self.set_calls.append((installation_external_id, access_token))


def _repository() -> RepositorySnapshot:
    return RepositorySnapshot(
        id=101,
        full_name="octo/api",
        default_branch="main",
        html_url="https://github.com/octo/api",
    )


def test_github_app_token_provider_exchanges_an_app_jwt_per_installation_and_caches_it() -> None:
    requests: list[httpx.Request] = []
    encoded_claims: list[dict[str, object]] = []
    cache = FakeInstallationTokenCache()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "token": "token-for-17",
                "expires_at": datetime.fromtimestamp(1_800_003_600, UTC).isoformat(),
            },
        )

    def encode(claims: dict[str, object], private_key: str) -> str:
        assert private_key == "test-private-key"
        encoded_claims.append(claims)
        return "app-jwt"

    async def get_tokens() -> tuple[str, str]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id="123",
                private_key="test-private-key",
                jwt_encoder=encode,
                cache=cache,
                now=lambda: 1_800_000_000,
            )
            return (
                await provider.get_installation_access_token(17),
                await provider.get_installation_access_token(17),
            )

    first, replay = asyncio.run(get_tokens())

    assert (first, replay) == ("token-for-17", "token-for-17")
    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert str(requests[0].url) == "https://api.github.com/app/installations/17/access_tokens"
    assert requests[0].headers["Authorization"] == "Bearer app-jwt"
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert encoded_claims == [{"iat": 1_799_999_940, "exp": 1_800_000_540, "iss": "123"}]
    assert cache.set_calls == [(17, "token-for-17")]


def test_github_app_token_provider_propagates_exchange_failure_without_caching() -> None:
    cache = FakeInstallationTokenCache()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, request=request, json={"message": "Bad credentials"})

    async def get_token() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id="123",
                private_key="test-private-key",
                jwt_encoder=lambda claims, private_key: "app-jwt",
                cache=cache,
                now=lambda: 1_800_000_000,
            )
            await provider.get_installation_access_token(17)

    with pytest.raises(httpx.HTTPStatusError):
        asyncio.run(get_token())

    assert cache.tokens == {}


def test_github_app_token_provider_does_not_cache_a_token_near_expiry() -> None:
    now = 1_800_000_000
    cache = FakeInstallationTokenCache()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            201,
            json={
                "token": "near-expiry-token",
                "expires_at": datetime.fromtimestamp(now + 60, UTC).isoformat(),
            },
        )

    async def get_tokens() -> tuple[str, str]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubAppInstallationAccessTokenProvider(
                client=client,
                app_id="123",
                private_key="test-private-key",
                jwt_encoder=lambda claims, private_key: "app-jwt",
                cache=cache,
                now=lambda: now,
            )
            return (
                await provider.get_installation_access_token(17),
                await provider.get_installation_access_token(17),
            )

    assert asyncio.run(get_tokens()) == ("near-expiry-token", "near-expiry-token")
    assert len(requests) == 2
    assert cache.tokens == {}


def test_resolver_returns_only_the_existing_github_installation_id() -> None:
    installation_id = uuid4()
    session = FakeSession(installation_id)
    resolver = SqlAlchemyGitHubInstallationResolver(lambda: session)  # type: ignore[arg-type]

    result = asyncio.run(resolver.find_github_installation_id(17))

    assert result == installation_id
    assert session.scalar_calls == 1


def test_resolver_returns_none_when_the_github_installation_does_not_exist() -> None:
    session = FakeSession(None)
    resolver = SqlAlchemyGitHubInstallationResolver(lambda: session)  # type: ignore[arg-type]

    result = asyncio.run(resolver.find_github_installation_id(17))

    assert result is None
    assert session.scalar_calls == 1


def test_tree_provider_fetches_the_recursive_default_branch_tree() -> None:
    token_provider = FakeInstallationTokenProvider()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "src/main.py", "type": "blob", "size": 42},
                    {"path": "src", "type": "tree"},
                    {"path": "vendor", "type": "commit"},
                ]
            },
        )

    async def fetch() -> tuple[RepositoryTreeBlob, ...]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubInstallationTreeProvider(
                client=client,
                token_provider=token_provider,
            )
            return await provider.fetch_default_branch_tree(
                installation_external_id=17,
                repository=_repository(),
            )

    tree = asyncio.run(fetch())

    assert token_provider.calls == [17]
    assert requests[0].method == "GET"
    assert (
        str(requests[0].url) == "https://api.github.com/repos/octo/api/git/trees/main?recursive=1"
    )
    assert requests[0].headers["Authorization"] == "Bearer installation-token"
    assert tree == (
        RepositoryTreeBlob(path="src/main.py", size=42, entry_type="blob"),
        RepositoryTreeBlob(path="src", size=0, entry_type="tree"),
        RepositoryTreeBlob(path="vendor", size=0, entry_type="commit"),
    )


def test_tree_provider_propagates_github_provider_failures() -> None:
    token_provider = FakeInstallationTokenProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, request=request)

    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubInstallationTreeProvider(
                client=client,
                token_provider=token_provider,
            )
            await provider.fetch_default_branch_tree(
                installation_external_id=17,
                repository=_repository(),
            )

    try:
        asyncio.run(fetch())
    except httpx.HTTPStatusError as error:
        assert error.response.status_code == 502
    else:
        raise AssertionError("expected the GitHub provider failure to propagate")


def test_tree_provider_rejects_a_truncated_recursive_tree() -> None:
    token_provider = FakeInstallationTokenProvider()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "truncated": True,
                "tree": [{"path": "src/main.py", "type": "blob", "size": 42}],
            },
        )

    async def fetch() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            provider = GitHubInstallationTreeProvider(
                client=client,
                token_provider=token_provider,
            )
            await provider.fetch_default_branch_tree(
                installation_external_id=17,
                repository=_repository(),
            )

    with pytest.raises(GitHubTreeResponseIncompleteError):
        asyncio.run(fetch())
