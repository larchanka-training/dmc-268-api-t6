"""Mandatory Bitbucket rules and AGENTS.md from the pinned target revision."""

from __future__ import annotations

from collections.abc import Mapping

from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttpError
from app.modules.reviews.application.policy_documents import (
    MAX_DOCUMENT_BYTES,
    PolicyEntryKind,
    PolicyTreeEntry,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree


class BitbucketPolicyDocumentSource:
    def __init__(self, *, trees: BitbucketRepositoryTree, policy_base_sha: str) -> None:
        if policy_base_sha != trees.expected_input.base_sha:
            raise ValueError("Bitbucket policy revision does not match the attempt")
        self._trees = trees
        self._base = policy_base_sha
        self._known_files: set[str] = set()

    def _error(self, error: BitbucketRunHttpError) -> PolicyError:
        code = {
            "rate_limited": PolicyErrorCode.RATE_LIMITED,
            "body_too_large": PolicyErrorCode.DOCUMENT_TOO_LARGE,
            "budget_exhausted": PolicyErrorCode.REVISION_UNAVAILABLE,
            "invalid_response": PolicyErrorCode.REVISION_UNAVAILABLE,
            "invalid_json": PolicyErrorCode.REVISION_UNAVAILABLE,
        }.get(error.kind, PolicyErrorCode.FETCH_TRANSIENT)
        return PolicyError(
            code,
            "Bitbucket policy source is unavailable",
            policy_base_sha=self._base,
            retry_after=error.retry_after,
            limit=MAX_DOCUMENT_BYTES if code is PolicyErrorCode.DOCUMENT_TOO_LARGE else None,
        )

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        try:
            path = "" if tree_oid is None else self._trees.directory_path(tree_oid)
            entries = await self._trees.directory(self._base, path)
            kinds = {
                "file": PolicyEntryKind.FILE,
                "directory": PolicyEntryKind.DIRECTORY,
                "symlink": PolicyEntryKind.SYMLINK,
            }
            result = {
                entry.name: PolicyTreeEntry(kinds[entry.kind], entry.oid, entry.size)
                for entry in entries
            }
            self._known_files.update(entry.oid for entry in entries if entry.kind == "file")
            return result
        except BitbucketRunHttpError as error:
            raise self._error(error) from None

    async def read_blob(self, oid: str) -> bytes:
        try:
            if oid not in self._known_files:
                raise BitbucketRunHttpError("invalid_response")
            return await self._trees.read_file(oid, max_bytes=MAX_DOCUMENT_BYTES)
        except BitbucketRunHttpError as error:
            raise self._error(error) from None
