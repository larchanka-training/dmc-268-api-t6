"""Bitbucket conventions and PR metadata for one claimed run."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttpError
from app.modules.reviews.application.conventions import (
    RepositoryFile,
    RepositorySnapshot,
    RepositoryTreeResult,
)
from app.modules.reviews.application.policy_documents import PolicyEntryKind, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.bitbucket_policy_source import BitbucketPolicyDocumentSource
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider


class BitbucketRunSource:
    def __init__(
        self,
        *,
        trees: BitbucketRepositoryTree,
        vcs: HttpBitbucketVcsProvider,
        run_id: UUID,
    ) -> None:
        self._trees = trees
        self._vcs = vcs
        self._run_id = run_id
        self._files: dict[str, tuple[str, int]] = {}

    def _repository(self, repository_id: UUID) -> None:
        if repository_id != self._trees.expected_input.repository_id:
            raise RunCancelled("installation_unavailable")

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        await self._trees.checkpoint()
        self._repository(repository_id)
        base = self._trees.expected_input.base_sha
        source = BitbucketPolicyDocumentSource(trees=self._trees, policy_base_sha=base)
        entry = (await source.list_entries(None)).get("AGENTS.md")
        if entry is None:
            return RepositorySnapshot(None, None)
        if entry.kind is not PolicyEntryKind.FILE:
            raise PolicyError(
                PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
                "AGENTS.md must be a regular file",
                policy_base_sha=base,
            )
        raw = await source.read_blob(entry.oid)
        document = normalize_document("AGENTS.md", raw, blob_oid=entry.oid, policy_base_sha=base)
        return RepositorySnapshot(document.text, entry.oid)

    async def fetch_tree(self, repository_id: UUID) -> RepositoryTreeResult:
        await self._trees.checkpoint()
        self._repository(repository_id)
        listing = await self._trees.walk(self._trees.expected_input.base_sha)
        self._files = {
            entry.path: (entry.oid, entry.size)
            for entry in listing.entries
            if entry.kind == "file" and entry.size is not None
        }
        return RepositoryTreeResult(
            tuple(RepositoryFile(path, size) for path, (_, size) in sorted(self._files.items())),
            listing.complete,
            listing.complete,
            () if listing.complete else ("source_unavailable",),
        )

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        await self._trees.checkpoint()
        self._repository(repository_id)
        files: list[RepositoryFile] = []
        total = 0
        for path in paths[:10]:
            known = self._files.get(path)
            if known is None or total + known[1] > 131072:
                continue
            try:
                raw = await self._trees.read_file(known[0], max_bytes=131072)
                content = raw.decode("utf-8")
            except (BitbucketRunHttpError, UnicodeDecodeError):
                continue
            total += len(raw)
            files.append(RepositoryFile(path, len(raw), content))
        return tuple(files)

    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta | None:
        await self._trees.checkpoint()
        if run_id != self._run_id:
            raise RunCancelled("installation_unavailable")
        locator = self._trees.expected_input.locator
        meta = (await self._vcs.get_pull_request(locator, checkpoint=self._trees.checkpoint)).meta
        prefix = await self._vcs.get_commit_messages(locator, checkpoint=self._trees.checkpoint)
        return replace(
            meta, commit_messages=prefix.messages, commit_messages_incomplete=prefix.incomplete
        )
