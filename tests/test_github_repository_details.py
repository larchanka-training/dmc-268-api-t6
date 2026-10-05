"""GitHub adapter that supplies the repository fields installation events omit (api#71)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from app.modules.integrations.webhooks.application.installation_event_projector import (
    RepositoryDetails,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
    GitHubRepositoryDetailsResponseError,
)


@dataclass
class FakeInstallationTokenProvider:
    token: str = "installation-token"
    calls: list[int] = field(default_factory=list)

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        return self.token


def _fetch(
    handler: Callable[[httpx.Request], httpx.Response],
    tokens: FakeInstallationTokenProvider,
    full_name: str = "example-owner/example-repo",
) -> RepositoryDetails:
    async def run() -> RepositoryDetails:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            return await GitHubInstallationRepositoryDetailsProvider(
                client=client, token_provider=tokens
            ).fetch_repository_details(installation_external_id=17, full_name=full_name)

    return asyncio.run(run())


def test_provider_reads_the_default_branch_and_web_url_with_the_installation_token() -> None:
    tokens = FakeInstallationTokenProvider()
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "id": 1000004,
                "full_name": "example-owner/example-repo",
                "default_branch": "trunk",
                "html_url": "https://example.test/example-owner/example-repo",
            },
        )

    details = _fetch(handler, tokens)

    assert details == RepositoryDetails(
        default_branch="trunk", web_url="https://example.test/example-owner/example-repo"
    )
    assert tokens.calls == [17]
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == "https://api.github.com/repos/example-owner/example-repo"
    assert requests[0].headers["Authorization"] == "Bearer installation-token"
    assert requests[0].headers["Accept"] == "application/vnd.github+json"
    assert requests[0].headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_provider_percent_encodes_the_repository_path_but_keeps_the_owner_separator() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"default_branch": "main", "html_url": "https://example.test/o/r"},
        )

    _fetch(handler, FakeInstallationTokenProvider(), "o/r with space")

    assert str(requests[0].url) == "https://api.github.com/repos/o/r%20with%20space"


def test_provider_propagates_a_missing_repository_as_an_http_status_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, json={"message": "Not Found"})

    with pytest.raises(httpx.HTTPStatusError) as raised:
        _fetch(handler, FakeInstallationTokenProvider())

    assert raised.value.response.status_code == 404


@pytest.mark.parametrize(
    "body",
    [
        {"html_url": "https://example.test/example-owner/example-repo"},
        {"default_branch": "trunk"},
        {"default_branch": "", "html_url": "https://example.test/example-owner/example-repo"},
        {"default_branch": "trunk", "html_url": ""},
        {"default_branch": None, "html_url": "https://example.test/example-owner/example-repo"},
        {"default_branch": "trunk", "html_url": 7},
        {},
        ["not", "an", "object"],
    ],
)
def test_provider_rejects_a_response_without_a_usable_branch_and_url(body: object) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=body)

    with pytest.raises(GitHubRepositoryDetailsResponseError):
        _fetch(handler, FakeInstallationTokenProvider())
