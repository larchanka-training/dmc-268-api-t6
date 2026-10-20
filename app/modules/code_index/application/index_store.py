"""Port of the graph index store, fenced per ADR 0001 §6.2 (#141).

Rows of one attempt are written invisibly under its publication number and become
visible only through the publication compare-and-set. Labels T1–T7 name the steps of
ADR §6.2 that each method implements.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
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
    """What a publication was built with; a change of any of it forces full."""

    extractor_version: str
    resolver_version: str
    parser_profile_version: str
    limits_profile_version: str
    default_branch: str
    # `IgnorePolicy.digest` of `.review/rules.md` at the published commit.
    exclusions_digest: str


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
    # Estimated bytes (`domain/storage.py`) of the published rows and of the leased
    # attempt's rows.
    stored_bytes: int
    pending_bytes: int
    profile: IndexProfile | None
    stats: IndexBuildStats
    updated_at: datetime


class ClaimRefusal(StrEnum):
    """Why `claim_lease` granted no lease, in the order the claim checks it."""

    STALE = "stale"  # no row, purged, another generation, or `failed`
    LEASED = "leased"  # this repository has a live lease
    QUOTA = "quota"  # a storage quota is exceeded; the claim wrote `failed`
    BUSY = "busy"  # the concurrency limit is reached; the job waits in the queue


@dataclass(frozen=True)
class IndexAdmission:
    """The ADR §7 limits a claim admits a job under."""

    max_leases: int
    max_installation_leases: int
    repository_storage_bytes: int
    workspace_storage_bytes: int


@dataclass(frozen=True)
class LeaseClaim:
    """A granted lease and the target commit snapshot taken with it."""

    fence: IndexFence
    target_commit_sha: str | None


@dataclass(frozen=True)
class ClaimRefused:
    """A refused claim; `error_code` is the `failed` code a QUOTA refusal wrote."""

    reason: ClaimRefusal
    error_code: str | None = None


@dataclass(frozen=True)
class IndexStorage:
    """A repository's estimated bytes after a write: published, and of this attempt; and
    its Workspace's: every repository's stored bytes and the pending bytes of its live
    lease, this write included."""

    stored_bytes: int
    pending_bytes: int
    workspace_bytes: int


@dataclass(frozen=True)
class IndexCounts:
    """Visible rows of some paths, to take out of the state counters (incremental)."""

    file_count: int
    symbol_count: int
    edge_count: int


class CodeIndexStore(Protocol):
    """Index rows of one repository per call; flushes, never commits.

    Every method takes `repository_id` first and reads or writes only that repository's
    rows; admission in `claim_lease` and the Workspace bytes of `replace_file` also read
    sums over other repositories' state rows, never their graph rows. Reads see only the
    published rows of the current generation of a `ready` index.
    Each read statement is consistent on its own, but a read of several statements
    (`get_file`, `outgoing_edges`, or a sequence of reads) sees one snapshot only inside
    the caller's REPEATABLE READ read-only transaction (ADR §6.2): the #144 retriever
    opens one.
    """

    async def get_state(self, repository_id: UUID) -> IndexState | None: ...

    async def enable(self, repository_id: UUID) -> IndexState:
        """T1': create the state row, or re-arm a purged one with a new generation.

        Creation and re-arm request a build through the wake-up marker; a live or a
        `failed` index stays as it is.
        """
        ...

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        """T1: logical purge; a missing or already purged row keeps its generation.

        A repeated purge changes nothing, except that `removed` or `disabled` replaces a
        `ttl` reason.
        """
        ...

    async def purge_unused(self, repository_id: UUID, unused_for: timedelta) -> bool:
        """T1 for the TTL (ADR §4): `purge` with `ttl` if the index is still unused.

        Re-checked under FOR UPDATE against `clock_timestamp()`: only a live index (no
        purge reason, not `failed`) whose `last_used_at` is older than `unused_for`. A
        `failed` index stays: a TTL purge would turn it into `empty` and reopen the
        repeated full build that ADR §7 forbids. Returns whether it purged.
        """
        ...

    async def request_build(self, repository_id: UUID, *, target_commit_sha: str | None) -> bool:
        """A trigger of ADR §6.1: request a build of a live index up to its target.

        Under FOR UPDATE, only for a row without a purge reason that is not `failed`:
        writes `target_commit_sha` when given and always sets a fresh wake-up marker, so
        the leader's compare-and-set clear of an older marker misses. Returns whether
        it requested; a missing, purged or `failed` row is left alone.
        """
        ...

    async def reconcile_head(
        self,
        repository_id: UUID,
        *,
        generation: int,
        branch: str,
        commit_sha: str,
        read_at: datetime,
        versions_current: Callable[[IndexProfile], bool],
    ) -> bool:
        """The leader reconcile of ADR §6.1 with the live default-branch head.

        Under FOR UPDATE; a missing row, another generation, a purged or a `failed` row
        is left alone, and so is a row whose wake-up marker is later than `read_at`, a
        database time taken before the head was read: a trigger after the read wrote
        its own target, which a stale head must not overwrite. Otherwise the target
        becomes `commit_sha`, which clears a target a late delivery left behind, and the
        wake-up marker is set when the publication differs from (`branch`,
        `commit_sha`), when there is none, or when `versions_current` rejects the
        profile of the locked row. Returns whether it set the marker.
        """
        ...

    async def clear_wake_request(self, repository_id: UUID, requested_at: datetime) -> bool:
        """Clear the wake-up marker if it is still the one read at `requested_at`.

        A compare-and-set: a request made after the read keeps its marker for the next
        dispatch. Returns whether it cleared.
        """
        ...

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int, admission: IndexAdmission
    ) -> LeaseClaim | ClaimRefused:
        """T2 with admission (ADR §7), checked in the order of `ClaimRefusal`.

        A QUOTA refusal writes `failed` as `mark_failed` does and records zero job
        metrics (the refused job made no GitHub request), so the caller commits a
        refusal too. A grant resets the pending bytes and turns `empty` into `building`.
        The pending bytes of another repository's expired lease do not count.

        It must be the first row-locking call of its own transaction: its admission
        advisory lock orders before every state row lock. Each store call runs in a
        short transaction of its own.
        """
        ...

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, limit: int) -> int:
        """T3: delete up to `limit` rows of abandoned attempts; returns the deleted count."""
        ...

    async def replace_file(
        self, repository_id: UUID, fence: IndexFence, file: IndexedFile
    ) -> IndexStorage:
        """T4: replace this attempt's rows of `file.path`; raises `IndexLeaseLost`.

        The pending bytes gain the new rows and lose the replaced ones, so a rewrite of
        a path is counted once. It takes the admission lock of `claim_lease` and then
        the state row, so batches, claims and publications run one at a time and each
        batch reads the Workspace bytes after every earlier batch and publication
        committed; a purge waits for the batch. The caller checks both storage quotas
        on the returned `IndexStorage` and raises before its commit, so a batch over a
        quota rolls back.

        Like `claim_lease`, its transaction takes no state row lock before its first
        `replace_file`: the admission lock orders before every state row lock.
        """
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

        Both a publication and a miss spend the lease. A publication moves the estimate
        of its visible rows into the stored bytes and clears `last_error_code`. A miss
        records `stats` as `release` does; the counters and stored bytes stay those of
        the visible publication.

        It takes the admission lock before the state row, so a batch of another build
        waits for the publication, even one whose lease expired meanwhile, and then
        counts its stored bytes. Like `claim_lease`, its transaction takes no state row
        lock before it.
        """
        ...

    async def mark_failed(
        self,
        repository_id: UUID,
        fence: IndexFence,
        error_code: str,
        *,
        stats: IndexBuildStats | None = None,
    ) -> None:
        """ADR §7: fail the index with a code of `FAILED_ERROR_CODES`, else `ValueError`.

        Spends the lease; the publication is no longer used, so garbage collection
        removes every row. `None` keeps the recorded stats. Raises `IndexLeaseLost`.
        """
        ...

    async def release(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        last_error_code: str | None = None,
        stats: IndexBuildStats | None = None,
    ) -> IndexStatus:
        """Spend the lease without publishing: `ready` with a publication, else `empty`.

        `stats` records the job's metrics; the file, symbol and edge counters and the
        stored bytes stay those of the visible publication. `None` keeps the previous
        `last_error_code` or the recorded stats. Raises `IndexLeaseLost`.
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

    async def iter_files(
        self, repository_id: UUID, after: str | None, limit: int
    ) -> list[tuple[str, str]]:
        """Up to `limit` visible `(path, blob_sha)` pairs after the path `after`, by path."""
        ...

    async def iter_imports(
        self, repository_id: UUID, after: ImportRecord | None, limit: int
    ) -> list[ImportRecord]:
        """Up to `limit` visible import records after `after`, by path, specifier and
        resolution."""
        ...

    async def visible_counts(self, repository_id: UUID, paths: Sequence[str]) -> IndexCounts:
        """The visible files, symbols and edges of `paths`, read in one statement."""
        ...


class CodeIndexUnitOfWork(UnitOfWork, Protocol):
    @property
    def index(self) -> CodeIndexStore: ...
