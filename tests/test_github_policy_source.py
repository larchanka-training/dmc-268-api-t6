"""GitHub policy reads: tree entries by type and failures that are never 'missing'."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.policy_documents import LoadPolicyDocuments, PolicyEntryKind
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import DocumentPresence
from app.modules.reviews.application.vcs_diff import BlobTooLargeError, PullRequestLocator
from app.modules.reviews.infrastructure.github_policy_source import GitHubPolicyDocumentSource

_BASE = "b" * 40
_REVIEW_TREE = "1" * 40
_AGENTS = "2" * 40
_RULES = "3" * 40
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)


async def _checkpoint() -> None:
    return None


@dataclass
class Blobs:
    contents: dict[str, bytes] = field(default_factory=dict)
    error: Exception | None = None

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        await checkpoint()
        assert locator == _LOCATOR
        if self.error is not None:
            raise self.error
        return self.contents[sha]


def _item(path: str, sha: str, *, type_: str = "blob", mode: str = "100644") -> dict[str, Any]:
    return {"path": path, "mode": mode, "type": type_, "sha": sha, "size": 5}


def _run(respond: Any, blobs: Blobs, action: Any) -> Any:
    async def exercise() -> Any:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            source = GitHubPolicyDocumentSource(
                client=client,
                token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
                vcs=blobs,  # type: ignore[arg-type]
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
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    loaded = _run(
        respond,
        Blobs({_AGENTS: b"agents", _RULES: b"rules"}),
        lambda source: LoadPolicyDocuments(source, policy_base_sha=_BASE).execute(),
    )

    assert (loaded.rules.document.text, loaded.rules.document.blob_oid) == ("rules", _RULES)
    assert (loaded.agents.document.text, loaded.agents.document.blob_oid) == ("agents", _AGENTS)
    assert paths == [
        f"/repos/octo/repo/git/trees/{_BASE}",
        f"/repos/octo/repo/git/trees/{_REVIEW_TREE}",
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

    entries = _run(respond, Blobs(), lambda source: source.list_entries(None))

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

    assert _run(empty, Blobs(), load).rules.document.presence is DocumentPresence.MISSING
    with pytest.raises(PolicyError) as caught:
        _run(truncated, Blobs(), load)
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
                Blobs(),
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
        _run(respond, Blobs(), lambda source: source.list_entries(None))
    assert caught.value.retry_after is not None
    assert caught.value.retry_after.total_seconds() == 30


def test_blob_failures_are_policy_errors() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError("blob reads go through the VCS provider")

    request = httpx.Request("GET", "https://api.github.test/git/blobs/x")
    unavailable = httpx.HTTPStatusError(
        "bad gateway", request=request, response=httpx.Response(502, request=request)
    )
    for error, code in (
        (unavailable, PolicyErrorCode.FETCH_TRANSIENT),
        (BlobTooLargeError("too large"), PolicyErrorCode.DOCUMENT_TOO_LARGE),
    ):
        with pytest.raises(PolicyError) as caught:
            _run(respond, Blobs(error=error), lambda source: source.read_blob(_AGENTS))
        assert caught.value.code is code


def test_answer_that_cannot_be_decoded_is_a_policy_fetch_failure_not_an_internal_one() -> None:
    def proxy(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>proxy error</html>")

    with pytest.raises(PolicyError) as tree:
        _run(proxy, Blobs(), lambda source: source.list_entries(None))
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
                vcs=Blobs(),  # type: ignore[arg-type]
                locator=_LOCATOR,
                checkpoint=_checkpoint,
                policy_base_sha=_BASE,
            )
            await source.list_entries(None)

    with pytest.raises(PolicyError) as token:
        asyncio.run(with_broken_tokens())
    assert (token.value.code, token.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)

    # What the blob adapter raises for an answer that is not JSON or not base64.
    with pytest.raises(PolicyError) as blob:
        _run(
            proxy,
            Blobs(error=ValueError("GitHub blob response is not valid")),
            lambda source: source.read_blob(_AGENTS),
        )
    assert (blob.value.code, blob.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)


def test_unreadable_tree_listing_is_a_retryable_failure_and_a_missing_mode_is_a_file() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"tree": [{"path": "AGENTS.md"}]})

    with pytest.raises(PolicyError) as caught:
        _run(broken, Blobs(), lambda source: source.list_entries(None))
    assert (caught.value.code, caught.value.retryable) == (PolicyErrorCode.FETCH_TRANSIENT, True)

    def without_mode(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"tree": [{"path": "AGENTS.md", "type": "blob", "sha": _AGENTS, "size": 9}]}
        )

    entries = _run(without_mode, Blobs(), lambda source: source.list_entries(None))
    assert entries["AGENTS.md"].kind is PolicyEntryKind.FILE
