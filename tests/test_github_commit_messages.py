"""Bounded PR commit context through the production GitHub source (#82)."""

from __future__ import annotations

import asyncio
import gzip
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import event
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubAppInstallationAccessTokenProvider,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import HEAD, RUN_ATTEMPTED, Env
from tests.test_github_vcs_provider import _pull_payload
from tests.test_retired_installation_postgres import DiscardTokenCache
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire
from tests.test_run_vcs_origin_postgres import vcs_env as vcs_env


@dataclass
class Runs:
    available: bool = True
    transaction_open: bool = False
    retire_after_pr: bool = False

    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput | None:
        self.transaction_open = True
        try:
            if not self.available:
                return None
            return RunVcsInput(
                code_change_id=uuid4(),
                repository_id=uuid4(),
                head_sha="a" * 40,
                base_sha="b" * 40,
                locator=PullRequestLocator(17, "octo/repo", 7),
            )
        finally:
            self.transaction_open = False

    async def get_run_snapshots(self, run_id: UUID) -> None:
        return None


@dataclass
class Tokens:
    runs: Runs
    calls: int = 0
    retire_on_call: int | None = None

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == 17
        assert not self.runs.transaction_open
        self.calls += 1
        if self.calls == self.retire_on_call:
            self.runs.available = False
        return "installation-token"


def _read(
    respond: Callable[[httpx.Request], httpx.Response],
    *,
    runs: Runs | None = None,
    tokens: Tokens | None = None,
) -> PullRequestMeta:
    runs = runs or Runs()
    tokens = tokens or Tokens(runs)

    def github(request: httpx.Request) -> httpx.Response:
        assert not runs.transaction_open
        assert request.headers["Authorization"] == "Bearer installation-token"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        assert request.extensions["timeout"] == {
            "connect": 10.0,
            "read": 10.0,
            "write": 10.0,
            "pool": 10.0,
        }
        if request.url.path == "/repos/octo/repo/pulls/7":
            if runs.retire_after_pr:
                runs.available = False
            return httpx.Response(200, json=_pull_payload())
        assert request.url.host == "api.github.test"
        assert request.url.path == "/repos/octo/repo/pulls/7/commits"
        assert request.url.params["per_page"] == "10"
        return respond(request)

    async def exercise() -> PullRequestMeta:
        async with httpx.AsyncClient(
            base_url="https://api.github.test",
            transport=httpx.MockTransport(github),
        ) as client:
            source = GitHubRunSource(
                client=client,
                token_provider=tokens,
                vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                runs=runs,
                run_id=uuid4(),
            )
            meta = await source.get_pull_request_meta(uuid4())
            assert meta is not None
            return meta

    return asyncio.run(exercise())


def test_short_page_preserves_api_order_and_composes_pr_metadata() -> None:
    pages: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        return httpx.Response(
            200,
            json=[
                {"commit": {"message": "second authored\n<fix>& change"}},
                {"commit": {"message": "first authored"}},
                {"commit": {"message": ""}},
            ],
        )

    meta = _read(github)

    assert meta.commit_messages == ("second authored\n<fix>& change", "first authored", "")
    assert meta.title == "Fix parser"
    assert meta.description == "Review me"
    assert meta.labels == ("backend",)
    assert meta.head_sha == "a" * 40
    assert pages == ["1"]


@pytest.mark.parametrize("second_page_size", [0, 3, 10])
def test_only_two_numeric_pages_are_read_despite_untrusted_links(second_page_size: int) -> None:
    pages: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        page = request.url.params["page"]
        pages.append(page)
        assert page in {"1", "2"}
        messages = ["z", "a", "y", "b", "x", "c", "w", "d", "v", "e"]
        if page == "2":
            messages = ["u", "f", "t", "g", "s", "h", "r", "i", "q", "j"][:second_page_size]
        return httpx.Response(
            200,
            json=[{"commit": {"message": m}} for m in messages],
            headers={"Link": '<https://evil.test/steal?page=900>; rel="next"'},
        )

    meta = _read(github)

    expected: tuple[str, ...] = ("z", "a", "y", "b", "x", "c", "w", "d", "v", "e")
    if second_page_size == 3:
        expected += ("u", "f", "t")
    elif second_page_size == 10:
        expected += ("u", "f", "t", "g", "s", "h", "r", "i", "q", "j")
    assert meta.commit_messages == expected
    assert pages == ["1", "2"]


