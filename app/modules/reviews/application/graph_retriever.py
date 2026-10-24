"""The graph hints a review reads from the code index (#144, ADR 0001 §5, §6.2)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.rag_index import RagIndex


@dataclass(frozen=True)
class GraphTarget:
    """A changed symbol `name` of the file at `path`; `name` is "" for a module-level change,
    whose hints are the `imports` edges of the file."""

    path: str
    name: str


@dataclass(frozen=True)
class GraphHint:
    """A reverse edge: the file at `path` imports `target_path` or uses its `target_name`.

    A hint only; the review confirms it at the PR head.
    """

    path: str
    kind: EdgeKind
    target_path: str
    target_name: str


@dataclass(frozen=True)
class GraphHints:
    """One consistent read: ``index`` is the status read with the hints, and ``hints`` is
    empty unless that index is usable."""

    index: RagIndex
    hints: tuple[GraphHint, ...]

    @property
    def indexed_commit_sha(self) -> str | None:
        """The commit the hints belong to, read with them; None when the index is not usable."""
        return self.index.indexed_commit_sha


class GraphRetriever(Protocol):
    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        """The incoming edges of each target, in the order of the targets, read with the
        index status in one read-only snapshot of the repository's index.

        The read is a transaction of its own, closed before this returns, so no blob read
        happens inside it. An index that is not usable in this read gives its fresh status
        and no hints, even if the review's gate found it usable.
        """
        ...
