"""Provider-neutral PR file inputs and the existing L1 diff representation."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.commit_prefix_limits import (
    CommitMessagePrefix as CommitMessagePrefix,
)
from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    FileStatus,
    PullRequestMeta,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.unified_diff import HunkRow, parse_hunks, split_lf

_LOGGER = logging.getLogger(__name__)
_MAX_PATCH_LINES = 20_000
_MAX_PATCH_BYTES = 1_048_576
MAX_BLOB_BYTES = 1_048_576
_LOCK_FILES = frozenset(
    {
        "package-lock.json",
        "pnpm-lock.yaml",
        "yarn.lock",
        "bun.lock",
        "bun.lockb",
        "cargo.lock",
        "gemfile.lock",
        "poetry.lock",
        "uv.lock",
        "pipfile.lock",
        "composer.lock",
        "go.sum",
    }
)


@dataclass(frozen=True)
class PullRequestLocator:
    installation_external_id: int
    repository_full_name: str
    number: int


@dataclass(frozen=True)
class VcsPullRequest:
    locator: PullRequestLocator
    head_sha: str
    base_sha: str
    meta: PullRequestMeta


@dataclass(frozen=True)
class VcsFile:
    filename: str
    status: FileStatus
    blob_sha: str | None
    previous_filename: str | None
    additions: int
    deletions: int
    changes: int
    patch: str | None
    size: int | None = None


class OmissionReason(StrEnum):
    BINARY = "binary"
    TOO_LARGE = "too_large"
    GENERATED = "generated"
    MISSING_PATCH = "missing_patch"


class BlobTooLargeError(ValueError):
    """The provider stopped a blob retrieval at the review size budget."""


@dataclass(frozen=True)
class VcsFileOmission:
    file: VcsFile
    reason: OmissionReason


@dataclass(frozen=True)
class FetchedVcsReviewInput:
    pull_request: VcsPullRequest
    files: tuple[VcsFile, ...]
    changed_files: tuple[ChangedFile, ...]
    omitted_files: tuple[str, ...]
    omissions: tuple[VcsFileOmission, ...]
    failed_blob_shas: frozenset[str]


class VcsProvider(Protocol):
    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest: ...

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]: ...

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes: ...


class CommitMessagesProvider(Protocol):
    """A bounded API-order commit prefix, fetched independently of ordinary PR reads."""

    async def get_commit_messages(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> CommitMessagePrefix: ...


class VcsMetadataProvider(VcsProvider, CommitMessagesProvider, Protocol):
    """Capabilities consumed by the run's metadata and repository context source."""


class MergeBaseUnavailable(Exception):
    """The provider could not tell the merge base; the old side of the diff stays unknown."""


class MergeBaseProvider(Protocol):
    """Resolves the old side of a pull request diff: the merge base of its base and head.

    An implementation raises ``MergeBaseUnavailable`` for any failure of its transport or
    of the provider answer, never an error of its own client library.
    """

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str: ...


