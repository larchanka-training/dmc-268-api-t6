"""GitHub policy reads: tree entries by type and failures that are never 'missing'."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.repositories.application.rules_status import RulesRefreshTarget
from app.modules.reviews.application.policy_documents import LoadPolicyDocuments, PolicyEntryKind
from app.modules.reviews.application.policy_errors import (
    PolicyError,
    PolicyErrorCode,
    policy_error_detail,
)
from app.modules.reviews.application.policy_snapshot import DocumentPresence
from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_policy_source import (
    GitHubDefaultBranchHeadSource,
    GitHubPolicyDocumentSource,
)
from app.modules.reviews.infrastructure.vcs_failures import StreamedStatusError
from tests.github_raw_blobs import RAW_BLOB_MEDIA_TYPE, git_blob_oid

_BASE = "b" * 40
_REVIEW_TREE = "1" * 40
_AGENTS = git_blob_oid(b"agents")
_RULES = git_blob_oid(b"rules")
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)


async def _checkpoint() -> None:
    return None


def _item(path: str, sha: str, *, type_: str = "blob", mode: str = "100644") -> dict[str, Any]:
    return {"path": path, "mode": mode, "type": type_, "sha": sha, "size": 5}


def _run(respond: Any, action: Any) -> Any:
    async def exercise() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            source = GitHubPolicyDocumentSource(
                client=client,
                token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
                locator=_LOCATOR,
                checkpoint=_checkpoint,
                policy_base_sha=_BASE,
            )
            return await action(source)

    return asyncio.run(exercise())


def test_documents_are_read_from_trees_of_the_base_revision_by_sha() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers["Authorization"] == "Bearer token"
        if request.url.path.endswith(f"/git/trees/{_BASE}"):
            return httpx.Response(
                200,
                json={
                    "tree": [
                        _item(".review", _REVIEW_TREE, type_="tree", mode="040000"),
                        _item("AGENTS.md", _AGENTS),
                    ],
                    "truncated": False,
                },
            )
        if request.url.path.endswith(f"/git/trees/{_REVIEW_TREE}"):
            return httpx.Response(200, json={"tree": [_item("rules.md", _RULES)]})
        blobs = {_AGENTS: b"agents", _RULES: b"rules"}
        sha = request.url.path.rsplit("/", 1)[1]
        if request.url.path.endswith(f"/git/blobs/{sha}") and sha in blobs:
            return httpx.Response(200, content=blobs[sha])
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    loaded = _run(
        respond, lambda source: LoadPolicyDocuments(source, policy_base_sha=_BASE).execute()
    )

    assert (loaded.rules.document.text, loaded.rules.document.blob_oid) == ("rules", _RULES)
    assert (loaded.agents.document.text, loaded.agents.document.blob_oid) == ("agents", _AGENTS)
    assert paths == [
        f"/repos/octo/repo/git/trees/{_BASE}",
        f"/repos/octo/repo/git/trees/{_REVIEW_TREE}",
        f"/repos/octo/repo/git/blobs/{_RULES}",
        f"/repos/octo/repo/git/blobs/{_AGENTS}",
    ]


def test_tree_entry_types_are_told_apart() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "tree": [
                    _item("file.md", "4" * 40),
                    _item("run.sh", "5" * 40, mode="100755"),
                    _item("link.md", "6" * 40, mode="120000"),
                    _item("dir", "7" * 40, type_="tree", mode="040000"),
                    _item("sub", "8" * 40, type_="commit", mode="160000"),
                ]
            },
        )

    entries = _run(respond, lambda source: source.list_entries(None))

    assert {name: entry.kind for name, entry in entries.items()} == {
        "file.md": PolicyEntryKind.FILE,
        "run.sh": PolicyEntryKind.FILE,
        "link.md": PolicyEntryKind.SYMLINK,
        "dir": PolicyEntryKind.DIRECTORY,
        "sub": PolicyEntryKind.SUBMODULE,
    }
    assert entries["file.md"].size == 5


def test_empty_tree_proves_absence_and_a_truncated_one_does_not() -> None:
    def empty(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [], "truncated": False})

    def truncated(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [], "truncated": True})

    def load(source: GitHubPolicyDocumentSource) -> Any:
        return LoadPolicyDocuments(source, policy_base_sha=_BASE).execute()

    assert _run(empty, load).rules.document.presence is DocumentPresence.MISSING
    with pytest.raises(PolicyError) as caught:
        _run(truncated, load)
    assert caught.value.code is PolicyErrorCode.REVISION_UNAVAILABLE


def test_http_failures_map_to_policy_codes_and_never_to_a_missing_document() -> None:
    cases: list[tuple[httpx.Response | Exception, PolicyErrorCode, bool]] = [
        (httpx.Response(404), PolicyErrorCode.REVISION_UNAVAILABLE, False),
        (httpx.Response(401), PolicyErrorCode.ACCESS_DENIED, False),
        (httpx.Response(403), PolicyErrorCode.ACCESS_DENIED, False),
        (
            httpx.Response(403, headers={"X-RateLimit-Remaining": "0"}),
            PolicyErrorCode.RATE_LIMITED,
            True,
        ),
        # A Retry-After too long to convert is still a rate limit, not a lost access.
        (
            httpx.Response(403, headers={"Retry-After": "1" * 5000}),
            PolicyErrorCode.RATE_LIMITED,
            True,
        ),
        (httpx.Response(429, headers={"Retry-After": "30"}), PolicyErrorCode.RATE_LIMITED, True),
        (httpx.Response(502), PolicyErrorCode.FETCH_TRANSIENT, True),
        (httpx.ReadTimeout("slow"), PolicyErrorCode.FETCH_TRANSIENT, True),
    ]
    for outcome, code, retryable in cases:

        def respond(
            request: httpx.Request, outcome: httpx.Response | Exception = outcome
        ) -> httpx.Response:
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with pytest.raises(PolicyError) as caught:
            _run(
                respond,
                lambda source: LoadPolicyDocuments(source, policy_base_sha=_BASE).execute(),
            )
        assert caught.value.code is code
        assert caught.value.retryable is retryable
        assert caught.value.policy_base_sha == _BASE

    assert caught.value.retry_after is None


def test_rate_limit_keeps_the_retry_after_delay() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": "30"})

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.list_entries(None))
    assert caught.value.retry_after is not None
    assert caught.value.retry_after.total_seconds() == 30


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (404, PolicyErrorCode.REVISION_UNAVAILABLE),
        (403, PolicyErrorCode.ACCESS_DENIED),
        (502, PolicyErrorCode.FETCH_TRANSIENT),
    ],
)
def test_blob_failures_are_policy_errors(status: int, code: PolicyErrorCode) -> None:
    # The source reads the blob itself, not through the VCS provider (#168).
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/octo/repo/git/blobs/{_AGENTS}"
        return httpx.Response(status)

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.read_blob(_AGENTS))
    assert (caught.value.code, caught.value.policy_base_sha) == (code, _BASE)


class _Chunks(httpx.AsyncByteStream):
    """A body without ``Content-Length`` sent in fixed chunks; counts the bytes handed out."""

    def __init__(self, body: bytes, chunk: int = 4096) -> None:
        self._body = body
        self._chunk = chunk
        self.sent = 0

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), self._chunk):
            piece = self._body[start : start + self._chunk]
            self.sent += len(piece)
            yield piece


def test_policy_blob_is_streamed_raw_by_its_sha() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"agents")

    assert _run(respond, lambda source: source.read_blob(_AGENTS)) == b"agents"
    (request,) = seen
    assert request.url.path == f"/repos/octo/repo/git/blobs/{_AGENTS}"
    assert request.headers["Authorization"] == "Bearer token"
    # The bytes of the document themselves: no base64 envelope, no transfer encoding.
    assert (request.headers["Accept"], request.headers["Accept-Encoding"]) == (
        RAW_BLOB_MEDIA_TYPE,
        "identity",
    )


@pytest.mark.parametrize("declared", [True, False], ids=["content-length", "chunked"])
def test_policy_blob_of_exactly_65536_bytes_is_read_whole(declared: bool) -> None:
    raw = b"a" * 65_536

    def respond(request: httpx.Request) -> httpx.Response:
        if declared:
            return httpx.Response(200, content=raw)
        return httpx.Response(200, stream=_Chunks(raw))

    assert _run(respond, lambda source: source.read_blob(git_blob_oid(raw))) == raw


@pytest.mark.parametrize("size", [65_537, 1_048_576], ids=["one-byte-over", "one-mebibyte"])
def test_policy_blob_over_65536_bytes_stops_the_stream(size: int) -> None:
    body = _Chunks(b"a" * size)

    with pytest.raises(PolicyError) as caught:
        _run(
            lambda request: httpx.Response(200, stream=body),
            lambda source: source.read_blob(_AGENTS),
        )

    assert (caught.value.code, caught.value.limit) == (PolicyErrorCode.DOCUMENT_TOO_LARGE, 65_536)
    # The read stops with the first chunk over the limit: at most one chunk more is taken.
    assert 65_536 < body.sent <= 65_536 + 4096


def test_policy_blob_declared_over_65536_bytes_is_not_read() -> None:
    body = _Chunks(b"a" * 70_000)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Length": "70000"}, stream=body)

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.read_blob(_AGENTS))

    assert caught.value.code is PolicyErrorCode.DOCUMENT_TOO_LARGE
    assert body.sent == 0


def test_policy_blob_of_another_id_is_a_retryable_fetch_failure() -> None:
    # The raw answer echoes no SHA: the bytes must hash to the requested blob id.
    with pytest.raises(PolicyError) as caught:
        _run(
            lambda request: httpx.Response(200, content=b"rules"),
            lambda source: source.read_blob(_AGENTS),
        )

    assert (caught.value.code, caught.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)


def test_policy_blob_id_of_64_digits_is_checked_with_sha256() -> None:
    raw = b"agents"
    oid = hashlib.sha256(b"blob %d\0" % len(raw) + raw).hexdigest()

    assert _run(lambda request: httpx.Response(200, content=raw), lambda s: s.read_blob(oid)) == raw


def _too_large_agents(blob: httpx.Response) -> PolicyError:
    """The failure of loading an ``AGENTS.md`` whose listing has no size, so its blob is read."""

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(f"/git/trees/{_BASE}"):
            entry = {"path": "AGENTS.md", "mode": "100644", "type": "blob", "sha": _AGENTS}
            return httpx.Response(200, json={"tree": [entry], "truncated": False})
        assert request.url.path.endswith(f"/git/blobs/{_AGENTS}")
        return blob

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: LoadPolicyDocuments(source, policy_base_sha=_BASE).execute())
    assert caught.value.code is PolicyErrorCode.DOCUMENT_TOO_LARGE
    return caught.value


def test_too_large_blob_reports_the_declared_size_as_exact() -> None:
    error = _too_large_agents(
        httpx.Response(200, headers={"Content-Length": "70000"}, stream=_Chunks(b"a" * 70_000))
    )

    assert (error.source_path, error.actual, error.actual_is_lower_bound) == (
        "AGENTS.md",
        70_000,
        False,
    )
    assert (
        policy_error_detail(error.message) == "файл `AGENTS.md`, фактически 70000, допустимо 65536"
    )
    assert error.diagnostic() == {
        "code": "policy_document_too_large",
        "source_path": "AGENTS.md",
        "policy_base_sha": _BASE,
        "message": "The policy document exceeds 65536 bytes",
        "line": None,
        "column": None,
        "pattern_index": None,
        "limit": 65_536,
        "actual": 70_000,
        "actual_is_lower_bound": False,
        "retryable": False,
    }


def test_too_large_blob_without_a_declared_size_reports_the_bytes_read_as_a_lower_bound() -> None:
    error = _too_large_agents(httpx.Response(200, stream=_Chunks(b"a" * 1_048_576)))

    # 17 chunks of 4096 bytes were read when the stream stopped: the blob has at least those.
    assert (error.actual, error.actual_is_lower_bound) == (69_632, True)
    # The read stopped past the limit, so "more than 65536" is true; 69632 is never shown.
    assert policy_error_detail(error.message) == (
        "файл `AGENTS.md`, фактически более 65536, допустимо 65536"
    )
    diagnostic = error.diagnostic()
    assert (diagnostic["actual"], diagnostic["actual_is_lower_bound"]) == (69_632, True)


@pytest.mark.parametrize("encoding", ["gzip", "deflate", "br"])
def test_encoded_policy_blob_is_refused_before_reading(encoding: str) -> None:
    # The read asks for identity: httpx would decode each chunk whole, so 1 MiB gzipped
    # would be in memory before the 64 KiB cap could stop it.
    body = _Chunks(gzip.compress(b"a" * 1_048_576))

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"Content-Encoding": encoding}, stream=body)

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.read_blob(_AGENTS))

    assert (caught.value.code, caught.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)
    assert body.sent == 0


def test_answer_that_cannot_be_decoded_is_a_policy_fetch_failure_not_an_internal_one() -> None:
    def proxy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with pytest.raises(PolicyError) as tree:
        _run(proxy, lambda source: source.list_entries(None))
    assert (tree.value.code, tree.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)

    class BrokenTokens:
        async def get_installation_access_token(self, installation_external_id: int) -> str:
            raise ValueError("GitHub token response is not valid")

    async def with_broken_tokens() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(proxy), base_url="https://api.github.test"
        ) as client:
            source = GitHubPolicyDocumentSource(
                client=client,
                token_provider=BrokenTokens(),
                locator=_LOCATOR,
                checkpoint=_checkpoint,
                policy_base_sha=_BASE,
            )
            await source.list_entries(None)

    with pytest.raises(PolicyError) as token:
        asyncio.run(with_broken_tokens())
    assert (token.value.code, token.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)

    # A proxy page instead of the raw blob: its bytes are not the requested blob.
    with pytest.raises(PolicyError) as blob:
        _run(proxy, lambda source: source.read_blob(_AGENTS))
    assert (blob.value.code, blob.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)


def test_unreadable_tree_listing_is_a_retryable_failure() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [{"path": "AGENTS.md"}]})

    with pytest.raises(PolicyError) as caught:
        _run(broken, lambda source: source.list_entries(None))
    assert (caught.value.code, caught.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)


@pytest.mark.parametrize(
    "entries",
    [
        [_item(".review/rules.md", _RULES)],
        [_item(".", _AGENTS)],
        [_item("..", _AGENTS)],
        [_item(".review", _REVIEW_TREE, mode="040000")],
        [_item(".review", _REVIEW_TREE, type_="tree", mode="100644")],
        [_item("sub", "8" * 40, type_="commit", mode="100644")],
        [{"path": "AGENTS.md", "type": "blob", "sha": _AGENTS, "size": 9}],
        [_item(".review", _REVIEW_TREE, type_="tree", mode="040000"), _item(".review", _RULES)],
    ],
    ids=[
        "recursive-path",
        "dot-path",
        "dotdot-path",
        "inconsistent-blob-mode",
        "inconsistent-tree-mode",
        "inconsistent-commit-mode",
        "missing-mode",
        "duplicate-name",
    ],
)
@pytest.mark.parametrize("tree", ["root", "review"])
def test_ambiguous_tree_listing_is_unavailable_and_never_missing(
    entries: list[dict[str, Any]], tree: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if tree == "review" and request.url.path.endswith(f"/git/trees/{_BASE}"):
            # A sound root listing: only the `.review` listing is ambiguous.
            root = [_item(".review", _REVIEW_TREE, type_="tree", mode="040000")]
            return httpx.Response(200, json={"tree": root, "truncated": False})
        return httpx.Response(200, json={"tree": entries, "truncated": False})

    with pytest.raises(PolicyError) as caught:
        _run(
            respond,
            lambda source: LoadPolicyDocuments(source, policy_base_sha=_BASE).execute(),
        )
    assert (caught.value.code, caught.value.retryable) == (
        PolicyErrorCode.REVISION_UNAVAILABLE,
        False,
    )
    assert caught.value.policy_base_sha == _BASE


@pytest.mark.parametrize(
    ("retry_after", "delay"),
    [
        ("1" * 5000, timedelta(days=1)),
        ("0" * 5000 + "120", timedelta(seconds=120)),
        ("9" * 30, timedelta(days=1)),
        ("soon", None),
        ("-1", None),
        ("120", timedelta(seconds=120)),
        ("86401", timedelta(days=1)),
    ],
    ids=[
        "over-int-limit",
        "leading-zeros-over-int-limit",
        "over-timedelta-limit",
        "word",
        "negative",
        "seconds",
        "cap-one-day",
    ],
)
def test_policy_source_bounds_untrusted_retry_after(
    retry_after: str, delay: timedelta | None
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, headers={"Retry-After": retry_after})

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.list_entries(None))

    assert caught.value.code is PolicyErrorCode.RATE_LIMITED
    assert caught.value.retry_after == delay


@pytest.mark.parametrize(
    ("status", "retry_after", "code", "delay"),
    [
        (
            403,
            "Tue, 27 Oct 2026 07:02:00 GMT",
            PolicyErrorCode.RATE_LIMITED,
            timedelta(seconds=120),
        ),
        (
            429,
            "Tue, 27 Oct 2026 07:02:00 GMT",
            PolicyErrorCode.RATE_LIMITED,
            timedelta(seconds=120),
        ),
        # Already past at the response Date: no wait, and still a limit, not a lost access.
        (403, "Tue, 27 Oct 2026 06:59:00 GMT", PolicyErrorCode.RATE_LIMITED, timedelta(0)),
        (403, "soon", PolicyErrorCode.ACCESS_DENIED, None),
        (502, "30", PolicyErrorCode.FETCH_TRANSIENT, timedelta(seconds=30)),
        (
            503,
            "Tue, 27 Oct 2026 07:02:00 GMT",
            PolicyErrorCode.FETCH_TRANSIENT,
            timedelta(seconds=120),
        ),
        (503, "soon", PolicyErrorCode.FETCH_TRANSIENT, None),
    ],
    ids=[
        "403-http-date",
        "429-http-date",
        "403-past-http-date",
        "403-word",
        "502-seconds",
        "503-http-date",
        "503-word",
    ],
)
def test_policy_source_reads_retry_after_as_seconds_or_an_http_date(
    status: int, retry_after: str, code: PolicyErrorCode, delay: timedelta | None
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        headers = {"Retry-After": retry_after, "Date": "Tue, 27 Oct 2026 07:00:00 GMT"}
        return httpx.Response(status, headers=headers)

    with pytest.raises(PolicyError) as caught:
        _run(respond, lambda source: source.list_entries(None))

    assert (caught.value.code, caught.value.retry_after) == (code, delay)


# The message GitHub sends with a secondary rate limit; only its "secondary rate limit" counts.
_SECONDARY_LIMIT = {
    "message": "You have exceeded a secondary rate limit. Please wait a few minutes before you "
    "try again.",
    "documentation_url": "https://docs.github.com/rest/using-the-rest-api/rate-limits-for-the-rest-api",
}
_NO_ACCESS = {"message": "Resource not accessible by integration"}


def _read(target: str) -> Any:
    if target == "tree":
        return lambda source: source.list_entries(None)
    return lambda source: source.read_blob(_AGENTS)


@pytest.mark.parametrize("target", ["tree", "blob"])
@pytest.mark.parametrize(
    ("status", "headers", "body", "code", "delay"),
    [
        (403, {}, _SECONDARY_LIMIT, PolicyErrorCode.RATE_LIMITED, timedelta(seconds=60)),
        (429, {}, _SECONDARY_LIMIT, PolicyErrorCode.RATE_LIMITED, timedelta(seconds=60)),
        (
            403,
            {},
            {"message": "SECONDARY RATE LIMIT exceeded"},
            PolicyErrorCode.RATE_LIMITED,
            timedelta(seconds=60),
        ),
        # The headers of a limit decide first; the message only stands in for them.
        (
            403,
            {"Retry-After": "30"},
            _SECONDARY_LIMIT,
            PolicyErrorCode.RATE_LIMITED,
            timedelta(seconds=30),
        ),
        (403, {"X-RateLimit-Remaining": "0"}, _SECONDARY_LIMIT, PolicyErrorCode.RATE_LIMITED, None),
        # An unreadable Retry-After gives no wait, so the minute of the message applies.
        (
            403,
            {"Retry-After": "soon"},
            _SECONDARY_LIMIT,
            PolicyErrorCode.RATE_LIMITED,
            timedelta(seconds=60),
        ),
        (403, {}, _NO_ACCESS, PolicyErrorCode.ACCESS_DENIED, None),
        # As before #168 part 6: a 429 is a limit, yet without headers it names no wait.
        (429, {}, _NO_ACCESS, PolicyErrorCode.RATE_LIMITED, None),
        # Only a 403 or a 429 can be a secondary rate limit.
        (404, {}, _SECONDARY_LIMIT, PolicyErrorCode.REVISION_UNAVAILABLE, None),
        (502, {}, _SECONDARY_LIMIT, PolicyErrorCode.FETCH_TRANSIENT, None),
        # The phrase counts only as the JSON message.
        (403, {}, "secondary rate limit", PolicyErrorCode.ACCESS_DENIED, None),
        (
            403,
            {},
            {"documentation_url": "secondary rate limit"},
            PolicyErrorCode.ACCESS_DENIED,
            None,
        ),
    ],
    ids=[
        "403-marker",
        "429-marker",
        "403-marker-upper-case",
        "403-marker-retry-after",
        "403-marker-remaining-0",
        "403-marker-unreadable-retry-after",
        "403-other-message",
        "429-other-message",
        "404-marker",
        "502-marker",
        "403-plain-text",
        "403-marker-outside-message",
    ],
)
def test_secondary_rate_limit_without_headers_waits_a_minute(
    target: str,
    status: int,
    headers: dict[str, str],
    body: object,
    code: PolicyErrorCode,
    delay: timedelta | None,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if isinstance(body, str):
            return httpx.Response(status, headers=headers, text=body)
        return httpx.Response(status, headers=headers, json=body)

    with pytest.raises(PolicyError) as caught:
        _run(respond, _read(target))

    assert (caught.value.code, caught.value.retry_after) == (code, delay)
    if delay == timedelta(seconds=60):
        # A minute is shorter than the 2m queue, which a rate limit takes anyway.
        assert RetryDelays().for_failure(1, caught.value) == ("2m", timedelta(minutes=2))


def _padded_secondary_limit(size: int) -> bytes:
    """The secondary-limit JSON, led by blanks to ``size`` bytes: cut short, it is no JSON."""
    message = json.dumps(_SECONDARY_LIMIT).encode()
    return b" " * (size - len(message)) + message


@pytest.mark.parametrize(
    ("size", "code"),
    [(4096, PolicyErrorCode.RATE_LIMITED), (4097, PolicyErrorCode.ACCESS_DENIED)],
    ids=["4096-bytes", "4097-bytes"],
)
def test_error_body_of_a_policy_blob_is_read_up_to_4_kib(size: int, code: PolicyErrorCode) -> None:
    body = _Chunks(_padded_secondary_limit(size), chunk=1024)

    with pytest.raises(PolicyError) as caught:
        _run(lambda request: httpx.Response(403, stream=body), _read("blob"))

    assert caught.value.code is code
    assert body.sent == 4096


def test_long_error_body_of_a_policy_blob_stops_the_stream() -> None:
    body = _Chunks(_padded_secondary_limit(1_048_576), chunk=1000)

    with pytest.raises(PolicyError) as caught:
        _run(lambda request: httpx.Response(403, stream=body), _read("blob"))

    assert caught.value.code is PolicyErrorCode.ACCESS_DENIED
    # The read stops with the first chunk that reaches 4 KiB: five chunks of 1000 bytes.
    assert body.sent == 5000
    # Only the first 4 KiB of them are kept with the failure.
    assert isinstance(caught.value.__cause__, StreamedStatusError)
    assert len(caught.value.__cause__.body) == 4096


@pytest.mark.parametrize(
    ("size", "code"),
    [(4096, PolicyErrorCode.RATE_LIMITED), (4097, PolicyErrorCode.ACCESS_DENIED)],
    ids=["4096-bytes", "4097-bytes"],
)
def test_error_body_of_a_tree_listing_counts_up_to_4_kib(size: int, code: PolicyErrorCode) -> None:
    # A plain request reads the whole body; only its first 4 KiB count, as with a stream.
    body = _padded_secondary_limit(size)

    with pytest.raises(PolicyError) as caught:
        _run(lambda request: httpx.Response(403, content=body), _read("tree"))

    assert caught.value.code is code


def test_encoded_error_body_of_a_policy_blob_is_not_decoded() -> None:
    # The read asks for identity: a gzip body could expand far past 4 KiB once decoded.
    encoded = gzip.compress(json.dumps(_SECONDARY_LIMIT).encode())
    body = _Chunks(encoded)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Content-Encoding": "gzip"}, stream=body)

    with pytest.raises(PolicyError) as caught:
        _run(respond, _read("blob"))

    assert caught.value.code is PolicyErrorCode.ACCESS_DENIED
    assert body.sent == 0


class _BrokenChunks(httpx.AsyncByteStream):
    """One chunk of a body, then a read timeout."""

    def __init__(self, first: bytes) -> None:
        self._first = first

    async def __aiter__(self) -> AsyncIterator[bytes]:
        yield self._first
        raise httpx.ReadTimeout("body cut short")


@pytest.mark.parametrize(
    ("status", "code"),
    [(403, PolicyErrorCode.ACCESS_DENIED), (429, PolicyErrorCode.RATE_LIMITED)],
)
def test_error_body_cut_short_keeps_the_status_verdict(status: int, code: PolicyErrorCode) -> None:
    # The status is the answer; a body that fails to arrive only lacks the marker.
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, stream=_BrokenChunks(b'{"message": "secondary'))

    with pytest.raises(PolicyError) as caught:
        _run(respond, _read("blob"))

    assert (caught.value.code, caught.value.retry_after) == (code, None)


def test_default_branch_head_reads_a_secondary_rate_limit_without_headers() -> None:
    # The rules status resolves the head with plain requests; their body is read too.
    target = RulesRefreshTarget(
        UUID(int=1),
        17,
        "octo/repo",
        "main",
        datetime(2026, 10, 27, tzinfo=UTC),
        datetime(2026, 10, 27, 0, 5, tzinfo=UTC),
    )

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json=_SECONDARY_LIMIT)

    async def resolve() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            heads = GitHubDefaultBranchHeadSource(
                client=client, token_provider=StaticGitHubInstallationAccessTokenProvider("token")
            )
            return await heads.resolve(target)

    with pytest.raises(PolicyError) as caught:
        asyncio.run(resolve())

    assert (caught.value.code, caught.value.retry_after) == (
        PolicyErrorCode.RATE_LIMITED,
        timedelta(seconds=60),
    )
    assert caught.value.policy_base_sha is None
