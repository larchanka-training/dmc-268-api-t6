"""Pinned GitLab revision trees and cached-path HEAD revalidation."""

from __future__ import annotations

import re
from typing import Literal, Protocol

from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttpError
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree


class TreeCache(Protocol):
    async def get(self, revision: str) -> RevisionTree | None: ...
    async def put(self, tree: RevisionTree) -> None: ...


class GitLabRevisionSource:
    def __init__(
        self,
        *,
        trees: GitLabRepositoryTree,
        locator: PullRequestLocator,
        tree_cache: TreeCache | None = None,
    ) -> None:
        if locator != trees.expected_input.locator:
            raise ValueError("GitLab revision source route does not match the attempt")
        self._trees = trees
        self._cache = tree_cache
        self._paths: dict[str, tuple[str, Literal["target", "head"], str]] = {}

    async def tree(self, revision: str) -> RevisionTree:
        await self._trees.checkpoint()
        canonical = await self._trees.canonical()
        side: Literal["target", "head"] = "head" if revision == canonical.head_sha else "target"
        if revision not in {canonical.head_sha, canonical.start_sha, canonical.base_sha}:
            raise RunCancelled("superseded")
        cached = await self._cache.get(revision) if self._cache is not None else None
        await self._trees.checkpoint()
        if cached is not None:
            valid = (
                cached.complete
                and cached.revision == revision
                and all(
                    isinstance(path, str)
                    and isinstance(oid, str)
                    and path
                    and not path.startswith("/")
                    and "\\" not in path
                    and all(part not in {"", ".", ".."} for part in path.split("/"))
                    and not any(
                        ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                        for char in path
                    )
                    and len(path.encode("utf-8")) <= 4096
                    and re.fullmatch(r"[a-fA-F0-9]{40}|[a-fA-F0-9]{64}", oid) is not None
                    for path, oid in cached.blob_shas.items()
                )
            )
            if valid:
                for path, oid in cached.blob_shas.items():
                    self._paths.setdefault(oid, (revision, side, path))
                return cached
        try:
            listing = await self._trees.tree(revision, side=side)
        except GitLabRunHttpError:
            return RevisionTree(revision, {}, complete=False)
        blobs = {entry.path: entry.oid for entry in listing.entries if entry.kind == "file"}
        result = RevisionTree(revision, blobs, complete=listing.listing_complete)
        for path, oid in blobs.items():
            self._paths.setdefault(oid, (revision, side, path))
        if result.complete and self._cache is not None:
            await self._trees.checkpoint()
            await self._cache.put(result)
            await self._trees.checkpoint()
        return result

    async def read_blob(self, blob_sha: str) -> bytes:
        await self._trees.checkpoint()
        known = self._paths.get(blob_sha)
        if known is None:
            raise GitLabRunHttpError("invalid_response")
        revision, side, path = known
        await self._trees.file_metadata(path, revision, side=side, expected_oid=blob_sha)
        return await self._trees.read_blob(blob_sha)
