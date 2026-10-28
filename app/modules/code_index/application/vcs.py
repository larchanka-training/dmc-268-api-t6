"""Provider-neutral VCS port of the indexer: branch HEAD, compare and archive (#142).

The use case knows no provider (AC9, compatible with #97). Every error carries the
provider-neutral `code` the use case writes as is: `error_code` of a failed index for
`IndexLimitExceeded` and `ArchiveUnsafe`, `last_error_code` of a released lease for
`VcsUnavailable` and `VcsAccessDenied`. `ArchiveReader` reads a downloaded archive in two
passes: the path set first, then the contents the build parses.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Set
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from typing import Protocol

from app.modules.code_index.domain.codes import FAILED_ERROR_CODES
from app.modules.reviews.application.policy_errors import PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicyDocument

VCS_RATE_LIMITED = "vcs_rate_limited"
VCS_UNAVAILABLE = "vcs_unavailable"
VCS_ACCESS_DENIED = "vcs_access_denied"
ARCHIVE_UNSAFE = "archive_unsafe"
# The `last_error_code` values a later attempt at the same head can heal: VCS errors and
# a policy read that failed on the network. Any other code of a released lease is a
# terminal refusal of that head (ADR 0001 §6.1, leader reconcile).
HEALABLE_ERROR_CODES = frozenset(
    {
        VCS_RATE_LIMITED,
        VCS_UNAVAILABLE,
        PolicyErrorCode.FETCH_TRANSIENT.value,
        PolicyErrorCode.RATE_LIMITED.value,
    }
)


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
    # Provider entries intentionally omitted before archive construction.
    provider_skips: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class ArchiveEntry:
    """A file or symlink of an archive, by its path below the archive root."""

    path: str
    size: int
    symlink: bool = False
    # The git blob SHA-1 of a file within the read limit; a symlink is never read.
    blob_sha: str | None = None


@dataclass(frozen=True)
class ArchiveCounters:
    """Every header of the archive, pax and long-name headers included, and the bytes
    they declare."""

    headers: int
    unpacked_bytes: int


@dataclass(frozen=True)
class ArchiveScan:
    """Pass 1 over an archive: its path set, the captured documents and the counters.

    `entries` holds every file and symlink; directories are implied by the paths.
    `documents` holds the content of each requested document that is a file within the
    read limit.
    """

    commit_sha: str
    entries: Mapping[str, ArchiveEntry]
    documents: Mapping[str, bytes] = field(repr=False)
    counters: ArchiveCounters


class VcsUnavailable(Exception):
    """The provider did not answer usefully; a later attempt can heal it.

    `rate_limited` is set for a confirmed rate limit, and `retry_after` is the delay
    the provider asked for.
    """

    def __init__(
        self,
        message: str,
        *,
        rate_limited: bool = False,
        retry_after: timedelta | None = None,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.rate_limited = rate_limited
        self.retry_after = retry_after
        self.http_status = http_status
        self.code = VCS_RATE_LIMITED if rate_limited else VCS_UNAVAILABLE


class VcsAccessDenied(Exception):
    """The repository or the installation can no longer be read."""

    code = VCS_ACCESS_DENIED

    def __init__(
        self, message: str, *, http_status: int | None = None, pin_head: bool = False
    ) -> None:
        super().__init__(message)
        self.http_status = http_status
        self.pin_head = pin_head


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

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        """The normalized original rules blob at the immutable archive commit.

        Missing requires complete tree evidence. Unreadable or invalid documents raise
        ``PolicyError``; archive omission alone never proves absence.
        """
        ...

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        """The archive of `commit_sha`; more than `max_bytes` downloaded raises
        `IndexLimitExceeded("download_bytes")`."""
        ...


class ArchiveReader(Protocol):
    """Reads an archive in memory, never on disk, off the event loop (ADR 0001 §7).

    Both passes stop at the `time.monotonic()` `deadline` with
    `IndexLimitExceeded("deadline")`, at the entries or unpacked-bytes limit with
    `IndexLimitExceeded`, at an unsafe entry with `ArchiveUnsafe`, and at an archive that
    cannot be read with `VcsUnavailable`.
    """

    async def scan(
        self, archive: SourceArchive, *, documents: Set[str], deadline: float
    ) -> ArchiveScan:
        """Pass 1: the path set, the blob SHA-1 of every file within the read limit and
        the content of `documents`."""
        ...

    def contents(
        self, archive: SourceArchive, paths: Set[str], *, deadline: float, batch_files: int
    ) -> AsyncIterator[tuple[tuple[str, bytes], ...]]:
        """Pass 2: `(path, content)` of the files of `paths` within the read limit, in
        archive order, at most `batch_files` per batch; a symlink is never read."""
        ...
