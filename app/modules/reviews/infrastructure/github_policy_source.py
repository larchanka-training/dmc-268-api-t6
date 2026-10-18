"""GitHub reads of the policy documents of one pinned revision.

Trees are listed by SHA and blobs are read by SHA, never ``contents`` by path. A 404 is
never reported as a missing document: with a private repository it cannot be told apart
from a lost access (RULES_FORMAT_SPEC §3.1).
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import timedelta
from typing import Literal

import httpx
from pydantic import Field, TypeAdapter

from app.common.infrastructure.github_repository_path import (
    commit_sha_segment,
    ref_segment,
    repository_path_from_full_name,
)
from app.modules.repositories.application.rules_status import BranchHead, RulesRefreshTarget
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
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
    _no_checkpoint,
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


class _Repository(_GitHubDto):
    default_branch: str = Field(min_length=1, max_length=255)


_REPOSITORY = TypeAdapter(_Repository)
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def _kind(item: _TreeItem) -> PolicyEntryKind:
    if item.type == "tree":
        return PolicyEntryKind.DIRECTORY
    if item.type == "commit":
        return PolicyEntryKind.SUBMODULE
    return PolicyEntryKind.SYMLINK if item.mode == _SYMLINK_MODE else PolicyEntryKind.FILE


def policy_tree_entries(payload: object, *, policy_base_sha: str) -> dict[str, PolicyTreeEntry]:
    """The entries of one GitHub tree listing; a listing that proves nothing raises.

    Shared by every policy source, so that a run and the repository rules status read a
    tree alike (RULES_FORMAT_SPEC §3.1).
    """
    try:
        tree = _TREE.validate_python(payload)
    except ValueError as exc:
        # An answer that is not a tree listing proves nothing about the documents.
        raise PolicyError(
            PolicyErrorCode.FETCH_TRANSIENT,
            "GitHub returned an unreadable tree listing",
            policy_base_sha=policy_base_sha,
        ) from exc
    if tree.truncated:
        # A cut listing cannot prove that a path is absent.
        raise PolicyError(
            PolicyErrorCode.REVISION_UNAVAILABLE,
            "GitHub returned a truncated tree listing",
            policy_base_sha=policy_base_sha,
        )
    return {item.path: PolicyTreeEntry(_kind(item), item.sha, item.size) for item in tree.tree}


def classify_policy_http_error(exc: httpx.HTTPError, *, policy_base_sha: str | None) -> PolicyError:
    """The §9 code of a failed GitHub read; a 404 is never a missing document."""

    def error(
        code: PolicyErrorCode, message: str, retry_after: timedelta | None = None
    ) -> PolicyError:
        return PolicyError(code, message, policy_base_sha=policy_base_sha, retry_after=retry_after)

    if not isinstance(exc, httpx.HTTPStatusError):
        return error(
            PolicyErrorCode.FETCH_TRANSIENT, f"GitHub request failed: {type(exc).__name__}"
        )
    response = exc.response
    status = response.status_code
    retry_after = response.headers.get("Retry-After")
    delay = timedelta(seconds=int(retry_after)) if retry_after and retry_after.isdigit() else None
    limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
    message = f"GitHub answered {status}"
    if status == 429 or (status == 403 and limited):
        return error(PolicyErrorCode.RATE_LIMITED, message, delay)
    if status in {401, 403}:
        return error(PolicyErrorCode.ACCESS_DENIED, message)
    if status >= 500:
        return error(PolicyErrorCode.FETCH_TRANSIENT, message)
    # 404, 409 and 422: the pinned revision or its object cannot be read.
    return error(PolicyErrorCode.REVISION_UNAVAILABLE, message)


class GitHubRepositoryPolicySource:
    """``PolicyDocumentSource`` bound to one repository and one commit, with no run.

    The rules status of a repository reads its default branch head with it (#150); the
    run source below adds the checkpoints and the pull request of its run. Both share
    the reads and the error classification, so a 404 is ``unavailable`` for both.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        installation_external_id: int,
        repository_full_name: str,
        policy_base_sha: str,
        read_blob: Callable[[str], Awaitable[bytes]],
        checkpoint: RunProviderCheckpoint = _no_checkpoint,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._installation = installation_external_id
        self._full_name = repository_full_name
        self._read_blob = read_blob
        self._checkpoint = checkpoint
        self._base_sha = policy_base_sha

    def _prefix(self) -> str:
        if self._installation <= 0:
            raise ValueError("GitHub installation must be positive")
        return repository_path_from_full_name(self._full_name)

    async def list_entries(self, tree_oid: str | None) -> Mapping[str, PolicyTreeEntry]:
        sha = commit_sha_segment(self._base_sha if tree_oid is None else tree_oid)
        prefix = self._prefix()
        try:
            await self._checkpoint()
            token = await self._tokens.get_installation_access_token(self._installation)
            await self._checkpoint()
            response = await self._client.get(
                f"{prefix}/git/trees/{sha}",
                headers=_headers(token),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc
        except ValueError as exc:
            # The token answer of GitHub has another shape.
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable token"
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            # An answer that is not JSON proves nothing about the documents.
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable tree listing"
            ) from exc
        return policy_tree_entries(payload, policy_base_sha=self._base_sha)

    async def read_blob(self, oid: str) -> bytes:
        try:
            return await self._read_blob(oid)
        except BlobTooLargeError as exc:
            raise PolicyError(
                PolicyErrorCode.DOCUMENT_TOO_LARGE,
                f"The policy document exceeds {MAX_DOCUMENT_BYTES} bytes",
                policy_base_sha=self._base_sha,
                limit=MAX_DOCUMENT_BYTES,
            ) from exc
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc
        except ValueError as exc:
            # The blob answer is not JSON, not base64 or of another shape.
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable blob"
            ) from exc

    def _error(
        self, code: PolicyErrorCode, message: str, *, retry_after: timedelta | None = None
    ) -> PolicyError:
        return PolicyError(code, message, policy_base_sha=self._base_sha, retry_after=retry_after)

    def _classify(self, exc: httpx.HTTPError) -> PolicyError:
        return classify_policy_http_error(exc, policy_base_sha=self._base_sha)


