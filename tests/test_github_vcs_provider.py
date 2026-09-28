"""GitHub App VCS adapter reads complete PR files and immutable blob content."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest

from app.modules.reviews.application.vcs_diff import (
    BlobTooLargeError,
    FetchVcsReviewInput,
    OmissionReason,
    PullRequestLocator,
)
from app.modules.reviews.infrastructure.github_vcs import HttpGitHubVcsProvider

_HEAD = "a" * 40
_BASE = "b" * 40
_BLOB = "c" * 40


@dataclass
class Tokens:
    calls: int = 0

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == 17
        self.calls += 1
        return "installation-token"


def _pull_payload(*, changed_files: int = 101, head_sha: str = _HEAD) -> dict[str, object]:
    return {
        "id": 901,
        "number": 7,
        "title": "Fix parser",
        "body": "Review me",
        "user": {"login": "octo"},
        "head": {"ref": "feature", "sha": head_sha, "repo": {"full_name": "fork/repo"}},
        "base": {"ref": "main", "sha": _BASE, "repo": {"full_name": "octo/repo"}},
        "labels": [{"name": "backend"}],
        "draft": False,
        "changed_files": changed_files,
        "additions": 102,
        "deletions": 1,
    }


def _file(index: int, *, status: str = "modified") -> dict[str, object]:
    return {
        "filename": f"src/file{index}.py",
        "status": status,
        "sha": _BLOB,
        "previous_filename": "src/old.py" if status == "renamed" else None,
        "additions": 1,
        "deletions": 1,
        "changes": 2,
        "patch": "@@ -1 +1 @@\n-old\n+new",
    }


def test_provider_paginates_101_files_at_100_and_reads_blob_by_sha() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(str(request.url))
        assert request.headers["Authorization"] == "Bearer installation-token"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull_payload())
        if request.url.path.endswith("/pulls/7/files"):
            assert request.url.params["per_page"] == "100"
            page = request.url.params["page"]
            return httpx.Response(
                200,
                json=[_file(i) for i in range(100)]
                if page == "1"
                else [_file(100, status="renamed")],
            )
        if request.url.path.endswith(f"/git/blobs/{_BLOB}"):
            return httpx.Response(
                200,
                json={
                    "sha": _BLOB,
                    "encoding": "base64",
                    "content": base64.b64encode(b"immutable\ncontent\n").decode(),
                    "size": 18,
                },
            )
        raise AssertionError(f"unexpected GitHub request: {request.url}")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            locator = PullRequestLocator(17, "octo/repo", 7)
            pr = await provider.get_pull_request(locator)
            files = await provider.get_diff(pr)
            blob = await provider.get_blob(locator, _BLOB)
        assert pr.head_sha == _HEAD
        assert pr.base_sha == _BASE
        assert pr.meta.files_changed == 101
        assert pr.meta.labels == ("backend",)
        assert pr.meta.is_fork is True
        assert len(files) == 101
        assert files[100].status == "renamed"
        assert files[100].previous_filename == "src/old.py"
        assert files[100].blob_sha == _BLOB
        assert blob == b"immutable\ncontent\n"

    asyncio.run(exercise())
    assert paths == [
        "https://api.github.test/repos/octo/repo/pulls/7",
        "https://api.github.test/repos/octo/repo/pulls/7/files?per_page=100&page=1",
        "https://api.github.test/repos/octo/repo/pulls/7/files?per_page=100&page=2",
        "https://api.github.test/repos/octo/repo/pulls/7",
        f"https://api.github.test/repos/octo/repo/git/blobs/{_BLOB}",
    ]


def test_provider_rejects_pr_exceeding_github_3000_file_cap() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/pulls/7")
        return httpx.Response(200, json=_pull_payload(changed_files=3001))

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            pr = await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))
            with pytest.raises(ValueError, match="3000"):
                await provider.get_diff(pr)

    asyncio.run(exercise())


def test_provider_rejects_overfull_page_even_when_pr_count_matches() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull_payload(changed_files=101))
        assert request.url.params["per_page"] == "100"
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json=[_file(i) for i in range(101)])

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            pr = await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))
            with pytest.raises(ValueError, match="page exceeded 100"):
                await provider.get_diff(pr)

    asyncio.run(exercise())


def test_provider_accepts_exactly_3000_files_without_requesting_page_31() -> None:
    pages: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull_payload(changed_files=3000))
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=[_file(i) for i in range((page - 1) * 100, page * 100)])

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            pr = await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))
            files = await provider.get_diff(pr)
        assert len(files) == 3000
        assert files[-1].filename == "src/file2999.py"

    asyncio.run(exercise())
    assert pages == list(range(1, 31))


def test_removed_file_has_no_new_blob_and_missing_patch_is_not_called_binary() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull_payload(changed_files=1))
        if request.url.path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        **_file(1, status="removed"),
                        "patch": None,
                    }
                ],
            )
        raise AssertionError("removed file must not request a new-side blob")

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            result = await FetchVcsReviewInput(
                HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            ).execute(PullRequestLocator(17, "octo/repo", 7), _HEAD)
        assert result.files[0].blob_sha is None
        assert result.changed_files == ()
        assert result.omissions[0].reason == OmissionReason.MISSING_PATCH

    asyncio.run(exercise())


def test_provider_rejects_early_pagination_and_changed_head() -> None:
    async def exercise(*, stale_head: bool) -> None:
        pr_calls = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal pr_calls
            if request.url.path.endswith("/pulls/7"):
                pr_calls += 1
                return httpx.Response(
                    200,
                    json=_pull_payload(head_sha="d" * 40 if stale_head and pr_calls > 1 else _HEAD),
                )
            if request.url.path.endswith("/files"):
                if stale_head:
                    page = request.url.params["page"]
                    return httpx.Response(
                        200,
                        json=[_file(i) for i in range(100)] if page == "1" else [_file(100)],
                    )
                return httpx.Response(200, json=[_file(i) for i in range(80)])
            raise AssertionError("unexpected request")

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            pr = await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))
            with pytest.raises(ValueError, match="changed" if stale_head else "pagination"):
                await provider.get_diff(pr)
            assert pr_calls == (2 if stale_head else 1)

    asyncio.run(exercise(stale_head=False))
    asyncio.run(exercise(stale_head=True))


def test_provider_rejects_duplicate_filename_across_pages() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json=_pull_payload())
        page = request.url.params["page"]
        return httpx.Response(
            200,
            json=[_file(i) for i in range(100)] if page == "1" else [_file(0)],
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            pr = await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))
            with pytest.raises(ValueError, match="repeated a filename"):
                await provider.get_diff(pr)

    asyncio.run(exercise())


def test_http_rate_limit_and_invalid_blob_fail_closed() -> None:
    def rate_limit(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"Retry-After": "60"}, json={"message": "rate limited"})

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(rate_limit), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            with pytest.raises(httpx.HTTPStatusError):
                await provider.get_pull_request(PullRequestLocator(17, "octo/repo", 7))

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={"sha": _BLOB, "encoding": "base64", "content": "%%%", "size": 4},
                )
            ),
            base_url="https://api.github.test",
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            with pytest.raises(ValueError, match="base64"):
                await provider.get_blob(PullRequestLocator(17, "octo/repo", 7), _BLOB)

    asyncio.run(exercise())


def test_blob_retrieval_stops_before_consuming_an_oversized_response() -> None:
    @dataclass
    class BlobStream(httpx.AsyncByteStream):
        chunks_read: int = 0

        async def __aiter__(self) -> AsyncIterator[bytes]:
            for _ in range(100):
                self.chunks_read += 1
                yield b"x" * 100_000

    stream = BlobStream()

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/git/blobs/{_BLOB}")
        return httpx.Response(200, stream=stream)

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            with pytest.raises(BlobTooLargeError):
                await provider.get_blob(PullRequestLocator(17, "octo/repo", 7), _BLOB)

    asyncio.run(exercise())
    assert stream.chunks_read < 100


def test_blob_declared_over_one_mebibyte_is_rejected_before_decoding() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"sha": _BLOB, "encoding": "base64", "content": "", "size": 1_048_577},
        )

    async def exercise() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            provider = HttpGitHubVcsProvider(client=client, token_provider=Tokens())
            with pytest.raises(BlobTooLargeError):
                await provider.get_blob(PullRequestLocator(17, "octo/repo", 7), _BLOB)

    asyncio.run(exercise())
