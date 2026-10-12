"""GitHub reads of the policy documents of one pinned base revision.

Trees are listed by SHA and blobs are read by SHA, never ``contents`` by path. A 404 is
never reported as a missing document: with a private repository it cannot be told apart
from a lost access (RULES_FORMAT_SPEC §3.1).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import Field, TypeAdapter, ValidationError

from app.common.infrastructure.github_repository_path import commit_sha_segment
from app.modules.reviews.application.policy_documents import (
    MAX_DOCUMENT_BYTES,
    PolicyEntryKind,
    PolicyTreeEntry,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.vcs_diff import (
    BlobTooLargeError,
    PullRequestLocator,
    VcsProvider,
)
from app.modules.reviews.infrastructure.github_vcs import (
    _REQUEST_TIMEOUT,
    _SHA,
    InstallationTokenProvider,
    _GitHubDto,
    _headers,
    _repository_prefix,
)

_SYMLINK_MODE = "120000"


class _TreeItem(_GitHubDto):
    path: str = Field(min_length=1)
    # GitHub always sends the mode; without it an entry cannot be told to be a symlink.
    mode: str | None = None
    type: Literal["blob", "tree", "commit"]
    sha: str = Field(pattern=_SHA)
    size: int | None = Field(default=None, ge=0)


class _Tree(_GitHubDto):
    tree: list[_TreeItem]
    truncated: bool = False


_TREE = TypeAdapter(_Tree)


def _kind(item: _TreeItem) -> PolicyEntryKind:
    if item.type == "tree":
        return PolicyEntryKind.DIRECTORY
    if item.type == "commit":
        return PolicyEntryKind.SUBMODULE
    return PolicyEntryKind.SYMLINK if item.mode == _SYMLINK_MODE else PolicyEntryKind.FILE


class GitHubPolicyDocumentSource:
    """``PolicyDocumentSource`` bound to one repository and its pinned base revision."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        vcs: VcsProvider,
        locator: PullRequestLocator,
        policy_base_sha: str,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._vcs = vcs
        self._locator = locator
        self._base_sha = policy_base_sha

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        sha = commit_sha_segment(self._base_sha if tree_oid is None else tree_oid)
        try:
            token = await self._tokens.get_installation_access_token(
                self._locator.installation_external_id
            )
            response = await self._client.get(
                f"{_repository_prefix(self._locator)}/git/trees/{sha}",
                headers=_headers(token),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc
        try:
            tree = _TREE.validate_python(response.json())
        except ValidationError as exc:
            # An answer that is not a tree listing proves nothing about the documents.
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable tree listing"
            ) from exc
        if tree.truncated:
            # A cut listing cannot prove that a path is absent.
            raise self._error(
                PolicyErrorCode.REVISION_UNAVAILABLE, "GitHub returned a truncated tree listing"
            )
        return {item.path: PolicyTreeEntry(_kind(item), item.sha, item.size) for item in tree.tree}

    async def read_blob(self, oid: str) -> bytes:
        try:
            return await self._vcs.get_blob(self._locator, oid)
        except BlobTooLargeError as exc:
            raise PolicyError(
                PolicyErrorCode.DOCUMENT_TOO_LARGE,
                f"The policy document exceeds {MAX_DOCUMENT_BYTES} bytes",
                policy_base_sha=self._base_sha,
                limit=MAX_DOCUMENT_BYTES,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc

    def _error(
        self, code: PolicyErrorCode, message: str, *, retry_after: timedelta | None = None
    ) -> PolicyError:
        return PolicyError(code, message, policy_base_sha=self._base_sha, retry_after=retry_after)

    def _classify(self, exc: httpx.HTTPError) -> PolicyError:
        if not isinstance(exc, httpx.HTTPStatusError):
            return self._error(
                PolicyErrorCode.FETCH_TRANSIENT, f"GitHub request failed: {type(exc).__name__}"
            )
        response = exc.response
        status = response.status_code
        retry_after = response.headers.get("Retry-After")
        delay = (
            timedelta(seconds=int(retry_after)) if retry_after and retry_after.isdigit() else None
        )
        limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
        message = f"GitHub answered {status}"
        if status == 429 or (status == 403 and limited):
            return self._error(PolicyErrorCode.RATE_LIMITED, message, retry_after=delay)
        if status in {401, 403}:
            return self._error(PolicyErrorCode.ACCESS_DENIED, message)
        if status >= 500:
            return self._error(PolicyErrorCode.FETCH_TRANSIENT, message)
        # 404, 409 and 422: the pinned revision or its object cannot be read.
        return self._error(PolicyErrorCode.REVISION_UNAVAILABLE, message)
