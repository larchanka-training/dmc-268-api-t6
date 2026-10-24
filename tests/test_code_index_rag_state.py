"""The RAG index a reader derives from a state row with the toggle on (#144)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.modules.code_index.domain.enums import IndexStatus, RagIndexStatus
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.code_index.infrastructure.rag_state import enabled_rag_index

_INDEXED = "1" * 40
_TARGET = "2" * 40
_PUBLISHED = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)


@dataclass(frozen=True)
class Row:
    """A state row: distinct values in every column, so a swap shows."""

    status: IndexStatus
    target_commit_sha: str | None = _INDEXED
    error_code: str | None = None
    indexed_commit_sha: str | None = _INDEXED
    published_at: datetime | None = _PUBLISHED
    file_count: int = 3
    symbol_count: int = 9
    last_error_code: str | None = "limit_exceeded:files"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        (
            Row(IndexStatus.READY),
            RagIndex(RagIndexStatus.READY, _INDEXED, _PUBLISHED, 3, 9, "limit_exceeded:files"),
        ),
        (
            Row(IndexStatus.READY, target_commit_sha=_TARGET),
            RagIndex(RagIndexStatus.STALE, _INDEXED, _PUBLISHED, 3, 9, "limit_exceeded:files"),
        ),
        (
            Row(IndexStatus.FAILED, error_code="archive_unsafe"),
            RagIndex(RagIndexStatus.FAILED, None, None, 0, 0, "archive_unsafe"),
        ),
        (
            Row(IndexStatus.BUILDING),
            RagIndex(RagIndexStatus.BUILDING, None, None, 0, 0, "limit_exceeded:files"),
        ),
        (
            Row(IndexStatus.EMPTY),
            RagIndex(RagIndexStatus.EMPTY, None, None, 0, 0, "limit_exceeded:files"),
        ),
        # No row yet: an enabled repository's index is empty, never disabled.
        (None, RagIndex(RagIndexStatus.EMPTY, None, None, 0, 0, None)),
    ],
    ids=["ready", "stale", "failed", "building", "empty", "no_row"],
)
def test_a_state_row_gives_the_index_readers_see(row: Row | None, expected: RagIndex) -> None:
    assert enabled_rag_index(row) == expected
