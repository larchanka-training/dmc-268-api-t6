"""GitLab conventions and metadata bound to one trusted claimed run."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttpError
from app.modules.reviews.application.conventions import (
    RepositoryFile,
    RepositorySnapshot,
    RepositoryTreeResult,
)
from app.modules.reviews.application.policy_documents import PolicyEntryKind, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import VcsMetadataProvider
from app.modules.reviews.infrastructure.gitlab_policy_source import GitLabPolicyDocumentSource
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree


class GitLabRunSource:
    def __init__(
        self,
        *,
        trees: GitLabRepositoryTree,
        vcs: VcsMetadataProvider,
        run_id: UUID,
        conventions_from_base: bool = False,
    ) -> None:
        self._trees = trees
        self._vcs = vcs
        self._run_id = run_id
        self._base = conventions_from_base
        self._files: dict[str, tuple[str, int]] = {}

    def _repository(self, repository_id: UUID) -> None:
        if repository_id != self._trees.expected_input.repository_id:
            raise RunCancelled("installation_unavailable")

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        await self._trees.checkpoint()
        self._repository(repository_id)
        source = GitLabPolicyDocumentSource(
            trees=self._trees, policy_base_sha=self._trees.expected_input.base_sha
        )
        entry = (await source.list_entries(None)).get("AGENTS.md")
        if entry is None:
            return RepositorySnapshot(None, None)
        if entry.kind is not PolicyEntryKind.FILE:
            raise PolicyError(
                PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
                "AGENTS.md must be a regular file",
                policy_base_sha=self._trees.expected_input.base_sha,
            )
        raw = await source.read_blob(entry.oid)
        document = normalize_document(
            "AGENTS.md",
            raw,
            blob_oid=entry.oid,
            policy_base_sha=self._trees.expected_input.base_sha,
        )
        return RepositorySnapshot(document.text, entry.oid)

    async def fetch_tree(self, repository_id: UUID) -> RepositoryTreeResult:
        await self._trees.checkpoint()
        self._repository(repository_id)
        value = self._trees.expected_input
        try:
            listing = await self._trees.tree(
                value.base_sha if self._base else value.head_sha,
                side="target" if self._base else "head",
                sizes=True,
            )
        except GitLabRunHttpError as error:
            return RepositoryTreeResult(
                (),
                False,
                False,
                ("budget_exhausted" if error.kind == "budget_exhausted" else "source_unavailable",),
            )
        self._files = {
            entry.path: (entry.oid, listing.sizes[entry.path])
            for entry in listing.entries
            if entry.kind == "file" and entry.path in listing.sizes
        }
        return RepositoryTreeResult(
            tuple(RepositoryFile(path, size) for path, (_, size) in sorted(self._files.items())),
            listing.listing_complete,
            listing.sizes_complete,
            listing.incomplete_reasons,
        )

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        await self._trees.checkpoint()
        self._repository(repository_id)
        files: list[RepositoryFile] = []
        total = 0
        for path in paths[:10]:
            await self._trees.checkpoint()
            known = self._files.get(path)
            if known is None or total + known[1] > 131072:
                continue
            try:
                raw = await self._trees.read_blob(known[0])
                content = raw.decode("utf-8", errors="strict")
            except (GitLabRunHttpError, UnicodeDecodeError):
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
