"""Mandatory GitLab policy documents from complete target/start listings."""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttpError
from app.modules.reviews.application.policy_documents import (
    MAX_DOCUMENT_BYTES,
    PolicyEntryKind,
    PolicyTreeEntry,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree


class GitLabPolicyDocumentSource:
    def __init__(self, *, trees: GitLabRepositoryTree, policy_base_sha: str) -> None:
        if policy_base_sha != trees.expected_input.base_sha:
            raise ValueError("GitLab policy revision does not match the attempt")
        self._trees = trees
        self._base = policy_base_sha
        self._known_blobs: set[str] = set()

    def _error(self, error: GitLabRunHttpError) -> PolicyError:
        code = {
            "access_denied": PolicyErrorCode.ACCESS_DENIED,
            "rate_limited": PolicyErrorCode.RATE_LIMITED,
            "not_found": PolicyErrorCode.REVISION_UNAVAILABLE,
            "invalid_response": PolicyErrorCode.REVISION_UNAVAILABLE,
            "body_too_large": PolicyErrorCode.DOCUMENT_TOO_LARGE,
            "budget_exhausted": PolicyErrorCode.REVISION_UNAVAILABLE,
        }.get(error.kind, PolicyErrorCode.FETCH_TRANSIENT)
        return PolicyError(
            code,
            "GitLab policy source is unavailable",
            policy_base_sha=self._base,
            retry_after=error.retry_after,
            limit=MAX_DOCUMENT_BYTES if code is PolicyErrorCode.DOCUMENT_TOO_LARGE else None,
        )

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        await self._trees.checkpoint()
        try:
            path = (
                ""
                if tree_oid is None
                else await self._trees.directory_path(tree_oid, revision=self._base)
            )
            listing = await self._trees.tree(self._base, path=path, recursive=False)
            if not listing.listing_complete:
                raise GitLabRunHttpError("invalid_response")
            self._known_blobs.update(entry.oid for entry in listing.entries if entry.kind == "file")
            kinds = {
                "file": PolicyEntryKind.FILE,
                "directory": PolicyEntryKind.DIRECTORY,
                "symlink": PolicyEntryKind.SYMLINK,
                "submodule": PolicyEntryKind.SUBMODULE,
            }
            return {
                entry.name: PolicyTreeEntry(
                    kinds[entry.kind], entry.oid, listing.sizes.get(entry.path)
                )
                for entry in listing.entries
            }
        except GitLabRunHttpError as error:
            raise self._error(error) from None

    async def read_blob(self, oid: str) -> bytes:
        await self._trees.checkpoint()
        try:
            if oid not in self._known_blobs:
                raise GitLabRunHttpError("invalid_response")
            return await self._trees.read_blob(oid, max_bytes=MAX_DOCUMENT_BYTES)
        except GitLabRunHttpError as error:
            raise self._error(error) from None
