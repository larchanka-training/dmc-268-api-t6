"""The RAG index a reader derives from a state row, with the toggle on (#144).

The review's gate and its graph read both run only for a repository whose RAG is on,
so neither reads the toggle: no row is an `empty` index, never `disabled`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.domain.rag_index import RagIndex, rag_index


class StateRow(Protocol):
    """The columns of a state row the index is derived from: the store's `IndexState`
    or the ORM row `CodeIndexState`."""

    @property
    def status(self) -> IndexStatus: ...

    @property
    def indexed_commit_sha(self) -> str | None: ...

    @property
    def target_commit_sha(self) -> str | None: ...

    @property
    def published_at(self) -> datetime | None: ...

    @property
    def file_count(self) -> int: ...

    @property
    def symbol_count(self) -> int: ...

    @property
    def error_code(self) -> str | None: ...

    @property
    def last_error_code(self) -> str | None: ...


def enabled_rag_index(state: StateRow | None) -> RagIndex:
    """The index of a repository whose RAG is on; ``None`` is no state row yet."""
    if state is None:
        return rag_index(
            rag_enabled=True,
            status=None,
            indexed_commit_sha=None,
            target_commit_sha=None,
            published_at=None,
            file_count=None,
            symbol_count=None,
            error_code=None,
            last_error_code=None,
        )
    return rag_index(
        rag_enabled=True,
        status=state.status,
        indexed_commit_sha=state.indexed_commit_sha,
        target_commit_sha=state.target_commit_sha,
        published_at=state.published_at,
        file_count=state.file_count,
        symbol_count=state.symbol_count,
        error_code=state.error_code,
        last_error_code=state.last_error_code,
    )
