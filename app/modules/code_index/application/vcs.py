"""Provider-neutral VCS port of the indexer: branch HEAD, compare and archive (#142).

The use case knows no provider (AC9, compatible with #97). Every error carries the
provider-neutral `code` the use case writes as is: `error_code` of a failed index for
`IndexLimitExceeded` and `ArchiveUnsafe`, `last_error_code` of a released lease for
`VcsUnavailable` and `VcsAccessDenied`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from app.modules.code_index.domain.codes import FAILED_ERROR_CODES

VCS_RATE_LIMITED = "vcs_rate_limited"
VCS_UNAVAILABLE = "vcs_unavailable"
VCS_ACCESS_DENIED = "vcs_access_denied"
ARCHIVE_UNSAFE = "archive_unsafe"


@dataclass(frozen=True)
class BranchHead:
    """The default branch of a repository and the commit it points at."""

    branch: str
    commit_sha: str


class Ancestry(StrEnum):
    """How the head commit of a comparison relates to its base commit."""

    AHEAD = "ahead"
    IDENTICAL = "identical"
    BEHIND = "behind"
    DIVERGED = "diverged"


class ChangeStatus(StrEnum):
    ADDED = "added"
    MODIFIED = "modified"
    REMOVED = "removed"
    RENAMED = "renamed"
    COPIED = "copied"
    CHANGED = "changed"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class ChangedPath:
    """One changed file; `previous_path` is where a renamed or copied file came from."""

    path: str
    status: ChangeStatus
    previous_path: str | None = None


@dataclass(frozen=True)
class Comparison:
    """The change set from a base commit to a head commit.

    `complete` is false when the provider may have cut the file list or its answer was
    too large to read: `files` is then not the whole change set, and an answer that was
    not read has no `ancestry` and no `files`.
    """

    ancestry: Ancestry | None
    files: tuple[ChangedPath, ...]
    complete: bool


@dataclass(frozen=True)
class SourceArchive:
    """The gzip-compressed tar archive of one commit, buffered in memory."""

    commit_sha: str
    content: bytes = field(repr=False)


class VcsUnavailable(Exception):
    """The provider did not answer usefully; a later attempt can heal it.

    `rate_limited` is set for a confirmed rate limit, and `retry_after` is the delay
    the provider asked for.
    """

    def __init__(
        self, message: str, *, rate_limited: bool = False, retry_after: timedelta | None = None
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after = retry_after
        self.code = VCS_RATE_LIMITED if rate_limited else VCS_UNAVAILABLE


class VcsAccessDenied(Exception):
    """The repository or the installation can no longer be read."""

    code = VCS_ACCESS_DENIED


class ArchiveUnsafe(Exception):
    """An archive entry is unsafe to unpack (ADR 0001 §7); the index fails."""

    code = ARCHIVE_UNSAFE


class IndexLimitExceeded(Exception):
    """A limit of ADR 0001 §7 is exceeded; the index fails with `limit_exceeded:<limit>`."""

    def __init__(self, limit: str) -> None:
        code = f"limit_exceeded:{limit}"
        if code not in FAILED_ERROR_CODES:
            raise ValueError(f"unknown index limit: {limit}")
        super().__init__(f"The index exceeds its {limit} limit")
        self.limit = limit
        self.code = code


class RepositorySnapshotSource(Protocol):
    """Reads of the default branch of one repository for one indexing job.

    `request_count` is the number of provider requests sent so far, the failed ones
    included, for `github_request_count`. A provider failure raises `VcsUnavailable`
    or `VcsAccessDenied`.
    """

    @property
    def request_count(self) -> int: ...

    async def head(self) -> BranchHead | None:
        """The default branch and its commit; `None` for an empty repository or one
        without a default branch."""
        ...

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        """The change set from `base_sha` to `head_sha`; `None` when the provider does not
        know `base_sha`."""
        ...

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        """The archive of `commit_sha`; more than `max_bytes` downloaded raises
        `IndexLimitExceeded("download_bytes")`."""
        ...
