"""Context reads of one Run when GitHub returns less than asked (#52)."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.conventions import RepositoryFile
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsMetadataProvider
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource
from tests.github_run_source_fakes import BASE, HEAD, LOCATOR, Blobs, Runs


def _source(tree: dict[str, Any], blobs: Blobs) -> GitHubRunSource:
    def github(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/octo/repo/git/trees/{HEAD}"
        return httpx.Response(200, json=tree)

    return GitHubRunSource(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(github)
        ),
        token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
        vcs=cast(VcsMetadataProvider, blobs),
        runs=Runs(),
        run_id=uuid4(),
    )


def _blob(path: str, sha: str, size: int = 10) -> dict[str, Any]:
    return {"path": path, "sha": sha, "type": "blob", "size": size}


def test_truncated_tree_keeps_the_returned_part_and_warns(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = {
        "truncated": True,
        "tree": [_blob("src/app.py", "1" * 40), {"path": "src", "type": "tree", "sha": "2" * 40}],
    }

    with caplog.at_level(logging.WARNING):
        files = asyncio.run(_source(tree, Blobs()).fetch_tree(uuid4()))

    assert files == (RepositoryFile(path="src/app.py", size=10),)
    assert "is truncated" in caplog.text


def test_unreadable_or_binary_context_files_are_skipped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    tree = {
        "truncated": False,
        "tree": [
            _blob("ok.py", "1" * 40),
            _blob("logo.png", "2" * 40),
            _blob("lost.py", "3" * 40),
        ],
    }
    blobs = Blobs({"1" * 40: b"print(1)\n", "2" * 40: b"\x89PNG\xff\xfe"})
    source = _source(tree, blobs)

    async def scenario() -> tuple[RepositoryFile, ...]:
        await source.fetch_tree(uuid4())
        return await source.fetch_files(uuid4(), ("ok.py", "logo.png", "lost.py", "absent.py"))

    with caplog.at_level(logging.WARNING):
        files = asyncio.run(scenario())

    assert files == (RepositoryFile(path="ok.py", size=9, content="print(1)\n"),)
    assert "Skipping context file logo.png" in caplog.text
    assert "Skipping context file lost.py" in caplog.text


def test_rate_limited_context_blob_is_skipped_with_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class LimitedBlobs(Blobs):
        async def get_blob(
            self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
        ) -> bytes:
            if sha == "2" * 40:
                raise RunFailure("diff_fetch_failed", retry_after=None, rate_limited=True)
            return await super().get_blob(locator, sha, checkpoint=checkpoint)

    tree = {"tree": [_blob("ok.py", "1" * 40), _blob("limited.py", "2" * 40)]}
    source = _source(tree, LimitedBlobs({"1" * 40: b"print(1)\n"}))

    async def scenario() -> tuple[RepositoryFile, ...]:
        await source.fetch_tree(uuid4())
        return await source.fetch_files(uuid4(), ("ok.py", "limited.py"))

    with caplog.at_level(logging.WARNING):
        files = asyncio.run(scenario())

    assert files == (RepositoryFile(path="ok.py", size=9, content="print(1)\n"),)
    assert "Skipping context file limited.py" in caplog.text


def test_conventions_tree_and_files_come_from_the_base_revision_when_asked() -> None:
    requested: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(200, json={"tree": [_blob("src/app.py", "1" * 40)]})

    def source(*, from_base: bool) -> GitHubRunSource:
        return GitHubRunSource(
            client=httpx.AsyncClient(
                base_url="https://api.github.test", transport=httpx.MockTransport(github)
            ),
            token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
            vcs=cast(VcsMetadataProvider, Blobs({"1" * 40: b"base content"})),
            runs=Runs(),
            run_id=uuid4(),
            conventions_from_base=from_base,
        )

    async def read(from_base: bool) -> tuple[RepositoryFile, ...]:
        reader = source(from_base=from_base)
        await reader.fetch_tree(uuid4())
        return await reader.fetch_files(uuid4(), ("src/app.py",))

    files = asyncio.run(read(True))
    asyncio.run(read(False))

    # Shared conventions never see the tree or the code of the pull request head.
    assert requested == [
        f"/repos/octo/repo/git/trees/{BASE}",
        f"/repos/octo/repo/git/trees/{HEAD}",
    ]
    assert files[0].content == "base content"


def test_get_pull_request_meta_makes_zero_compare_requests() -> None:
    calls: list[str] = []

    def github(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if "/commits" in request.url.path:
            return httpx.Response(200, json=[{"commit": {"message": "commit msg"}}])
        if "/compare" in request.url.path:
            raise AssertionError("unexpected /compare request!")
        return httpx.Response(200, json={})

    from app.modules.reviews.application.prompt_builder import PullRequestMeta
    from app.modules.reviews.application.vcs_diff import VcsPullRequest

    class FakeVcs:
        async def get_pull_request(
            self, locator: object, *, checkpoint: object = None
        ) -> VcsPullRequest:
            return VcsPullRequest(
                locator=LOCATOR,
                head_sha=HEAD,
                base_sha=BASE,
                meta=PullRequestMeta(
                    "Title", None, "octo", "feature", "main", (), 1, 1, 0, False, False
                ),
            )

        async def get_commit_messages(
            self, locator: object, *, checkpoint: object = None
        ) -> object:
            from app.modules.reviews.application.vcs_diff import CommitMessagePrefix

            return CommitMessagePrefix(("commit msg",), False)

    source = GitHubRunSource(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(github)
        ),
        token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
        vcs=cast(VcsMetadataProvider, FakeVcs()),
        runs=Runs(),
        run_id=uuid4(),
    )
    meta = asyncio.run(source.get_pull_request_meta(uuid4()))
    assert meta is not None
    assert not any("/compare" in call for call in calls)
    assert meta.diff_old_sha is None
