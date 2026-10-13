"""GitHub revision reads for context: the recursive tree and its completeness."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

import httpx

from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    StaticGitHubInstallationAccessTokenProvider,
)
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.github_revision_source import GitHubRevisionSource

_HEAD = "a" * 40
_LOCATOR = PullRequestLocator(17, "octo/repo", 7)


@dataclass
class Blobs:
    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes:
        assert locator == _LOCATOR
        return sha.encode()


@dataclass
class Trees:
    stored: dict[str, RevisionTree] = field(default_factory=dict)

    async def get(self, revision: str) -> RevisionTree | None:
        return self.stored.get(revision)

    async def put(self, tree: RevisionTree) -> None:
        self.stored[tree.revision] = tree


def _run(truncated: bool, cache: Trees | None) -> tuple[RevisionTree, RevisionTree, bytes, int]:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        assert request.url.path == f"/repos/octo/repo/git/trees/{_HEAD}"
        assert request.url.params["recursive"] == "1"
        return httpx.Response(
            200,
            json={
                "tree": [
                    {"path": "src", "type": "tree", "sha": "1" * 40},
                    {"path": "src/a.py", "type": "blob", "sha": "2" * 40},
                    {"path": "vendor", "type": "commit", "sha": "3" * 40},
                ],
                "truncated": truncated,
            },
        )

    async def exercise() -> tuple[RevisionTree, RevisionTree, bytes, int]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(respond), base_url="https://api.github.test"
        ) as client:
            source = GitHubRevisionSource(
                client=client,
                token_provider=StaticGitHubInstallationAccessTokenProvider("token"),
                vcs=Blobs(),  # type: ignore[arg-type]
                locator=_LOCATOR,
                tree_cache=cache,
            )
            first = await source.tree(_HEAD)
            second = await source.tree(_HEAD)
            return first, second, await source.read_blob("2" * 40), len(requests)

    return asyncio.run(exercise())


def test_tree_lists_files_only_and_a_complete_tree_is_cached() -> None:
    cache = Trees()

    first, second, blob, requests = _run(False, cache)

    assert first == RevisionTree(_HEAD, {"src/a.py": "2" * 40}, complete=True)
    assert second == first
    assert requests == 1
    assert blob == b"2" * 40


def test_truncated_tree_is_incomplete_and_is_not_cached() -> None:
    cache = Trees()

    first, _, _, requests = _run(True, cache)

    assert first.complete is False
    assert cache.stored == {}
    assert requests == 2
