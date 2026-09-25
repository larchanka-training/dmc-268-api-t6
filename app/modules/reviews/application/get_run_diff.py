"""Application services and contracts for persisted per-file run diffs."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

MAX_DIFF_LINES = 3000


@dataclass(frozen=True)
class DiffSnapshot:
    filename: str
    patch: str | None


class RunDiffRepository(Protocol):
    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None: ...


class DiffSnapshotRepository(Protocol):
    async def replace_diff_snapshots(
        self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> None: ...


class GetRunDiff:
    def __init__(self, repository: RunDiffRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> list[DiffSnapshot] | None:
        return await self._repository.get_run_diff(run_id)


class StoreDiffSnapshot:
    """Persist the immutable diff used by a review run.

    The frontend contract deliberately represents oversized files as the same
    ``{filename, patch: null}`` shape as a provider that supplies no text patch.
    """

    def __init__(self, repository: DiffSnapshotRepository) -> None:
        self._repository = repository

    async def execute(
        self, *, code_change_id: UUID, head_sha: str, files: list[DiffSnapshot]
    ) -> None:
        snapshots = (
            [DiffSnapshot(filename=file.filename, patch=None) for file in files]
            if _total_line_count(files) > MAX_DIFF_LINES
            else [_normalize_snapshot(file) for file in files]
        )
        await self._repository.replace_diff_snapshots(code_change_id, head_sha, snapshots)


def _normalize_snapshot(snapshot: DiffSnapshot) -> DiffSnapshot:
    if snapshot.patch is None:
        return DiffSnapshot(
            filename=snapshot.filename,
            patch=(
                f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
                f"Binary files a/{snapshot.filename} and b/{snapshot.filename} differ"
            ),
        )
    if snapshot.patch.startswith("diff --git "):
        return snapshot
    return DiffSnapshot(
        filename=snapshot.filename,
        patch=(
            f"diff --git a/{snapshot.filename} b/{snapshot.filename}\n"
            f"--- a/{snapshot.filename}\n"
            f"+++ b/{snapshot.filename}\n"
            f"{snapshot.patch}"
        ),
    )


def _line_count(patch: str) -> int:
    return patch.count("\n") + 1


def _total_line_count(files: list[DiffSnapshot]) -> int:
    return sum(_line_count(file.patch) for file in files if file.patch is not None)
