"""Provider-neutral PR file inputs and the existing L1 diff representation."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    DiffLine,
    FileStatus,
    PullRequestMeta,
)

_LOGGER = logging.getLogger(__name__)
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
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
    async def get_pull_request(self, locator: PullRequestLocator) -> VcsPullRequest: ...

    async def get_diff(self, pull_request: VcsPullRequest) -> tuple[VcsFile, ...]: ...

    async def get_blob(self, locator: PullRequestLocator, sha: str) -> bytes: ...


class FetchVcsReviewInput:
    """Fetch current-head L1 inputs without a database transaction."""

    def __init__(self, provider: VcsProvider, *, run_id: UUID | None = None) -> None:
        self._provider = provider
        self._run_id = run_id

    async def execute(
        self,
        locator: PullRequestLocator,
        expected_head_sha: str,
        expected_base_sha: str | None = None,
    ) -> FetchedVcsReviewInput:
        pull_request = await self._provider.get_pull_request(locator)
        if pull_request.locator != locator or pull_request.head_sha != expected_head_sha:
            raise ValueError("GitHub pull request head SHA changed")
        if expected_base_sha is not None and pull_request.base_sha != expected_base_sha:
            raise ValueError("GitHub pull request base SHA changed")
        files = await self._provider.get_diff(pull_request)
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
                        blob = await self._provider.get_blob(locator, file.blob_sha)
                    except BlobTooLargeError:
                        reason = OmissionReason.TOO_LARGE
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
        or filename.endswith((".lock", ".lockb"))
        or filename.endswith((".min.js", ".pb.go", ".snap"))
        or any(
            part in {"dist", "__snapshots__", "locale", "locales", "i18n", "l10n", "translations"}
            for part in parts[:-1]
        )
        or ("migrations" in parts[:-1] and "snapshot" in filename)
    )


def _is_binary(blob: bytes) -> bool:
    return b"\x00" in blob[:8192] or blob.startswith(
        (b"\x89PNG\r\n\x1a\n", b"\xff\xd8\xff", b"GIF8", b"%PDF-", b"PK\x03\x04", b"\x7fELF")
    )


def _patch_lines(patch: str) -> list[str]:
    """Split only on LF; Unicode line separators can be literal source content."""
    if not patch:
        return []
    lines = patch.split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def parse_file_patch(path: str, status: FileStatus, patch: str) -> ChangedFile:
    """Parse GitHub's per-file hunks, preserving old-side removal anchors."""
    lines: list[DiffLine] = []
    old_number = new_number = old_remaining = new_remaining = 0
    saw_hunk = False
    for raw_line in _patch_lines(patch):
        header = _HUNK.match(raw_line)
        if header is not None:
            if saw_hunk and (old_remaining or new_remaining):
                raise ValueError("GitHub diff hunk ended before its declared line counts")
            old_number, new_number = int(header.group(1)), int(header.group(3))
            old_remaining = int(header.group(2)) if header.group(2) is not None else 1
            new_remaining = int(header.group(4)) if header.group(4) is not None else 1
            saw_hunk = True
            continue
        if not saw_hunk:
            raise ValueError("GitHub diff has no hunk header")
        if raw_line == "\\ No newline at end of file":
            continue
        if raw_line.startswith("+"):
            lines.append(DiffLine(new_number, "added", raw_line[1:]))
            new_number += 1
            new_remaining -= 1
        elif raw_line.startswith("-"):
            lines.append(DiffLine(old_number, "removed", raw_line[1:]))
            old_number += 1
            old_remaining -= 1
        elif raw_line.startswith(" "):
            lines.append(DiffLine(new_number, "context", raw_line[1:]))
            old_number += 1
            new_number += 1
            old_remaining -= 1
            new_remaining -= 1
        else:
            raise ValueError("GitHub diff hunk has an unknown line")
        if old_remaining < 0 or new_remaining < 0:
            raise ValueError("GitHub diff hunk exceeded its declared line counts")
    if not saw_hunk or old_remaining or new_remaining:
        raise ValueError("GitHub diff hunk is incomplete")
    return ChangedFile(path, status, tuple(lines))
