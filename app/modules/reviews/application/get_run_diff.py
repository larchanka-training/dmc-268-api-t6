"""Application services and contracts for persisted per-file run diffs."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.prompt_builder import ChangedFile, FileStatus
from app.modules.reviews.application.vcs_diff import (
    VcsFile,
    is_proven_empty_addition,
    parse_file_patch,
)

MAX_DIFF_LINES = 3000


@dataclass(frozen=True)
class DiffSnapshot:
    filename: str
    patch: str | None
    blob_sha: str | None = None
    status: FileStatus = "modified"
    previous_filename: str | None = None
    additions: int = 0
    deletions: int = 0
    changes: int = 0
    omission_reason: str | None = None
    review_patch: str | None = None
    summary_only: bool = False


def is_proven_empty_snapshot(snapshot: DiffSnapshot) -> bool:
    """Revalidate an empty addition whose available evidence passed during fetch.

    The stored omission is required: size and Git mode are not persisted, so proof
    must have passed the fetcher's conflicting-size and regular-file checks.
    Only the exact wrapper produced for an empty provider patch is stripped.
    """
    if (
        snapshot.omission_reason != "empty_file"
        or snapshot.summary_only
        or snapshot.review_patch is not None
    ):
        return False
    patch = snapshot.patch
    empty_wrapper = (
        f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
        f"--- a/{snapshot.filename}\n"
        f"+++ b/{snapshot.filename}\n"
    )
    if patch == empty_wrapper:
        patch = ""
    return is_proven_empty_addition(
        VcsFile(
            filename=snapshot.filename,
            status=snapshot.status,
            blob_sha=snapshot.blob_sha,
            previous_filename=snapshot.previous_filename,
            additions=snapshot.additions,
            deletions=snapshot.deletions,
            changes=snapshot.changes,
            patch=patch,
            git_mode="100644",  # The persisted marker certifies a checked regular mode.
        )
    )


class RunDiffRepository(Protocol):
    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None: ...


class DiffSnapshotRepository(Protocol):
    async def store_diff_snapshots(
        self,
        run_id: UUID,
        code_change_id: UUID,
        head_sha: str,
        snapshots: list[DiffSnapshot],
    ) -> list[DiffSnapshot]: ...


class GetRunDiff:
    def __init__(self, repository: RunDiffRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return await self._repository.get_run_diff(run_id)


class StoreDiffSnapshot:
    """Persist every UI patch and mark oversized model input as summary only."""

    def __init__(self, repository: DiffSnapshotRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        *,
        run_id: UUID,
        code_change_id: UUID,
        head_sha: str,
        files: list[DiffSnapshot],
        excluded: Callable[[DiffSnapshot], bool] | None = None,
    ) -> list[DiffSnapshot]:
        """Store the diff; ``excluded`` names files the policy withholds from a model.

        An excluded file keeps its UI patch and gets no model patch. It does not count
        toward the summary-only threshold: 4 000 ignored lines next to 50 allowed ones
        are an ordinary review of those 50.
        """
        withheld = [excluded is not None and excluded(file) for file in files]
        summary_only = over_max_diff_lines(files, excluded=excluded)
        snapshots = []
        for file, skip in zip(files, withheld, strict=True):
            snapshot = _normalize_snapshot(file)
            if skip:
                snapshot = replace(snapshot, review_patch=None)
            elif summary_only:
                snapshot = replace(snapshot, review_patch=None, summary_only=True)
            snapshots.append(snapshot)
        return await self._repository.store_diff_snapshots(
            run_id, code_change_id, head_sha, snapshots
        )


def stored_review_patch(patch: str | None, omission_reason: str | None) -> str | None:
    """Model patch of a stored UI patch. A git header contributes only its hunk."""
    if patch is None or omission_reason is not None:
        return None
    if patch.startswith("diff --git "):
        hunk_start = patch.find("\n@@ ")
        if hunk_start < 0:
            return None
        return patch[hunk_start + 1 :]
    return patch


def _normalize_snapshot(snapshot: DiffSnapshot) -> DiffSnapshot:
    if snapshot.patch is None:
        return snapshot
    review_patch = stored_review_patch(snapshot.patch, snapshot.omission_reason)
    if snapshot.patch.startswith("diff --git "):
        return replace(snapshot, review_patch=review_patch)
    return replace(
        snapshot,
        review_patch=review_patch,
        patch=(
            f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
            f"--- a/{snapshot.filename}\n"
            f"+++ b/{snapshot.filename}\n"
            f"{snapshot.patch}"
        ),
    )


# Binary and generated files are exclusions and add nothing to the threshold. A text file
# whose patch is missing or too large is incompleteness: its lines still count.
_UNCOUNTED_OMISSIONS = frozenset({"binary", "generated"})


def over_max_diff_lines(
    files: list[DiffSnapshot],
    *,
    excluded: Callable[[DiffSnapshot], bool] | None = None,
) -> bool:
    """Whether this change is over ``MAX_DIFF_LINES`` by the store's own counter.

    With an exclusion predicate the counter is added and removed lines of the allowed
    files. Without one it is the patch line count of files that have a patch. The
    comparison is ``>``.
    """
    if excluded is None:
        return _total_line_count(files) > MAX_DIFF_LINES
    allowed = [file for file in files if not excluded(file)]
    return _changed_line_count(allowed) > MAX_DIFF_LINES


def _changed_line_count(files: list[DiffSnapshot]) -> int:
    """Added and removed lines of the allowed changes by the counters of the VCS.

    Hunk headers and context lines of a patch are not changes, and a file whose patch
    the provider did not send is not zero changes.
    """
    return sum(
        file.additions + file.deletions
        for file in files
        if file.omission_reason not in _UNCOUNTED_OMISSIONS
    )


def _line_count(patch: str) -> int:
    return patch.count("\n") + (0 if patch.endswith("\n") else 1)


def _total_line_count(files: list[DiffSnapshot]) -> int:
    return sum(
        _line_count(file.patch)
        for file in files
        if file.patch is not None and file.omission_reason is None
    )


def review_files_from_snapshots(
    snapshots: list[DiffSnapshot],
) -> tuple[tuple[ChangedFile, ...], tuple[str, ...]]:
    """Project the durable snapshot into model input without omitted files.

    ``review_patch`` is the only model input (RULES §5.1). A row without one, withheld by
    the policy or stored before the column existed, is omitted under its stored name; its
    UI patch is never parsed again.
    """
    changed: list[ChangedFile] = []
    omitted: list[str] = []
    for snapshot in snapshots:
        if (
            snapshot.summary_only
            or snapshot.omission_reason is not None
            or snapshot.patch is None
            or snapshot.review_patch is None
        ):
            omitted.append(snapshot.filename)
        else:
            changed.append(
                parse_file_patch(snapshot.filename, snapshot.status, snapshot.review_patch)
            )
    return tuple(changed), tuple(omitted)
