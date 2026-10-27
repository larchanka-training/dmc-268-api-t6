"""GitHub App VCS adapter for complete PR file lists and SHA-addressed blobs."""

from __future__ import annotations

import base64
import binascii
import logging
import re
from collections.abc import Callable
from dataclasses import replace
from typing import Literal, Protocol

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    model_validator,
)

from app.common.application.github_repository_name import REPOSITORY_FULL_NAME_MAX_LENGTH
from app.common.infrastructure.github_link_header import has_next_relation
from app.common.infrastructure.github_repository_path import (
    commit_sha_segment,
    repository_path_from_full_name,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.commit_prefix_limits import (
    COMMIT_ACCEPT_ENCODING,
    COMMIT_PAGE_COUNT,
    COMMIT_PAGE_SIZE,
    MAX_COMMIT_MESSAGES,
    MAX_COMMIT_RESPONSE_BYTES,
    commit_prefix_incomplete,
)
from app.modules.reviews.application.prompt_builder import (
    PullRequestMeta,
    shorten_commit_message,
)
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.application.vcs_diff import (
    MAX_BLOB_BYTES,
    BlobTooLargeError,
    CommitMessagePrefix,
    MergeBaseUnavailable,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    is_empty_addition_candidate,
    is_generated_path,
)
from app.modules.reviews.infrastructure.vcs_failures import (
    classify_vcs_error,
    raise_for_streamed_status,
)

_SHA = r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"
_MAX_FILES = 3000
_PAGE_SIZE = 100
_REQUEST_TIMEOUT = 10.0
_MAX_BLOB_RESPONSE_BYTES = 1_500_000  # 1 MiB of base64 content plus JSON envelope.
MAX_EMPTY_FILE_TREE_BYTES = 1_500_000  # One optional pinned-head metadata read per PR diff.
_API_VERSION = "2022-11-28"
_LOGGER = logging.getLogger(__name__)


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class _GitHubDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _User(_GitHubDto):
    login: str = Field(min_length=1, max_length=255)


class _Repo(_GitHubDto):
    full_name: str = Field(min_length=3, max_length=REPOSITORY_FULL_NAME_MAX_LENGTH)


class _Ref(_GitHubDto):
    ref: str = Field(min_length=1, max_length=255)
    sha: str = Field(pattern=_SHA)
    repo: _Repo | None


class _Label(_GitHubDto):
    name: str = Field(min_length=1)


class _PullRequest(_GitHubDto):
    id: int = Field(gt=0, le=2**63 - 1)
    number: int = Field(gt=0, le=2**31 - 1)
    title: str = Field(min_length=1, max_length=500)
    body: str | None = None
    user: _User
    head: _Ref
    base: _Ref
    labels: list[_Label] = Field(default_factory=list)
    draft: bool
    changed_files: int = Field(ge=0)
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class _File(_GitHubDto):
    filename: str = Field(min_length=1, max_length=1024)
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    sha: str | None = Field(default=None, pattern=_SHA)
    previous_filename: str | None = None
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)
    changes: int = Field(ge=0)
    patch: str | None = None
    size: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def validate_file(self) -> _File:
        if self.status == "renamed" and not self.previous_filename:
            raise ValueError("renamed GitHub file needs previous_filename")
        if self.status != "removed" and self.sha is None:
            raise ValueError("GitHub file needs an immutable blob SHA")
        if self.changes != self.additions + self.deletions:
            raise ValueError("GitHub file change counts disagree")
        return self


class _Blob(_GitHubDto):
    sha: str = Field(pattern=_SHA)
    encoding: Literal["base64"]
    content: str
    size: int = Field(ge=0)


class _TreeEntry(_GitHubDto):
    path: str = Field(min_length=1)
    sha: str = Field(pattern=_SHA)
    type: str
    mode: str
    size: int | None = Field(default=None, ge=0)


class _TreeListing(_GitHubDto):
    tree: list[_TreeEntry]
    truncated: bool


class _Commit(_GitHubDto):
    sha: str = Field(pattern=_SHA)


class _CommitMessage(_GitHubDto):
    message: str


class _PullRequestCommit(_GitHubDto):
    commit: _CommitMessage


class _Comparison(_GitHubDto):
    merge_base_commit: _Commit


