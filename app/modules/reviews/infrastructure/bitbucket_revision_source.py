"""Pinned Bitbucket revision trees for snapshot/v4 context."""

from __future__ import annotations

from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree


class BitbucketRevisionSource:
    def __init__(self, *, trees: BitbucketRepositoryTree, locator: PullRequestLocator) -> None:
        if locator != trees.expected_input.locator:
            raise ValueError("Bitbucket revision source route does not match the attempt")
        self._trees = trees

    async def tree(self, revision: str) -> RevisionTree:
        await self._trees.checkpoint()
        listing = await self._trees.walk(revision)
        return RevisionTree(
            revision,
            {entry.path: entry.oid for entry in listing.entries if entry.kind == "file"},
            complete=listing.complete,
        )

    async def read_blob(self, blob_sha: str) -> bytes:
        await self._trees.checkpoint()
        return await self._trees.read_file(blob_sha, max_bytes=1024 * 1024)
