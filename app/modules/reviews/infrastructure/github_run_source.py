"""GitHub reads of one Run: AGENTS.md, tree and files for conventions, and PR metadata.

AGENTS.md is read at the Run's ``base_sha`` and the tree at its ``head_sha`` (SD §6.2);
file contents come from SHA-addressed blobs, never from ``contents`` by path (SD §8.3).
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace
from typing import Any
from uuid import UUID

import httpx

from app.common.infrastructure.github_repository_path import commit_sha_segment
from app.modules.reviews.application.conventions import RepositoryFile, RepositorySnapshot
from app.modules.reviews.application.process_run import RunVcsInput, RunVcsRepository
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import VcsProvider
from app.modules.reviews.infrastructure.github_vcs import (
    _REQUEST_TIMEOUT,
    InstallationTokenProvider,
    _headers,
    _repository_prefix,
)
from app.modules.reviews.infrastructure.vcs_failures import classify_vcs_error

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
        conventions_from_base: bool = False,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._vcs = vcs
        self._runs = runs
        self._run_id = run_id
        # Shared conventions describe the repository, not the pull request: with this
        # set, their tree and files come from the base revision only (CONTEXT §4.2).
        self._conventions_from_base = conventions_from_base
        self._run: RunVcsInput | None = None
        self._head_blobs: dict[str, str] = {}

    async def _input(self) -> RunVcsInput:
        if self._run is None:
            run = await self._runs.get_run_vcs_input(self._run_id)
            if run is None:
                raise RunCancelled("installation_unavailable")
            self._run = run
        return self._run

    async def _checkpoint(self) -> None:
        if await self._runs.get_run_vcs_input(self._run_id) is None:
            raise RunCancelled("installation_unavailable")

    async def _tree(self, run: RunVcsInput, sha: str, *, recursive: bool) -> list[dict[str, Any]]:
        path = f"{_repository_prefix(run.locator)}/git/trees/{commit_sha_segment(sha)}"
        await self._checkpoint()
        token = await self._tokens.get_installation_access_token(
            run.locator.installation_external_id
        )
        await self._checkpoint()
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
        blob = await self._vcs.get_blob(run.locator, str(entry["sha"]), checkpoint=self._checkpoint)
        return RepositorySnapshot(content=blob.decode("utf-8", errors="replace"), sha=entry["sha"])

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        run = await self._input()
        revision = run.base_sha if self._conventions_from_base else run.head_sha
        entries = await self._tree(run, revision, recursive=True)
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
                blob = await self._vcs.get_blob(run.locator, sha, checkpoint=self._checkpoint)
                content = blob.decode("utf-8")
            except (httpx.HTTPError, ValueError):
                # Context files are best effort, like the blob cache of the diff step.
                _LOGGER.warning("Skipping context file %s of run %s", path, self._run_id)
                continue
            files.append(RepositoryFile(path=path, size=len(blob), content=content))
        return tuple(files)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        run = await self._input()
        meta = (await self._vcs.get_pull_request(run.locator, checkpoint=self._checkpoint)).meta
        path = f"{_repository_prefix(run.locator)}/pulls/{run.locator.number}/commits"
        messages: list[str] = []
        for page in (1, 2):
            try:
                await self._checkpoint()
                token = await self._tokens.get_installation_access_token(
                    run.locator.installation_external_id
                )
                await self._checkpoint()
                async with self._client.stream(
                    "GET",
                    path,
                    params={"per_page": 10, "page": page},
                    headers=_headers(token),
                    timeout=_REQUEST_TIMEOUT,
                ) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > 1_048_576:
                            raise RunFailure(
                                "diff_fetch_failed",
                                "GitHub commit response exceeds the 1 MiB retrieval budget",
                            )
                        body.extend(chunk)
            except httpx.HTTPError as exc:
                raise classify_vcs_error(exc) from None
            try:
                batch = json.loads(body)
            except ValueError:
                raise RunFailure(
                    "diff_fetch_failed", "GitHub commit response is malformed"
                ) from None
            if not isinstance(batch, list) or len(batch) > 10:
                raise RunFailure("diff_fetch_failed", "GitHub commit response is malformed")
            for item in batch:
                if not isinstance(item, dict) or not isinstance(item.get("commit"), dict):
                    raise RunFailure("diff_fetch_failed", "GitHub commit response is malformed")
                message = item["commit"].get("message")
                if not isinstance(message, str):
                    raise RunFailure("diff_fetch_failed", "GitHub commit response is malformed")
                messages.append(message if len(message) <= 500 else message[:499] + "…")
            if len(batch) < 10:
                break
        return replace(meta, commit_messages=tuple(messages))
