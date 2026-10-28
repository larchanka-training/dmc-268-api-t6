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

    def _error(self, error: BitbucketRunHttpError, *, listing: bool) -> PolicyError:
        code = {
            "rate_limited": PolicyErrorCode.RATE_LIMITED,
            "body_too_large": (
                PolicyErrorCode.REVISION_UNAVAILABLE
                if listing
                else PolicyErrorCode.DOCUMENT_TOO_LARGE
            ),
            "budget_exhausted": PolicyErrorCode.REVISION_UNAVAILABLE,
            "invalid_response": PolicyErrorCode.REVISION_UNAVAILABLE,
            "invalid_json": PolicyErrorCode.FETCH_TRANSIENT,
        }.get(error.kind, PolicyErrorCode.FETCH_TRANSIENT)
        if error.status_code in {401, 403}:
            code = PolicyErrorCode.ACCESS_DENIED
        elif error.status_code == 404:
            code = PolicyErrorCode.REVISION_UNAVAILABLE
        oversized_blob = error.kind == "body_too_large" and not listing
        return PolicyError(
            code,
            "Bitbucket policy source is unavailable",
            policy_base_sha=self._base,
            retry_after=error.retry_after,
            limit=MAX_DOCUMENT_BYTES if code is PolicyErrorCode.DOCUMENT_TOO_LARGE else None,
            actual=MAX_DOCUMENT_BYTES + 1 if oversized_blob else None,
            actual_is_lower_bound=oversized_blob,
            http_status=error.status_code,
            pin_head=error.status_code == 401,
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
            if error.kind == "request_budget_exceeded":
                raise
            raise self._error(error, listing=True) from None

    async def read_blob(self, oid: str) -> bytes:
        try:
            if oid not in self._known_files:
                raise BitbucketRunHttpError("invalid_response")
            return await self._trees.read_file(oid, max_bytes=MAX_DOCUMENT_BYTES)
        except BitbucketRunHttpError as error:
            if error.kind == "request_budget_exceeded":
                raise
            raise self._error(error, listing=False) from None