@pytest.mark.parametrize("length", [499, 500, 501])
def test_message_limit_counts_unicode_characters_including_ellipsis(length: int) -> None:
    message = "😀" * length
    meta = _read(lambda request: httpx.Response(200, json=[{"commit": {"message": message}}]))
    expected = "😀" * length if length < 501 else "😀" * 499 + "…"
    assert meta.commit_messages == (expected,)


def test_twenty_long_messages_retain_at_most_ten_thousand_characters() -> None:
    meta = _read(
        lambda request: httpx.Response(
            200, json=[{"commit": {"message": "é" * 600}} for _ in range(10)]
        )
    )
    assert meta.commit_messages == ("é" * 499 + "…",) * 20
    assert sum(len(message) for message in meta.commit_messages) == 10_000


def test_empty_first_page_succeeds_without_further_requests() -> None:
    pages: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        return httpx.Response(200, json=[])

    assert _read(github).commit_messages == ()
    assert pages == ["1"]


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        "installation-token",
        17,
        True,
        [None],
        ["private commit text"],
        [{}],
        [{"commit": None}],
        [{"commit": []}],
        [{"commit": {}}],
        [{"commit": {"message": None}}],
        [{"commit": {"message": 7}}],
        [{"commit": {"message": ["private commit text"]}}],
        [{"commit": {"message": True}}],
        [{"commit": {"message": "private commit text"}}] * 11,
    ],
)
def test_malformed_commit_page_fails_without_exposing_provider_content(payload: object) -> None:
    with pytest.raises(RunFailure) as raised:
        _read(lambda request: httpx.Response(200, json=payload))
    assert raised.value.error_code == "diff_fetch_failed"
    assert "GitHub commit response" in raised.value.message
    assert "installation-token" not in str(raised.value)
    assert "private commit text" not in str(raised.value)


@pytest.mark.parametrize("body", [b"private commit text", b"[", b"\xff"])
def test_invalid_json_or_encoding_is_an_explicit_safe_failure(body: bytes) -> None:
    with pytest.raises(RunFailure) as raised:
        _read(lambda request: httpx.Response(200, content=body))
    assert raised.value.error_code == "diff_fetch_failed"
    assert raised.value.message == "GitHub commit response is malformed"


def test_malformed_second_page_does_not_succeed_with_a_partial_prefix() -> None:
    def github(request: httpx.Request) -> httpx.Response:
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[{"commit": {"message": "ok"}}] * 10)
        return httpx.Response(200, json=[{"commit": {"message": None}}])

    with pytest.raises(RunFailure, match="GitHub commit response is malformed"):
        _read(github)


@dataclass
class PageStream(httpx.AsyncByteStream):
    chunks: tuple[bytes, ...]
    read_chunks: int = 0
    closed: bool = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_chunks += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def test_one_mib_streamed_page_is_allowed_at_the_exact_boundary() -> None:
    body = b'[{"commit":{"message":"ok"}}]'
    body += b" " * (1_048_576 - len(body))
    stream = PageStream((body[:524_288], body[524_288:]))
    assert _read(lambda request: httpx.Response(200, stream=stream)).commit_messages == ("ok",)
    assert stream.closed


def test_streaming_limit_stops_before_buffering_the_remaining_page() -> None:
    stream = PageStream((b" " * 524_288, b" " * 524_288, b" ", b"private commit text"))
    with pytest.raises(RunFailure) as raised:
        _read(lambda request: httpx.Response(200, stream=stream))
    assert raised.value.error_code == "diff_fetch_failed"
    assert raised.value.message == "GitHub commit response exceeds the 1 MiB retrieval budget"
    assert stream.read_chunks == 3
    assert stream.closed


def test_response_limit_applies_to_decoded_gzip_bytes() -> None:
    body = gzip.compress(b"[]" + b" " * 1_048_575)
    stream = PageStream((body,))
    with pytest.raises(RunFailure, match="exceeds the 1 MiB retrieval budget"):
        _read(
            lambda request: httpx.Response(
                200,
                stream=stream,
                headers={"Content-Encoding": "gzip", "Content-Length": str(len(body))},
            )
        )
    assert stream.closed


@pytest.mark.parametrize(
    ("status", "headers", "code", "retry_seconds"),
    [
        (403, {}, "github_forbidden", None),
        (404, {}, "github_forbidden", None),
        (403, {"X-RateLimit-Remaining": "0"}, "diff_fetch_failed", None),
        (429, {"Retry-After": "120"}, "diff_fetch_failed", 120),
        (502, {}, "diff_fetch_failed", None),
    ],
)
def test_http_status_failures_use_existing_safe_classification(
    status: int,
    headers: dict[str, str],
    code: str,
    retry_seconds: int | None,
) -> None:
    with pytest.raises(RunFailure) as raised:
        _read(
            lambda request: httpx.Response(
                status, headers=headers, content=b"private commit text installation-token"
            )
        )
    assert raised.value.error_code == code
    assert raised.value.message == f"GitHub answered {status}"
    assert raised.value.retry_after == (
        timedelta(seconds=retry_seconds) if retry_seconds is not None else None
    )


