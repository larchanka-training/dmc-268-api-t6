"""GitHub reads of a repository revision for context: the file tree and blobs by SHA."""

from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import Field, TypeAdapter

from app.common.infrastructure.github_repository_path import commit_sha_segment
from app.modules.reviews.application.ast_resolution import RevisionTree
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsProvider
from app.modules.reviews.infrastructure.github_vcs import (
    _REQUEST_TIMEOUT,
    _SHA,
    InstallationTokenProvider,
    _GitHubDto,
    _headers,
    _repository_prefix,
)


class _Entry(_GitHubDto):
    path: str = Field(min_length=1)
    type: str
    sha: str = Field(pattern=_SHA)


class _Listing(_GitHubDto):
    tree: list[_Entry]
    truncated: bool = False


_LISTING = TypeAdapter(_Listing)


class TreeCache(Protocol):
    async def get(self, revision: str) -> RevisionTree | None: ...

    async def put(self, tree: RevisionTree) -> None: ...


class GitHubRevisionSource:
    """``RevisionSource`` of one repository; a truncated tree is reported as incomplete."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        vcs: VcsProvider,
        locator: PullRequestLocator,
        tree_cache: TreeCache | None = None,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._vcs = vcs
        self._locator = locator
        self._tree_cache = tree_cache

    async def tree(self, revision: str) -> RevisionTree:
        if self._tree_cache is not None:
            cached = await self._tree_cache.get(revision)
            if cached is not None:
                return cached
        token = await self._tokens.get_installation_access_token(
            self._locator.installation_external_id
        )
        response = await self._client.get(
            f"{_repository_prefix(self._locator)}/git/trees/{commit_sha_segment(revision)}",
            params={"recursive": "1"},
            headers=_headers(token),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        listing = _LISTING.validate_python(response.json())
        tree = RevisionTree(
            revision=revision,
            blob_shas={item.path: item.sha for item in listing.tree if item.type == "blob"},
            complete=not listing.truncated,
        )
        if self._tree_cache is not None and tree.complete:
            await self._tree_cache.put(tree)
        return tree

    async def read_blob(self, blob_sha: str) -> bytes:
        return await self._vcs.get_blob(self._locator, blob_sha)
