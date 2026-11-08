"""The GitHub read behind the background visibility refresh, over a mock transport (#454)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable

import httpx
import pytest

from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_visibility import (
    GitHubRepositoryVisibilityReader,
)
from app.modules.repositories.application.repository_visibility import (
    RepositoryVisibilityReading,
    RepositoryVisibilityUnavailable,
)

BODY = {"private": False, "full_name": "octo/new", "html_url": "https://github.test/octo/new"}


class _Tokens:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.calls: list[int] = []

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls.append(installation_external_id)
        if self.error is not None:
            raise self.error
        return "installation-token"


def _read(
    handler: Callable[[httpx.Request], httpx.Response], tokens: _Tokens | None = None
) -> RepositoryVisibilityReading | None:
    async def run() -> RepositoryVisibilityReading | None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="https://api.github.com"
        ) as client:
            return await GitHubRepositoryVisibilityReader(
                client=client, token_provider=tokens or _Tokens()
            ).read(17, 101)

    return asyncio.run(run())


def _status_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://api.github.com/app/installations/17/access_tokens")
    return httpx.HTTPStatusError(
        "failed",
        request=request,
        response=httpx.Response(status, request=request, headers=headers),
    )


def test_a_read_asks_by_the_repository_id_with_the_installation_token() -> None:
    requests: list[httpx.Request] = []
    tokens = _Tokens()

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=BODY)

    reading = _read(handler, tokens)

    assert reading == RepositoryVisibilityReading(False, "octo/new", "https://github.test/octo/new")
    assert tokens.calls == [17]
    assert str(requests[0].url) == "https://api.github.com/repositories/101"
    assert requests[0].headers["Authorization"] == "Bearer installation-token"


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        pytest.param({"private": True}, (True, "octo/new"), id="private"),
        pytest.param({"private": "false"}, (None, "octo/new"), id="flag-not-a-boolean"),
        pytest.param({"private": None}, (None, "octo/new"), id="flag-null"),
        pytest.param({"full_name": "octo/.."}, (False, None), id="name-github-cannot-have"),
        pytest.param({"full_name": 7}, (False, None), id="name-not-a-string"),
    ],
)
def test_a_value_that_cannot_be_used_reads_as_unknown(
    change: dict[str, object], expected: tuple[bool | None, str | None]
) -> None:
    reading = _read(lambda _: httpx.Response(200, json=BODY | change))

    assert reading is not None
    assert (reading.private, reading.full_name) == expected


@pytest.mark.parametrize(
    "response",
    [
        pytest.param(httpx.Response(404, json={}), id="gone-or-no-access"),
        pytest.param(httpx.Response(403, json={}), id="forbidden"),
        pytest.param(httpx.Response(451, json={}), id="blocked-for-legal-reasons"),
        pytest.param(httpx.Response(410, json={}), id="gone"),
        pytest.param(httpx.Response(422, json={}), id="unprocessable"),
        pytest.param(httpx.Response(301, headers={"location": "/x"}), id="redirect"),
    ],
)
def test_a_final_answer_without_the_repository_reads_as_none(response: httpx.Response) -> None:
    # Read as unavailable, such a row would stay first in the queue and end every pass.
    assert _read(lambda _: response) is None


def _answer(response: httpx.Response) -> Callable[[httpx.Request], httpx.Response]:
    return lambda _: response


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("refused", request=request)


@pytest.mark.parametrize(
    "handler",
    [
        pytest.param(_answer(httpx.Response(500, json={})), id="server-error"),
        pytest.param(_answer(httpx.Response(401, json={})), id="token-rejected"),
        pytest.param(_answer(httpx.Response(429, json={})), id="too-many-requests"),
        pytest.param(
            _answer(httpx.Response(403, json={}, headers={"retry-after": "60"})),
            id="secondary-rate-limit",
        ),
        pytest.param(
            _answer(httpx.Response(403, json={}, headers={"x-ratelimit-remaining": "0"})),
            id="rate-limit",
        ),
        pytest.param(_answer(httpx.Response(200, text="not json")), id="not-json"),
        pytest.param(_answer(httpx.Response(200, json=[])), id="not-an-object"),
        pytest.param(_refused, id="connection-refused"),
    ],
)
def test_a_failed_read_is_unavailable_not_unknown(
    handler: Callable[[httpx.Request], httpx.Response],
) -> None:
    with pytest.raises(RepositoryVisibilityUnavailable) as raised:
        _read(handler)

    assert "not json" not in str(raised.value)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_status_error(403), id="suspended"),
        pytest.param(_status_error(404), id="uninstalled"),
        pytest.param(InstallationAccessTokenError("no key", transient=False), id="permanent"),
    ],
)
def test_a_token_github_refuses_for_good_reads_as_none(error: Exception) -> None:
    assert _read(lambda _: httpx.Response(200, json=BODY), _Tokens(error)) is None


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(_status_error(401), id="app-key-rejected"),
        pytest.param(_status_error(403, {"retry-after": "60"}), id="secondary-rate-limit"),
        pytest.param(_status_error(403, {"x-ratelimit-remaining": "0"}), id="rate-limit"),
        pytest.param(_status_error(429), id="too-many-requests"),
        pytest.param(_status_error(500), id="server-error"),
        pytest.param(httpx.ConnectError("refused"), id="exchange"),
        pytest.param(InstallationAccessTokenError("later", transient=True), id="transient"),
    ],
)
def test_a_token_github_could_not_issue_now_is_unavailable(error: Exception) -> None:
    with pytest.raises(RepositoryVisibilityUnavailable):
        _read(lambda _: httpx.Response(200, json=BODY), _Tokens(error))