class FetchVcsReviewInput:
    """Fetch current-head L1 inputs without a database transaction."""

    def __init__(
        self,
        provider: VcsProvider,
        *,
        checkpoint: RunProviderCheckpoint,
        run_id: UUID | None = None,
    ) -> None:
        self._provider = provider
        self._checkpoint = checkpoint
        self._run_id = run_id

    async def execute(
        self,
        locator: PullRequestLocator,
        expected_head_sha: str,
        expected_base_sha: str | None = None,
    ) -> FetchedVcsReviewInput:
        pull_request = await self._provider.get_pull_request(locator, checkpoint=self._checkpoint)
        if pull_request.locator != locator or pull_request.head_sha != expected_head_sha:
            raise ValueError("GitHub pull request head SHA changed")
        if expected_base_sha is not None and pull_request.base_sha != expected_base_sha:
            raise ValueError("GitHub pull request base SHA changed")
        files = await self._provider.get_diff(pull_request, checkpoint=self._checkpoint)
        changed: list[ChangedFile] = []
        omitted: list[VcsFileOmission] = []
        failed_blob_shas: set[str] = set()
        for file in files:
            reason = _known_omission(file)
            if reason is None and not file.patch:
                if file.blob_sha is None or file.blob_sha in failed_blob_shas:
                    reason = OmissionReason.MISSING_PATCH
                else:
                    try:
                        blob = await self._provider.get_blob(
                            locator, file.blob_sha, checkpoint=self._checkpoint
                        )
                    except BlobTooLargeError:
                        reason = OmissionReason.TOO_LARGE
                    except RunCancelled:
                        raise
                    except Exception:
                        # Blob classification is best effort; retain this file as an omission.
                        _LOGGER.warning(
                            "VCS blob classification failed run_id=%s repository=%s "
                            "pr_number=%s file=%s blob_sha=%s",
                            self._run_id,
                            locator.repository_full_name,
                            locator.number,
                            file.filename,
                            file.blob_sha,
                            exc_info=True,
                        )
                        failed_blob_shas.add(file.blob_sha)
                        reason = OmissionReason.MISSING_PATCH
                    else:
                        if len(blob) > MAX_BLOB_BYTES:
                            reason = OmissionReason.TOO_LARGE
                        elif _is_binary(blob):
                            reason = OmissionReason.BINARY
                        else:
                            reason = OmissionReason.MISSING_PATCH
            if reason is None:
                assert file.patch is not None
                changed.append(parse_file_patch(file.filename, file.status, file.patch))
            else:
                omitted.append(VcsFileOmission(file, reason))
        return FetchedVcsReviewInput(
            pull_request=pull_request,
            files=files,
            changed_files=tuple(changed),
            omitted_files=tuple(item.file.filename for item in omitted),
            omissions=tuple(omitted),
            failed_blob_shas=frozenset(failed_blob_shas),
        )


def _known_omission(file: VcsFile) -> OmissionReason | None:
    if _is_generated(file.filename):
        return OmissionReason.GENERATED
    if file.patch is not None and "\x00" in file.patch:
        return OmissionReason.BINARY
    if (
        file.changes > _MAX_PATCH_LINES
        or (file.size is not None and file.size > MAX_BLOB_BYTES)
        or (
            file.patch is not None
            and (
                len(file.patch.encode("utf-8")) > _MAX_PATCH_BYTES
                or len(_patch_lines(file.patch)) > _MAX_PATCH_LINES
            )
        )
    ):
        return OmissionReason.TOO_LARGE
    return None


def _is_generated(path: str) -> bool:
    parts = path.casefold().split("/")
    filename = parts[-1]
    return (
        filename in _LOCK_FILES
        or filename.endswith((".min.js", ".pb.go"))
        or (len(parts) > 1 and parts[0] == "dist")
        or ("__snapshots__" in parts[:-1] and filename.endswith(".snap"))
        or ("migrations" in parts[:-1] and "snapshot" in filename)
    )


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192] or blob.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"%PDF-", b"PK\x03\x04", b"\x7fELF")
    )


def _patch_lines(patch: str) -> list[str]:
    """Patch lines for the L1 prompt input: a trailing CR is dropped from every line."""
    return [line[:-1] if line.endswith("\r") else line for line in split_lf(patch)]


def parse_file_patch(path: str, status: FileStatus, patch: str) -> ChangedFile:
    """Parse GitHub's per-file hunks, preserving old-side removal anchors."""
    lines = tuple(
        DiffLine(_line_number(row), row.kind, row.text)
        for hunk in parse_hunks(_patch_lines(patch))
        for row in hunk.rows
    )
    return ChangedFile(path, status, lines)


def _line_number(row: HunkRow) -> int:
    """A removed line keeps its old number; every other line is numbered by the new side."""
    number = row.old_line if row.kind == "removed" else row.new_line
    assert number is not None
    return number
