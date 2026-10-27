"""GitHub reads of the policy documents of one pinned revision.

Trees are listed by SHA and blobs are read by SHA, never ``contents`` by path. A blob is
streamed raw and the read stops past ``MAX_DOCUMENT_BYTES`` (#168). A 404 is never reported
as a missing document: with a private repository it cannot be told apart from a lost access
(RULES_FORMAT_SPEC §3.1).
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
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
from app.modules.reviews.application.run_failures import parse_retry_after
from app.modules.reviews.application.vcs_diff import PullRequestLocator
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
# The modes Git writes for each entry type of a tree.
_MODES = {"blob": {"100644", "100755", _SYMLINK_MODE}, "tree": {"040000"}, "commit": {"160000"}}


class _TreeItem(_GitHubDto):
    path: str = Field(min_length=1)
    # GitHub always sends the mode; a listing without it is ambiguous, not unreadable.
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
# The portal shows a 40 hex commit SHA; GitHub has no SHA-256 repositories.
_FULL_SHA = re.compile(r"[0-9a-fA-F]{40}")
_RAW_BLOB = "application/vnd.github.raw+json"


@dataclass(frozen=True)
class OversizedBody:
    """A body over its cap: ``actual`` is the declared length of an unencoded body, or else
    the bytes received when the read stopped, then only a lower bound."""

    actual: int
    actual_is_lower_bound: bool


async def capped_body(response: httpx.Response, max_bytes: int) -> bytes | OversizedBody:
    """The decoded body, or its known size once it is declared or read over ``max_bytes``.

    The read stops with the first chunk past the cap, so at most one chunk more is taken.
    """
    declared = response.headers.get("Content-Length")
    # An encoded body declares the length of other bytes than the decoded ones counted here.
    encoded = response.headers.get("Content-Encoding", "").strip().casefold() not in {
        "",
        "identity",
    }
    if not encoded and declared is not None and declared.isdecimal() and int(declared) > max_bytes:
        return OversizedBody(int(declared), actual_is_lower_bound=False)
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_bytes:
            return OversizedBody(len(body) + len(chunk), actual_is_lower_bound=True)
        body.extend(chunk)
    return bytes(body)


def policy_blob_too_large(oversized: OversizedBody, *, policy_base_sha: str) -> PolicyError:
    """The §9 failure of a policy blob read over ``MAX_DOCUMENT_BYTES``, with its size.

    The loader adds the document path (``PolicyError.located``).
    """
    return PolicyError(
        PolicyErrorCode.DOCUMENT_TOO_LARGE,
        f"The policy document exceeds {MAX_DOCUMENT_BYTES} bytes",
        policy_base_sha=policy_base_sha,
        limit=MAX_DOCUMENT_BYTES,
        actual=oversized.actual,
        actual_is_lower_bound=oversized.actual_is_lower_bound,
    )


def _git_blob_oid(raw: bytes, *, like: str) -> str:
    """The Git object id of a blob, hashed as ``like`` is: SHA-256 for 64 digits."""
    digest = hashlib.sha256 if len(like) == 64 else hashlib.sha1
    return digest(b"blob %d\0" % len(raw) + raw, usedforsecurity=False).hexdigest()


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
    entries = {item.path: PolicyTreeEntry(_kind(item), item.sha, item.size) for item in tree.tree}
    if len(entries) != len(tree.tree) or any(
        "/" in item.path or item.path in {".", ".."} or item.mode not in _MODES[item.type]
        for item in tree.tree
    ):
        # Only a lossless, nonrecursive listing proves that a path is absent. The listing
        # of one SHA is always the same, so asking again cannot help.
        raise PolicyError(
            PolicyErrorCode.REVISION_UNAVAILABLE,
            "GitHub returned an ambiguous tree listing",
            policy_base_sha=policy_base_sha,
        )
    return entries


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
    delay = parse_retry_after(
        response.headers.get("Retry-After"), date=response.headers.get("Date")
    )
    # A date already past is a 0 s delay and still a limit, so test for None.
    limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
    message = f"GitHub answered {status}"
    if status == 429 or (status == 403 and limited):
        return error(PolicyErrorCode.RATE_LIMITED, message, delay)
    if status in {401, 403}:
        return error(PolicyErrorCode.ACCESS_DENIED, message)
    if status >= 500:
        # Still a transient read, not a rate limit, yet it waits out a given Retry-After.
        return error(PolicyErrorCode.FETCH_TRANSIENT, message, delay)
    # 404, 409 and 422: the pinned revision or its object cannot be read.
    return error(PolicyErrorCode.REVISION_UNAVAILABLE, message)


class GitHubRepositoryPolicySource:
    """``PolicyDocumentSource`` bound to one repository and one commit, with no run.

    The rules status of a repository reads its default branch head with it (#150); the
    run source below adds the checkpoints and the pull request of its run. Both share
    the reads and the error classification, so a 404 is ``unavailable`` for both. A blob
    is streamed raw.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        installation_external_id: int,
        repository_full_name: str,
        policy_base_sha: str,
        checkpoint: RunProviderCheckpoint = _no_checkpoint,
    ) -> None:
        self._client = client
        self._tokens = token_provider
        self._installation = installation_external_id
        self._full_name = repository_full_name
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
            return await self._read_raw_blob(oid)
        except httpx.HTTPError as exc:
            raise self._classify(exc) from exc
        except ValueError as exc:
            # A blob id or a token answer of another shape.
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned an unreadable blob"
            ) from exc

    async def _read_raw_blob(self, oid: str) -> bytes:
        sha = commit_sha_segment(oid)
        prefix = self._prefix()
        await self._checkpoint()
        token = await self._tokens.get_installation_access_token(self._installation)
        await self._checkpoint()
        async with self._client.stream(
            "GET",
            f"{prefix}/git/blobs/{sha}",
            # The bytes of the document: base64 JSON would cut it at ~48 KiB, and with a
            # transfer encoding the declared length would count other bytes.
            headers=_headers(token) | {"Accept": _RAW_BLOB, "Accept-Encoding": "identity"},
            timeout=_REQUEST_TIMEOUT,
        ) as response:
            response.raise_for_status()
            body = await capped_body(response, MAX_DOCUMENT_BYTES)
        if isinstance(body, OversizedBody):
            raise policy_blob_too_large(body, policy_base_sha=self._base_sha)
        # The raw answer echoes no SHA: a proxy page or another blob must not pass.
        if _git_blob_oid(body, like=sha) != sha.casefold():
            raise self._error(
                PolicyErrorCode.FETCH_TRANSIENT, "GitHub returned a blob of another id"
            )
        return body

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