def test_network_timeout_is_classified_without_exposing_request_details() -> None:
    def github(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("private commit text installation-token", request=request)

    with pytest.raises(RunFailure) as raised:
        _read(github)
    assert raised.value.error_code == "diff_fetch_failed"
    assert raised.value.message == "GitHub request failed: ReadTimeout"


@pytest.mark.parametrize(
    "when", ["initial", "after_pr", "between_pages", "first_token", "second_token"]
)
def test_cancellation_stops_subsequent_token_and_commit_http_work(when: str) -> None:
    runs = Runs(available=when != "initial", retire_after_pr=when == "after_pr")
    tokens = Tokens(runs, retire_on_call={"first_token": 2, "second_token": 3}.get(when))
    pages: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        pages.append(request.url.params["page"])
        if when == "between_pages":
            runs.available = False
        return httpx.Response(200, json=[{"commit": {"message": "ok"}}] * 10)

    with pytest.raises(RunCancelled, match="installation_unavailable"):
        _read(github, runs=runs, tokens=tokens)
    assert pages == (["1"] if when in {"between_pages", "second_token"} else [])
    assert (
        tokens.calls
        == {"initial": 0, "after_pr": 1, "between_pages": 2, "first_token": 2, "second_token": 3}[
            when
        ]
    )


@pytest.mark.integration
@pytest.mark.parametrize("retirement", ["none", "between_pages", "second_token"])
def test_commit_http_and_tokens_run_after_postgres_transactions_close(
    vcs_env: Env,
    retirement: str,
) -> None:
    async def exercise() -> None:
        engine = vcs_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        transactions = 0
        token_calls = 0
        pages: list[str] = []

        def begin(_connection: Connection) -> None:
            nonlocal transactions
            transactions += 1

        def end(_connection: Connection) -> None:
            nonlocal transactions
            transactions -= 1

        event.listen(engine.sync_engine, "begin", begin)
        event.listen(engine.sync_engine, "commit", end)
        event.listen(engine.sync_engine, "rollback", end)

        async def github(request: httpx.Request) -> httpx.Response:
            nonlocal token_calls
            assert transactions == 0
            if request.url.path == "/app/installations/17/access_tokens":
                token_calls += 1
                if retirement == "second_token" and token_calls == 3:
                    await retire(factory)
                return httpx.Response(
                    201,
                    json={
                        "token": "installation-token",
                        "expires_at": "2030-01-01T00:00:00Z",
                    },
                )
            if request.url.path == "/repos/octo/a/pulls/3":
                payload = _pull_payload(head_sha=HEAD)
                payload["number"] = 3
                return httpx.Response(200, json=payload)
            assert request.url.path == "/repos/octo/a/pulls/3/commits"
            page = request.url.params["page"]
            pages.append(page)
            if retirement == "between_pages" and page == "1":
                await retire(factory)
            return httpx.Response(200, json=[{"commit": {"message": "verified"}}] * 10)

        try:
            await prepare(factory, "active", "running")
            async with httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(github)
            ) as client:
                tokens = GitHubAppInstallationAccessTokenProvider(
                    client=client,
                    app_id="42",
                    private_key="unused",
                    jwt_encoder=lambda claims, key: "jwt",
                    cache=DiscardTokenCache(),
                    now=lambda: datetime(2026, 10, 10, tzinfo=UTC).timestamp(),
                )
                source = GitHubRunSource(
                    client=client,
                    token_provider=tokens,
                    vcs=HttpGitHubVcsProvider(client=client, token_provider=tokens),
                    runs=SqlAlchemyRunRepository(factory, allow_unscoped=True),
                    run_id=RUN_ATTEMPTED,
                )
                if retirement == "none":
                    meta = await source.get_pull_request_meta(RUN_ATTEMPTED)
                    assert meta is not None
                    assert meta.commit_messages == ("verified",) * 20
                else:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await source.get_pull_request_meta(RUN_ATTEMPTED)
            assert pages == (["1", "2"] if retirement == "none" else ["1"])
            assert token_calls == (2 if retirement == "between_pages" else 3)
            assert transactions == 0
        finally:
            await engine.dispose()

    asyncio.run(exercise())
