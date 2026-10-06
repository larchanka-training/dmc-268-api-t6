"""GitHub adapter that supplies the repository fields installation events omit (api#71)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field

import httpx
import pytest

from app.modules.integrations.webhooks.application.installation_event_projector import (
    RepositoryDetails,
    RepositoryDetailsUnavailableError,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
    GitHubRepositoryDetailsResponseError,
)


@dataclass
class FakeInstallationTokenProvider:
    token: str = "installation-token"
    error: Exception | None = None
    calls: list[int] = field(default_factory=list)

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        if self.error is not None:
            raise self.error
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


@pytest.mark.parametrize(
    ("full_name", "encoded_path"),
    [
        ("o/r with space", "/repos/o/r%20with%20space"),
        ("o/r?x", "/repos/o/r%3Fx"),
        ("o/r#x", "/repos/o/r%23x"),
        ("o/r%2Fx", "/repos/o/r%252Fx"),
    ],
)
def test_provider_percent_encodes_the_repository_path_but_keeps_the_owner_separator(
    full_name: str, encoded_path: str
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"default_branch": "main", "html_url": "https://example.test/o/r"},
        )

    _fetch(handler, FakeInstallationTokenProvider(), full_name)

    assert requests[0].url.raw_path == encoded_path.encode()
    assert requests[0].url.query == b""
    assert requests[0].url.fragment == ""


def _assert_unavailable(
    error: RepositoryDetailsUnavailableError, *, request: str, detail: str
) -> None:
    """The text names the failing request and the failure, and leaks no token, URL or body."""
    text = str(error)
    assert text.startswith(f"GitHub {request} failed")
    assert detail in text
    for secret in ("installation-token", "api.github.com", "example-owner", "SENTINEL-body"):
        assert secret not in text


def test_provider_reports_a_missing_repository_as_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, request=request, json={"message": "SENTINEL-body"})

    with pytest.raises(RepositoryDetailsUnavailableError) as raised:
        _fetch(handler, FakeInstallationTokenProvider())

    cause = raised.value.__cause__
    assert isinstance(cause, httpx.HTTPStatusError)
    assert cause.response.status_code == 404
    _assert_unavailable(raised.value, request="repository details request", detail="404")


@pytest.mark.parametrize("status", [403, 429, 500, 502, 503])
def test_provider_reports_any_other_http_error_status_as_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, request=request, json={"message": "SENTINEL-body"})

    with pytest.raises(RepositoryDetailsUnavailableError) as raised:
        _fetch(handler, FakeInstallationTokenProvider())

    cause = raised.value.__cause__
    assert isinstance(cause, httpx.HTTPStatusError)
    assert cause.response.status_code == status
    _assert_unavailable(raised.value, request="repository details request", detail=str(status))


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout])
def test_provider_reports_a_transport_failure_as_unavailable(
    failure: type[httpx.TransportError],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise failure("SENTINEL-body", request=request)

    with pytest.raises(RepositoryDetailsUnavailableError) as raised:
        _fetch(handler, FakeInstallationTokenProvider())

    assert isinstance(raised.value.__cause__, failure)
    _assert_unavailable(raised.value, request="repository details request", detail=failure.__name__)


@pytest.mark.parametrize(
    "mint_failure",
    [
        httpx.HTTPStatusError(
            "token mint failed",
            request=httpx.Request("POST", "https://api.github.com/app/installations/17/tokens"),
            response=httpx.Response(401, json={"message": "SENTINEL-body"}),
        ),
        httpx.ConnectError("SENTINEL-body"),
    ],
)
def test_provider_reports_a_token_mint_failure_as_unavailable(
    mint_failure: httpx.HTTPError,
) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"default_branch": "main", "html_url": "https://x.test/o"})

    with pytest.raises(RepositoryDetailsUnavailableError) as raised:
        _fetch(handler, FakeInstallationTokenProvider(error=mint_failure))

    assert raised.value.__cause__ is mint_failure
    assert requests == []
    detail = (
        "401" if isinstance(mint_failure, httpx.HTTPStatusError) else type(mint_failure).__name__
    )
    _assert_unavailable(raised.value, request="installation token request", detail=detail)


def test_provider_leaves_a_malformed_token_response_as_a_permanent_fault() -> None:
    malformed = ValueError("GitHub installation token response must contain a token")

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no repository request without a token")

    with pytest.raises(ValueError) as raised:
        _fetch(handler, FakeInstallationTokenProvider(error=malformed))

    assert raised.value is malformed
    assert not isinstance(raised.value, RepositoryDetailsUnavailableError)


def test_provider_leaves_an_unparsable_success_body_as_a_permanent_fault() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    with pytest.raises(ValueError) as raised:
        _fetch(handler, FakeInstallationTokenProvider())

    assert not isinstance(raised.value, RepositoryDetailsUnavailableError)


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
