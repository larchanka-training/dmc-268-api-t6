"""GitHub reads of one Run: AGENTS.md, tree and files for conventions, and PR metadata.

AGENTS.md is read at the Run's ``base_sha`` and the tree at its ``head_sha`` (SD §6.2);
file contents come from SHA-addressed blobs, never from ``contents`` by path (SD §8.3).
"""

from __future__ import annotations

import logging
from typing import Any
from uuid import UUID

import httpx

from app.common.infrastructure.github_repository_path import commit_sha_segment
from app.modules.reviews.application.conventions import RepositoryFile, RepositorySnapshot
from app.modules.reviews.application.process_run import RunVcsInput, RunVcsRepository
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import VcsProvider
from app.modules.reviews.infrastructure.github_vcs import (
    _REQUEST_TIMEOUT,
    InstallationTokenProvider,
    _headers,
    _repository_prefix,
)

_LOGGER = logging.getLogger(__name__)

AGENTS_MD = "AGENTS.md"


class GitHubRunSource:
    """``RepositoryConventionsSource`` and ``PullRequestMetaSource`` bound to one Run."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        vcs: VcsProvider,
        runs: RunVcsRepository,
        run_id: UUID,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._vcs = vcs
        self._runs = runs
        self._run_id = run_id
        self._run: RunVcsInput | None = None
        self._head_blobs: dict[str, str] = {}

    async def _input(self) -> RunVcsInput:
        if self._run is None:
            run = await self._runs.get_run_vcs_input(self._run_id)
            if run is None:
                raise LookupError(f"run {self._run_id} has no VCS input")
            self._run = run
        return self._run

    async def _tree(self, run: RunVcsInput, sha: str, *, recursive: bool) -> list[dict[str, Any]]:
        path = f"{_repository_prefix(run.locator)}/git/trees/{commit_sha_segment(sha)}"
        token = await self._tokens.get_installation_access_token(
            run.locator.installation_external_id
        )
        response = await self._client.get(
            path,
            params={"recursive": "1"} if recursive else None,
            headers=_headers(token),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("truncated"):
            # A huge monorepo: conventions use the part of the tree GitHub returned.
            _LOGGER.warning("GitHub tree %s of %s is truncated", sha, run.locator)
        return [item for item in payload.get("tree", []) if item.get("type") == "blob"]

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        run = await self._input()
        entry = next(
            (
                item
                for item in await self._tree(run, run.base_sha, recursive=False)
                if item.get("path") == AGENTS_MD
            ),
            None,
        )
        if entry is None:
            # A repository without AGENTS.md is ordinary; conventions use the code only.
            return RepositorySnapshot(content=None, sha=None)
        blob = await self._vcs.get_blob(run.locator, str(entry["sha"]))
        return RepositorySnapshot(content=blob.decode("utf-8", errors="replace"), sha=entry["sha"])

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        run = await self._input()
        entries = await self._tree(run, run.head_sha, recursive=True)
        self._head_blobs = {str(item["path"]): str(item["sha"]) for item in entries}
        return tuple(
            RepositoryFile(path=str(item["path"]), size=int(item.get("size") or 0))
            for item in entries
        )

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        run = await self._input()
        files: list[RepositoryFile] = []
        for path in paths:
            sha = self._head_blobs.get(path)
            if sha is None:
                continue
            try:
                blob = await self._vcs.get_blob(run.locator, sha)
                content = blob.decode("utf-8")
            except (httpx.HTTPError, ValueError):
                # Context files are best effort, like the blob cache of the diff step.
                _LOGGER.warning("Skipping context file %s of run %s", path, self._run_id)
                continue
            files.append(RepositoryFile(path=path, size=len(blob), content=content))
        return tuple(files)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        run = await self._input()
        return (await self._vcs.get_pull_request(run.locator)).meta
