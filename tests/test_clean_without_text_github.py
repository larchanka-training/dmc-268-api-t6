"""GitHub proves regular-file mode at the immutable head before empty exclusion."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx
import pytest

from app.modules.integrations.webhooks.application.installation_access_token import (
    InstallationAccessTokenError,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import (
    FetchedVcsReviewInput,
    FetchVcsReviewInput,
    PullRequestLocator,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider
from tests.installation_onboarding_fakes import provider_checkpoint
from tests.vcs_facts import EMPTY_BLOB_SHA as _EMPTY

_HEAD = "a" * 40
_BASE = "b" * 40

_PREFIX = "/repos/octo/repo"
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)


@dataclass
class Tokens:
    calls: int = 0
    fail_after: int | None = None

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise InstallationAccessTokenError("token refresh unavailable", transient=True)
        return "installation-token"


@dataclass
class TreeBody(httpx.AsyncByteStream):
    payload: bytes
    bytes_read: int = 0
    closed: bool = False

    async def aclose(self) -> None:
        self.closed = True

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for offset in range(0, len(self.payload), 65_536):
            chunk = self.payload[offset : offset + 65_536]
            self.bytes_read += len(chunk)
            yield chunk


@dataclass
class GitHubWire:
    mode: str = "100644"
    entry_type: str = "blob"
    entry_sha: str = _EMPTY
    entry_size: int = 0
    truncated: bool = False
    duplicate_entry: bool = False
    tree_status: int = 200
    tree_bytes: int | None = None
    declare_tree_length: bool = True
    tree_body: TreeBody | None = None
    entry_path: str | None = None
    tree_encoding: str | None = None
    tree_error: Exception | None = None
    paths: tuple[str, ...] = ("pkg/__init__.py",)
    additions: int = 0
    tree_requests: list[str] = field(default_factory=list)
    blob_requests: list[str] = field(default_factory=list)

    def respond(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{_PREFIX}/pulls/7":
            return httpx.Response(
                200,
                json={
                    "id": 1,
                    "number": 7,
                    "title": "Add package",
                    "body": None,
                    "user": {"login": "octo"},
                    "head": {"ref": "feature", "sha": _HEAD, "repo": {"full_name": "octo/repo"}},
                    "base": {"ref": "main", "sha": _BASE, "repo": {"full_name": "octo/repo"}},
                    "labels": [],
                    "draft": False,
                    "changed_files": len(self.paths),
                    "additions": self.additions,
                    "deletions": 0,
                },
            )
        if request.url.path == f"{_PREFIX}/pulls/7/files":
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": path,
                        "status": "added",
                        "sha": _EMPTY,
                        "additions": self.additions,
                        "deletions": 0,
                        "changes": self.additions,
                    }
                    for path in self.paths
                ],
            )
        if request.url.path == f"{_PREFIX}/git/trees/{_HEAD}":
            self.tree_requests.append(str(request.url))
            assert request.headers["Accept-Encoding"] == "identity"
            if self.tree_error is not None:
                raise self.tree_error
            entries = [
                {
                    "path": self.entry_path or path,
                    "sha": self.entry_sha,
                    "type": self.entry_type,
                    "mode": self.mode,
                    "size": self.entry_size,
                }
                for path in self.paths
            ]
            if self.duplicate_entry:
                entries += entries[:1]
            if self.tree_bytes is not None:
                payload = json.dumps({"truncated": self.truncated, "tree": entries}).encode()
                payload = (
                    b""
                    if self.tree_bytes == 0
                    else payload + b" " * (self.tree_bytes - len(payload))
                )
                assert len(payload) == self.tree_bytes
                self.tree_body = TreeBody(payload)
                return httpx.Response(
                    200,
                    stream=self.tree_body,
                    headers={"Content-Length": str(len(payload))}
                    if self.declare_tree_length
                    else {},
                )
            return httpx.Response(
                self.tree_status,
                json={
                    "truncated": self.truncated,
                    "tree": entries,
                },
                headers={}
                if self.tree_encoding is None
                else {
                    "Content-Encoding": self.tree_encoding,
                },
            )
        if request.url.path == f"{_PREFIX}/git/blobs/{_EMPTY}":
            self.blob_requests.append(str(request.url))
            return httpx.Response(
                200,
                json={
                    "sha": _EMPTY,
                    "encoding": "base64",
                    "content": "",
                    "size": 0,
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")


async def fetch(wire: GitHubWire, tokens: Tokens | None = None) -> FetchedVcsReviewInput:
    async with httpx.AsyncClient(
        base_url="https://api.github.test", transport=httpx.MockTransport(wire.respond)
    ) as client:
        provider = HttpGitHubVcsProvider(client=client, token_provider=tokens or Tokens())
        return await FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
            _LOCATOR, _HEAD
        )


def test_github_fetch_proves_empty_regular_file_from_one_pinned_tree_request() -> None:
    async def scenario() -> None:
        wire = GitHubWire()
        async with httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(wire.respond)
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            fetched = await FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
                _LOCATOR, _HEAD
            )

        assert [item.reason.value for item in fetched.omissions] == ["empty_file"]
        assert fetched.files[0].git_mode == "100644"
        assert wire.tree_requests == [
            "https://api.github.test/repos/octo/repo/git/trees/"
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa?recursive=1"
        ]
        assert wire.blob_requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "wire",
    [
        GitHubWire(mode="120000"),
        GitHubWire(mode="160000", entry_type="commit"),
        GitHubWire(mode="100644", entry_type="commit"),
        GitHubWire(entry_sha="c" * 40),
        GitHubWire(entry_size=1),
        GitHubWire(entry_path="other/__init__.py"),
        GitHubWire(entry_path="PKG/__init__.py"),
        GitHubWire(truncated=True),
        GitHubWire(duplicate_entry=True),
        GitHubWire(tree_status=404),
        GitHubWire(tree_bytes=1_500_001),
        GitHubWire(tree_bytes=0),
        GitHubWire(tree_encoding="br"),
    ],
)
def test_tree_cannot_prove_empty_with_unsupported_or_conflicting_evidence(
    wire: GitHubWire,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(fetch(wire))

    assert [item.reason.value for item in result.omissions] == ["missing_patch"]
    assert result.files[0].git_mode is None
    assert len(wire.tree_requests) == 1
    assert (
        "GitHub empty-file mode proof unavailable repository=octo/repo pr_number=7" in caplog.text
    )


def test_one_metadata_request_proves_multiple_empty_files() -> None:
    wire = GitHubWire(paths=("pkg/__init__.py", "other/__init__.py"))
    result = asyncio.run(fetch(wire))

    assert [item.reason.value for item in result.omissions] == ["empty_file", "empty_file"]
    assert len(wire.tree_requests) == 1
    assert wire.blob_requests == []


def test_no_tree_request_when_no_empty_addition_candidate_exists() -> None:
    wire = GitHubWire(additions=1)
    result = asyncio.run(fetch(wire))

    assert [item.reason.value for item in result.omissions] == ["missing_patch"]
    assert wire.tree_requests == []


def test_optional_mode_proof_does_not_swallow_cancellation() -> None:
    wire = GitHubWire(tree_error=RunCancelled("run_inactive"))
    with pytest.raises(RunCancelled, match="run_inactive"):
        asyncio.run(fetch(wire))


def test_generated_empty_addition_needs_no_mode_proof() -> None:
    wire = GitHubWire(paths=("dist/empty.js",))
    result = asyncio.run(fetch(wire))

    assert [item.reason.value for item in result.omissions] == ["generated"]
    assert wire.tree_requests == []
    assert wire.blob_requests == []


def test_optional_tree_proof_reuses_diff_token_when_refresh_would_fail() -> None:
    wire = GitHubWire()
    tokens = Tokens(fail_after=3)

    result = asyncio.run(fetch(wire, tokens))

    assert [item.reason.value for item in result.omissions] == ["empty_file"]
    assert tokens.calls == 3
    assert len(wire.tree_requests) == 1
    assert wire.blob_requests == []


@pytest.mark.parametrize("declare_length", [True, False], ids=["content-length", "streamed"])
def test_valid_tree_at_exact_metadata_budget_proves_empty(declare_length: bool) -> None:
    wire = GitHubWire(tree_bytes=1_500_000, declare_tree_length=declare_length)

    result = asyncio.run(fetch(wire))

    assert wire.tree_body is not None
    assert len(wire.tree_body.payload) == 1_500_000
    assert json.loads(wire.tree_body.payload)["tree"][0]["sha"] == _EMPTY
    assert [item.reason.value for item in result.omissions] == ["empty_file"]
    assert result.files[0].git_mode == "100644"
    assert len(wire.tree_requests) == 1
    assert wire.blob_requests == []


@pytest.mark.parametrize("declare_length", [True, False], ids=["content-length", "streamed"])
def test_valid_tree_over_metadata_budget_fails_for_cap(
    declare_length: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    wire = GitHubWire(tree_bytes=1_500_001, declare_tree_length=declare_length)
    tokens = Tokens()

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(fetch(wire, tokens))

    assert wire.tree_body is not None
    assert len(wire.tree_body.payload) == 1_500_001
    assert json.loads(wire.tree_body.payload)["tree"][0]["sha"] == _EMPTY
    assert [item.reason.value for item in result.omissions] == ["missing_patch"]
    assert result.files[0].git_mode is None
    assert "ValueError: GitHub empty-file tree exceeds metadata budget" in caplog.text
    assert wire.tree_body.bytes_read == (0 if declare_length else 1_500_001)
    assert len(wire.tree_requests) == 1
    assert len(wire.blob_requests) == 1
    assert tokens.calls == 4  # Three mandatory reads and the existing missing-patch blob fallback.


def test_optional_tree_http_failure_stays_missing_patch_with_reused_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    wire = GitHubWire(tree_error=httpx.ReadError("optional tree unavailable"))
    tokens = Tokens()

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(fetch(wire, tokens))

    assert [item.reason.value for item in result.omissions] == ["missing_patch"]
    assert result.files[0].git_mode is None
    assert "optional tree unavailable" in caplog.text
    assert len(wire.tree_requests) == 1
    assert len(wire.blob_requests) == 1
    assert tokens.calls == 4


def test_huge_streamed_tree_aborts_before_unread_tail_and_closes_response() -> None:
    wire = GitHubWire(tree_bytes=12_000_000, declare_tree_length=False)
    result = asyncio.run(fetch(wire))

    assert wire.tree_body is not None
    assert len(wire.tree_body.payload) == 12_000_000
    assert 1_500_000 < wire.tree_body.bytes_read <= 1_565_536
    assert wire.tree_body.bytes_read < len(wire.tree_body.payload) // 7
    assert wire.tree_body.closed is True
    assert [item.reason.value for item in result.omissions] == ["missing_patch"]
    assert result.files[0].git_mode is None
    assert len(wire.tree_requests) == 1
    # The existing missing-patch blob fallback remains bounded and cannot prove mode.
    assert len(wire.blob_requests) == 1
