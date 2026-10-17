"""The RAG index status readers see: the repository toggle over the state row (#145)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.modules.code_index.domain.enums import IndexStatus, RagIndexStatus

_USABLE = frozenset({RagIndexStatus.READY, RagIndexStatus.STALE})
_NOT_READY = frozenset({RagIndexStatus.EMPTY, RagIndexStatus.BUILDING, RagIndexStatus.FAILED})


@dataclass(frozen=True)
class RagIndex:
    """Pointers and counters only, no code text; the publication fields (commit, time,
    counts) are filled only for a ready or stale index, otherwise null or 0."""

    status: RagIndexStatus
    indexed_commit_sha: str | None
    indexed_at: datetime | None
    file_count: int
    symbol_count: int
    last_error_code: str | None

    @property
    def usable(self) -> bool:
        """A review may read the index (a stale one still answers for its commit)."""
        return self.status in _USABLE

    @property
    def not_ready_reason(self) -> RagIndexStatus | None:
        """Why an enabled index cannot serve a review yet; None when usable or disabled."""
        return self.status if self.status in _NOT_READY else None


def rag_index(
    *,
    rag_enabled: bool,
    status: IndexStatus | None,
    indexed_commit_sha: str | None,
    target_commit_sha: str | None,
    published_at: datetime | None,
    file_count: int | None,
    symbol_count: int | None,
    error_code: str | None,
    last_error_code: str | None,
) -> RagIndex:
    """Derive the status from the toggle and the state row; ``status`` None means no row.

    The state columns arrive as an outer join reads them, so a missing row gives None
    counts as well.
    """
    if not rag_enabled:
        return RagIndex(RagIndexStatus.DISABLED, None, None, 0, 0, None)
    if status is None:
        return RagIndex(RagIndexStatus.EMPTY, None, None, 0, 0, None)
    shown = RagIndexStatus(status.value)
    if (
        shown is RagIndexStatus.READY
        and target_commit_sha is not None
        and target_commit_sha != indexed_commit_sha
    ):
        shown = RagIndexStatus.STALE
    code = error_code if shown is RagIndexStatus.FAILED else last_error_code
    if shown not in _USABLE:
        return RagIndex(shown, None, None, 0, 0, code)
    return RagIndex(
        shown, indexed_commit_sha, published_at, file_count or 0, symbol_count or 0, code
    )