_FILES = TypeAdapter(list[_File])
_COMMITS = TypeAdapter(list[_PullRequestCommit])


async def _no_checkpoint() -> None:
    """The checkpoint of a read that belongs to no run."""


def _repository_prefix(locator: PullRequestLocator) -> str:
    if locator.installation_external_id <= 0 or locator.number <= 0:
        raise ValueError("GitHub installation and pull request number must be positive")
    return repository_path_from_full_name(locator.repository_full_name)


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


class HttpGitHubVcsProvider:
    def __init__(
        self, *, client: httpx.AsyncClient, token_provider: InstallationTokenProvider
    ) -> None:
        self._client = client
        self._tokens = token_provider

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        prefix = _repository_prefix(locator)
        await checkpoint()
        token = await self._tokens.get_installation_access_token(locator.installation_external_id)
        await checkpoint()
        response = await self._client.get(
            f"{prefix}/pulls/{locator.number}",
            headers=_headers(token),
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
        item = _PullRequest.model_validate(response.json())
        if item.number != locator.number:
            raise ValueError("GitHub pull request number changed")
        return VcsPullRequest(
            locator=locator,
            head_sha=item.head.sha,
            base_sha=item.base.sha,
            meta=PullRequestMeta(
                head_sha=item.head.sha,
                title=item.title,
                description=item.body,
                author=item.user.login,
                source_branch=item.head.ref,
                target_branch=item.base.ref,
                labels=tuple(label.name for label in item.labels),
                files_changed=item.changed_files,
                lines_added=item.additions,
                lines_removed=item.deletions,
                is_draft=item.draft,
                is_fork=item.head.repo is None
                or item.base.repo is None
                or item.head.repo.full_name != item.base.repo.full_name,
            ),
        )

    async def get_commit_messages(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> CommitMessagePrefix:
        """Read the bounded API-order prefix separately from ordinary PR metadata."""
        prefix = _repository_prefix(locator)
        messages: list[str] = []
        incomplete = False
        try:
            for page in range(1, COMMIT_PAGE_COUNT + 1):
                await checkpoint()
                token = await self._tokens.get_installation_access_token(
                    locator.installation_external_id
                )
                await checkpoint()
                body, has_next = await self._get_capped_body(
                    f"{prefix}/pulls/{locator.number}/commits",
                    headers=_headers(token) | {"Accept-Encoding": COMMIT_ACCEPT_ENCODING},
                    params={"per_page": COMMIT_PAGE_SIZE, "page": page},
                    max_bytes=MAX_COMMIT_RESPONSE_BYTES,
                    too_large=lambda: RunFailure(
                        "commit_messages_fetch_failed",
                        "GitHub commit response exceeds the "
                        f"{MAX_COMMIT_RESPONSE_BYTES / 2**20:g} MiB retrieval budget",
                    ),
                    accepted_encoding=COMMIT_ACCEPT_ENCODING,
                    unsupported_encoding=lambda: RunFailure(
                        "commit_messages_fetch_failed",
                        "GitHub commit response uses unsupported content encoding",
                    ),
                )
                try:
                    batch = _COMMITS.validate_json(body)
                except ValueError:
                    raise RunFailure(
                        "commit_messages_fetch_failed", "GitHub commit response is malformed"
                    ) from None
                if len(batch) > COMMIT_PAGE_SIZE:
                    raise RunFailure(
                        "commit_messages_fetch_failed", "GitHub commit response is malformed"
                    )
                remaining = MAX_COMMIT_MESSAGES - len(messages)
                messages.extend(
                    shorten_commit_message(item.commit.message) for item in batch[:remaining]
                )
                incomplete = commit_prefix_incomplete(
                    has_next=has_next, page=page, batch_length=len(batch), remaining=remaining
                )
                if len(batch) < COMMIT_PAGE_SIZE:
                    break
        except httpx.HTTPError as exc:
            raise classify_vcs_error(exc, fetch_error_code="commit_messages_fetch_failed") from None
        return CommitMessagePrefix(tuple(messages), incomplete)

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        locator = pull_request.locator
        prefix = _repository_prefix(locator)
        expected = pull_request.meta.files_changed
        if expected > _MAX_FILES:
            raise ValueError("GitHub pull request exceeds the 3000-file API cap")
        await checkpoint()
        token = await self._tokens.get_installation_access_token(locator.installation_external_id)
        files: list[VcsFile] = []
        for page in range(1, _MAX_FILES // _PAGE_SIZE + 1):
            await checkpoint()
            response = await self._client.get(
                f"{prefix}/pulls/{locator.number}/files",
                params={"per_page": _PAGE_SIZE, "page": page},
                headers=_headers(token),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            batch = _FILES.validate_python(response.json())
            if len(batch) > _PAGE_SIZE:
                raise ValueError("GitHub file page exceeded 100 entries")
            files.extend(
                VcsFile(
                    filename=item.filename,
                    status=item.status,
                    blob_sha=None if item.status == "removed" else item.sha,
                    previous_filename=item.previous_filename,
                    additions=item.additions,
                    deletions=item.deletions,
                    changes=item.changes,
                    patch=item.patch,
                    size=item.size,
                )
                for item in batch
            )
            if len(files) > expected:
                raise ValueError("GitHub file pagination exceeded the PR file count")
            if len(files) == expected:
                break
            if len(batch) < _PAGE_SIZE:
                raise ValueError("GitHub file pagination ended before the PR file count")
        else:
            raise ValueError("GitHub file pagination exceeded the 3000-file API cap")
        if len({file.filename for file in files}) != len(files):
            raise ValueError("GitHub file pagination repeated a filename")
        if await self.get_pull_request(locator, checkpoint=checkpoint) != pull_request:
            raise ValueError("GitHub pull request changed during file pagination")
        return await self._with_regular_empty_modes(pull_request, tuple(files), token, checkpoint)

    async def _with_regular_empty_modes(
        self,
        pull_request: VcsPullRequest,
        files: tuple[VcsFile, ...],
        token: str,
        checkpoint: RunProviderCheckpoint,
    ) -> tuple[VcsFile, ...]:
        """Certify candidates only from a complete bounded tree at the immutable head."""
        candidates = {
            file.filename
            for file in files
            if is_empty_addition_candidate(file) and not is_generated_path(file.filename)
        }
        if not candidates:
            return files
        locator = pull_request.locator
        try:
            await checkpoint()
            body, _ = await self._get_capped_body(
                f"{_repository_prefix(locator)}/git/trees/"
                f"{commit_sha_segment(pull_request.head_sha)}",
                headers=_headers(token) | {"Accept-Encoding": "identity"},
                params={"recursive": 1},
                max_bytes=MAX_EMPTY_FILE_TREE_BYTES,
                too_large=lambda: ValueError("GitHub empty-file tree exceeds metadata budget"),
                accepted_encoding="identity",
                unsupported_encoding=lambda: ValueError(
                    "GitHub empty-file tree uses unsupported content encoding"
                ),
            )
            listing = _TreeListing.model_validate_json(body)
            if listing.truncated:
                raise ValueError("GitHub empty-file tree is truncated")
            if len({item.path for item in listing.tree}) != len(listing.tree):
                raise ValueError("GitHub empty-file tree repeats a path")
        except (httpx.HTTPError, ValueError):
            _LOGGER.warning(
                "GitHub empty-file mode proof unavailable repository=%s pr_number=%s",
                locator.repository_full_name,
                locator.number,
                exc_info=True,
            )
            return files
        entries = {item.path: item for item in listing.tree if item.path in candidates}
        proven: list[VcsFile] = []
        for file in files:
            if file.filename not in candidates:
                proven.append(file)
                continue
            entry = entries.get(file.filename)
            if (
                entry is not None
                and entry.sha == file.blob_sha
                and entry.type == "blob"
                and entry.mode in {"100644", "100755"}
                and entry.size in (None, 0)
            ):
                proven.append(replace(file, git_mode=entry.mode))
            else:
                _LOGGER.warning(
                    "GitHub empty-file mode proof unavailable repository=%s pr_number=%s: "
                    "tree lacks matching regular-file evidence",
                    locator.repository_full_name,
                    locator.number,
                )
                proven.append(file)
        return tuple(proven)

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str:
        """Return the merge base GitHub diffs the pull request head against (three-dot)."""
        locator = pull_request.locator
        prefix = _repository_prefix(locator)
        base = commit_sha_segment(pull_request.base_sha)
        head = commit_sha_segment(pull_request.head_sha)
        try:
            await checkpoint()
            token = await self._tokens.get_installation_access_token(
                locator.installation_external_id
            )
            await checkpoint()
            response = await self._client.get(
                f"{prefix}/compare/{base}...{head}",
                # Only the merge base is read; one commit per page keeps the response small.
                params={"per_page": 1},
                headers=_headers(token),
                timeout=_REQUEST_TIMEOUT,
            )
            response.raise_for_status()
            return _Comparison.model_validate(response.json()).merge_base_commit.sha
        except (httpx.HTTPError, ValueError) as exc:
            # ValueError covers a body that is not JSON and an answer of another shape.
            raise MergeBaseUnavailable(f"GitHub comparison failed: {type(exc).__name__}") from exc

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        _repository_prefix(locator)
        return await self.get_repository_blob(
            locator.installation_external_id,
            locator.repository_full_name,
            sha,
            checkpoint=checkpoint,
        )

    async def get_repository_blob(
        self,
        installation_external_id: int,
        repository_full_name: str,
        sha: str,
        *,
        checkpoint: RunProviderCheckpoint = _no_checkpoint,
    ) -> bytes:
        """A blob by id in its base64 JSON answer, within the 1 MiB review budget.

        ``get_blob`` reads the blobs of a pull request with it.
        """
        if installation_external_id <= 0:
            raise ValueError("GitHub installation must be positive")
        prefix = repository_path_from_full_name(repository_full_name)
        if re.fullmatch(_SHA, sha) is None:
            raise ValueError("GitHub blob SHA must be 40 or 64 hexadecimal characters")
        await checkpoint()
        token = await self._tokens.get_installation_access_token(installation_external_id)
        await checkpoint()
        body, _ = await self._get_capped_body(
            f"{prefix}/git/blobs/{sha}",
            headers=_headers(token),
            max_bytes=_MAX_BLOB_RESPONSE_BYTES,
            too_large=lambda: BlobTooLargeError(
                "GitHub blob response exceeds the retrieval budget"
            ),
            chunk_size=65_536,
        )
        item = _Blob.model_validate_json(body)
        if item.sha.casefold() != sha.casefold():
            raise ValueError("GitHub blob SHA differs from the requested SHA")
        if item.size > MAX_BLOB_BYTES:
            raise BlobTooLargeError("GitHub blob exceeds the 1 MiB review budget")
        try:
            data = base64.b64decode("".join(item.content.split()), validate=True)
        except binascii.Error as exc:
            raise ValueError("GitHub blob content is invalid base64") from exc
        if len(data) != item.size:
            raise ValueError("GitHub blob decoded size differs from declared size")
        return data

    async def _get_capped_body(
        self,
        path: str,
        *,
        headers: dict[str, str],
        max_bytes: int,
        too_large: Callable[[], Exception],
        params: dict[str, int] | None = None,
        accepted_encoding: str = "",
        unsupported_encoding: Callable[[], Exception] | None = None,
        chunk_size: int | None = None,
    ) -> tuple[bytearray, bool]:
        """Bound a streamed JSON body while retaining each endpoint's failure contract.

        With ``unsupported_encoding`` set, only an unencoded body or ``accepted_encoding`` is read.
        """
        async with self._client.stream(
            "GET", path, headers=headers, params=params, timeout=_REQUEST_TIMEOUT
        ) as response:
            # At most 4 KiB of an error body, for a secondary rate limit (#168).
            await raise_for_streamed_status(response)
            # Check before httpx reads/decodes chunks: compression could allocate above
            # the commit cap even when the compressed transport chunk fits the budget.
            if unsupported_encoding is not None and response.headers.get(
                "Content-Encoding", ""
            ).strip().casefold() not in {"", accepted_encoding}:
                raise unsupported_encoding()
            content_length = response.headers.get("Content-Length")
            if (
                content_length is not None
                and content_length.isdecimal()
                and int(content_length) > max_bytes
            ):
                raise too_large()
            body = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=chunk_size):
                if len(body) + len(chunk) > max_bytes:
                    raise too_large()
                body.extend(chunk)
            has_next = has_next_relation(response.headers.get("Link"))
        return body, has_next
