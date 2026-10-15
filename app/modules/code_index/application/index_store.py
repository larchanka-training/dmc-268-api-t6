"""Port of the graph index store, fenced per ADR 0001 §6.2 (#141).

Rows of one attempt are written invisibly under its publication number and become
visible only through the publication compare-and-set. Labels T1–T7 name the steps of
ADR §6.2 that each method implements.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import SymbolKey
from app.modules.reviews.application.ast_resolution import ImportResolution


class IndexLeaseLost(Exception):
    """The fence no longer holds: the lease was taken over, revoked or expired, or the
    generation moved on (purge, toggle). The attempt stops without writing or retrying."""


@dataclass(frozen=True)
class IndexFence:
    """The lease of one indexing attempt: its token, generation and publication number."""

    token: UUID
    generation: int
    publication: int


@dataclass(frozen=True)
class IndexedSymbol:
    key: SymbolKey
    start_line: int
    end_line: int


@dataclass(frozen=True)
class IndexedEdge:
    """An edge from the file at `path`; an `imports` edge has an empty `target_name`."""

    path: str
    kind: EdgeKind
    target_path: str
    target_name: str


@dataclass(frozen=True)
class ImportRecord:
    """One import of the file at `path` with its outcome, resolved or not."""

    path: str
    specifier: str
    resolution: ImportResolution
    target_path: str | None


@dataclass(frozen=True)
class IndexedFile:
    """Every row of one file; `replace_file` writes them together."""

    path: str
    blob_sha: str
    symbols: tuple[IndexedSymbol, ...] = ()
    edges: tuple[IndexedEdge, ...] = ()
    imports: tuple[ImportRecord, ...] = ()


@dataclass(frozen=True)
class EdgeTarget:
    """An outgoing edge joined by name to visible rows.

    `target_blob_sha` is the visible file at `target_path`; `target_symbols` are its
    visible symbols named `target_name` (none for `imports`). An edge whose target is
    not visible is unresolved: no file, and no symbols for `uses_imported`.
    """

    edge: IndexedEdge
    target_blob_sha: str | None
    target_symbols: tuple[IndexedSymbol, ...]


@dataclass(frozen=True)
class IndexProfile:
    """Versions a publication was built with; a change of any of them forces full."""

    extractor_version: str
    resolver_version: str
    parser_profile_version: str
    limits_profile_version: str


@dataclass(frozen=True)
class IndexBuildStats:
    downloaded_bytes: int
    build_duration_ms: int
    github_request_count: int


@dataclass(frozen=True)
class IndexState:
    """The state row of a repository's index; the lease token is never exposed."""

    generation: int
    status: IndexStatus
    purge_reason: PurgeReason | None
    error_code: str | None
    last_error_code: str | None
    indexed_commit_sha: str | None
    target_commit_sha: str | None
    reserved_publication: int
    published_publication: int
    floor_publication: int
    published_at: datetime | None
    last_used_at: datetime | None
    lease_expires_at: datetime | None
    file_count: int
    symbol_count: int
    edge_count: int
    profile: IndexProfile | None
    stats: IndexBuildStats
    updated_at: datetime


class CodeIndexStore(Protocol):
    """Index rows of one repository per call; flushes, never commits.

    Every method takes `repository_id` first and reads or writes only that repository's
    rows. Reads see only the published rows of the current generation of a `ready` index.
    Each read statement is consistent on its own, but a read of several statements
    (`get_file`, `outgoing_edges`, or a sequence of reads) sees one snapshot only inside
    the caller's REPEATABLE READ read-only transaction (ADR §6.2): the #144 retriever
    opens one.
    """

    async def get_state(self, repository_id: UUID) -> IndexState | None: ...

    async def enable(self, repository_id: UUID) -> IndexState:
        """T1': create the state row, or re-arm a purged one with a new generation."""
        ...

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        """T1: logical purge; a missing or already purged row keeps its generation.

        A repeated purge changes nothing, except that `removed` or `disabled` replaces a
        `ttl` reason.
        """
        ...

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int
    ) -> IndexFence | None:
        """T2: `None` when the generation differs, a live lease exists or the row is purged."""
        ...

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, limit: int) -> int:
        """T3: delete up to `limit` rows of abandoned attempts; returns the deleted count."""
        ...

    async def replace_file(self, repository_id: UUID, fence: IndexFence, file: IndexedFile) -> None:
        """T4: replace this attempt's rows of `file.path`; raises `IndexLeaseLost`."""
        ...

    async def publish(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        base_commit_sha: str | None,
        commit_sha: str,
        full: bool,
        deleted_paths: Sequence[str],
        profile: IndexProfile,
        stats: IndexBuildStats,
    ) -> bool:
        """T5: publication compare-and-set; raises `IndexLeaseLost`, `False` on another miss.

        Both a publication and a miss spend the lease.
        """
        ...

    async def revoke_expired_lease(self, repository_id: UUID) -> bool:
        """T7: clear an expired lease; returns whether one was revoked."""
        ...

    async def collect_garbage(self, repository_id: UUID, limit: int) -> int:
        """T7: delete up to `limit` invisible rows; returns the deleted count."""
        ...

    async def get_file(self, repository_id: UUID, path: str) -> IndexedFile | None:
        """T6: the visible file with its visible symbols, edges and imports."""
        ...

    async def file_symbols(self, repository_id: UUID, path: str) -> list[IndexedSymbol]: ...

    async def outgoing_edges(self, repository_id: UUID, path: str) -> list[EdgeTarget]: ...

    async def incoming_edges(
        self, repository_id: UUID, target_path: str, target_name: str
    ) -> list[IndexedEdge]: ...

    async def importers_of(self, repository_id: UUID, target_path: str) -> list[ImportRecord]: ...


class CodeIndexUnitOfWork(UnitOfWork, Protocol):
    @property
    def index(self) -> CodeIndexStore: ...