class GitHubPolicyDocumentSource(GitHubRepositoryPolicySource):
    """``PolicyDocumentSource`` bound to one repository and its pinned base revision."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        vcs: VcsProvider,
        locator: PullRequestLocator,
        checkpoint: RunProviderCheckpoint,
        policy_base_sha: str,
    ) -> None:
        super().__init__(
            client=client,
            token_provider=token_provider,
            installation_external_id=locator.installation_external_id,
            repository_full_name=locator.repository_full_name,
            policy_base_sha=policy_base_sha,
            read_blob=lambda oid: vcs.get_blob(locator, oid, checkpoint=checkpoint),
            checkpoint=checkpoint,
        )
        self._locator = locator

    def _prefix(self) -> str:
        return _repository_prefix(self._locator)


class GitHubDefaultBranchHeadSource:
    """The current default branch of a repository and the full SHA of its head commit.

    The branch comes from GitHub, not from the stored repository, so a renamed default
    branch is read at once. Every failure is a ``PolicyError`` without a revision; a 404
    or a 422 (an unknown ref) is ``unavailable``, never a missing document.
    """

    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def resolve(self, target: RulesRefreshTarget) -> BranchHead:
        if target.installation_external_id <= 0:
            raise ValueError("GitHub installation must be positive")
        prefix = repository_path_from_full_name(target.repository_full_name)
        try:
            token = await self._tokens.get_installation_access_token(
                target.installation_external_id
            )
            response = await self._client.get(
                prefix, headers=_headers(token), timeout=_REQUEST_TIMEOUT
            )
            response.raise_for_status()
            branch = _REPOSITORY.validate_json(response.content).default_branch
            response = await self._client.get(
                f"{prefix}/commits/{ref_segment(branch)}",
                headers=_headers(token) | {"Accept": "application/vnd.github.sha"},
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise classify_policy_http_error(exc, policy_base_sha=None) from exc
        except ValueError as exc:
            # The token or the repository answer of GitHub has another shape.
            raise PolicyError(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable repository"
            ) from exc
        sha = response.text.strip()
        if _FULL_SHA.fullmatch(sha) is None:
            raise PolicyError(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable commit SHA"
            )
        return BranchHead(branch=branch, sha=sha.lower())
