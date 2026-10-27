"""GitHub policy reads: tree entries by type and failures that are never 'missing'."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.policy_documents import LoadPolicyDocuments, PolicyEntryKind
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import DocumentPresence
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_policy_source import GitHubPolicyDocumentSource
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
