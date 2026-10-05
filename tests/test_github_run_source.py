"""Context reads of one Run when GitHub returns less than asked (#52)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.conventions import RepositoryFile
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsProvider
from app.modules.reviews.infrastructure.github_run_source import GitHubRunSource

HEAD = "e" * 40
BASE = "b" * 40
LOCATOR = PullRequestLocator(
    installation_external_id=17, repository_full_name="octo/repo", number=7
)


@dataclass
class Runs:
    async def get_run_vcs_input(self, run_id: UUID) -> RunVcsInput:
        return RunVcsInput(
            code_change_id=uuid4(),
            repository_id=uuid4(),
            head_sha=HEAD,
            base_sha=BASE,
            locator=LOCATOR,
        )

    async def get_run_snapshots(self, run_id: UUID) -> None:
        return None


@dataclass
class Blobs:
    """Blob contents by sha; a missing sha answers like a GitHub error."""

    contents: dict[str, bytes] = field(default_factory=dict)

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        if sha not in self.contents:
            request = httpx.Request("GET", f"https://api.github.test/git/blobs/{sha}")
            raise httpx.HTTPStatusError(
                "server error", request=request, response=httpx.Response(502, request=request)
            )
        return self.contents[sha]


def _source(tree: dict[str, Any], blobs: Blobs) -> GitHubRunSource:
    def github(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/repos/octo/repo/git/trees/{HEAD}"
        return httpx.Response(200, json=tree)

    return GitHubRunSource(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(github)
        ),
        token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
        vcs=cast(VcsProvider, blobs),
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
