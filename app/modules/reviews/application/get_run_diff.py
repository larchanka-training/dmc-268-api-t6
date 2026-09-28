"""Application services and contracts for persisted per-file run diffs."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.prompt_builder import (
    ChangedFile,
    FileStatus,
    parse_unified_diff,
)
from app.modules.reviews.application.vcs_diff import parse_file_patch

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
        self, *, run_id: UUID, code_change_id: UUID, head_sha: str, files: list[DiffSnapshot]
    ) -> list[DiffSnapshot]:
        summary_only = _total_line_count(files) > MAX_DIFF_LINES
        snapshots = [
            replace(_normalize_snapshot(file), review_patch=None, summary_only=True)
            if summary_only
            else _normalize_snapshot(file)
            for file in files
        ]
        return await self._repository.store_diff_snapshots(
            run_id, code_change_id, head_sha, snapshots
        )


def _normalize_snapshot(snapshot: DiffSnapshot) -> DiffSnapshot:
    if snapshot.patch is None:
        return snapshot
    if snapshot.patch.startswith("diff --git "):
        hunk_start = snapshot.patch.find("\n@@ ")
        return replace(
            snapshot,
            review_patch=(
                snapshot.patch[hunk_start + 1 :]
                if hunk_start >= 0 and snapshot.omission_reason is None
                else None
            ),
        )
    return replace(
        snapshot,
        review_patch=snapshot.patch if snapshot.omission_reason is None else None,
        patch=(
            f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
            f"--- a/{snapshot.filename}\n"
            f"+++ b/{snapshot.filename}\n"
            f"{snapshot.patch}"
        ),
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
    """Project the durable snapshot into model input without omitted files."""
    changed: list[ChangedFile] = []
    omitted: list[str] = []
    for snapshot in snapshots:
        if snapshot.summary_only or snapshot.omission_reason is not None or snapshot.patch is None:
            omitted.append(snapshot.filename)
        elif snapshot.review_patch is not None:
            changed.append(
                parse_file_patch(snapshot.filename, snapshot.status, snapshot.review_patch)
            )
        elif snapshot.blob_sha is not None:
            omitted.append(snapshot.filename)
        else:
            changed.extend(parse_unified_diff(snapshot.patch))
    return tuple(changed), tuple(omitted)
