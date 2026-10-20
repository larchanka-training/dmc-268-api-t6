"""PostgreSQL adapter of the graph index store, fenced per ADR 0001 §6.2 (#141, #142).

Every statement filters by `repository_id`, except the admission sums of `claim_lease`
and the Workspace sum of `replace_file` over other repositories' state rows. The store
runs on the caller's session and never commits. Lease expiry is compared with
`clock_timestamp()` read after the state row is locked, never with the transaction start
time.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    ColumnElement,
    CursorResult,
    Enum,
    String,
    and_,
    case,
    delete,
    func,
    insert,
    literal,
    or_,
    select,
    text,
    true,
)
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.common.infrastructure.db.leader import CODE_INDEX_ADMISSION_LOCK
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    EdgeTarget,
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
    IndexCounts,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexLeaseLost,
    IndexProfile,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.domain.codes import (
    FAILED_ERROR_CODES,
    REPOSITORY_STORAGE_EXCEEDED,
    WORKSPACE_STORAGE_EXCEEDED,
)
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import MAX_INDEX_KEY_BYTES, SymbolKey
from app.modules.code_index.domain.storage import COLUMN_WEIGHTS, ROW_OVERHEAD_BYTES, row_bytes
from app.modules.code_index.infrastructure.models import (
    CodeIndexEdge,
    CodeIndexFile,
    CodeIndexImport,
    CodeIndexState,
    CodeIndexSymbol,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.ast_resolution import ImportResolution

type _GraphModel = (
    type[CodeIndexFile] | type[CodeIndexSymbol] | type[CodeIndexEdge] | type[CodeIndexImport]
)
type _Bound = int | InstrumentedAttribute[int]
type _Expression[T] = ColumnElement[T] | InstrumentedAttribute[T]

# Deletion order of one portion; the tables have no foreign keys between them.
_GRAPH_MODELS: tuple[_GraphModel, ...] = (
    CodeIndexEdge,
    CodeIndexImport,
    CodeIndexSymbol,
    CodeIndexFile,
)
# Displaced rows stay for in-flight readers this long after a publication (ADR §6.2).
DISPLACED_GRACE = timedelta(minutes=10)
# Character limits of the graph tables' varchar columns, read from the models.
_TEXT_LIMITS = {
    (model.__tablename__, column.name): column.type.length
    for model in _GRAPH_MODELS
    for column in model.__table__.columns
    if isinstance(column.type, String)
    and not isinstance(column.type, Enum)
    and column.type.length is not None
}


def _row_bytes(model: _GraphModel) -> ColumnElement[int]:
    """The SQL half of `row_bytes`: the same estimate over the stored row."""
    columns = model.__table__.columns
    estimate: ColumnElement[int] = literal(ROW_OVERHEAD_BYTES)
    for column, weight in COLUMN_WEIGHTS[model.__tablename__].items():
        estimate = estimate + func.coalesce(func.octet_length(columns[column]), 0) * weight
    return estimate


def _failure(error_code: str) -> dict[str, Any]:
    """ADR §7: a `failed` index uses no publication, so GC removes every row it has."""
    return {
        "status": IndexStatus.FAILED,
        "error_code": error_code,
        # A `failed` index is not rebuilt (ADR §7), so it waits for no wake-up.
        "wake_requested_at": None,
        "lease_token": None,
        "lease_expires_at": None,
        "pending_bytes": 0,
        "published_publication": 0,
        "floor_publication": 0,
        "published_at": None,
        "indexed_commit_sha": None,
        "extractor_version": None,
        "resolver_version": None,
        "parser_profile_version": None,
        "limits_profile_version": None,
        "indexed_default_branch": None,
        "exclusions_digest": None,
        "file_count": 0,
        "symbol_count": 0,
        "edge_count": 0,
        "stored_bytes": 0,
    }


def _stats(stats: IndexBuildStats) -> dict[str, int]:
    """The job metrics a failure, a release or a publication miss records."""
    return {
        "downloaded_bytes": stats.downloaded_bytes,
        "build_duration_ms": stats.build_duration_ms,
        "github_request_count": stats.github_request_count,
    }


def _visible(
    model: _GraphModel, generation: _Bound, floor: _Bound, published: _Bound
) -> ColumnElement[bool]:
    """T6: a row of the generation inside [floor, published], not closed by then."""
    return and_(
        model.generation == generation,
        model.valid_from >= floor,
        model.valid_from <= published,
        or_(model.valid_to.is_(None), model.valid_to > published),
    )


def _readable(model: _GraphModel, repository_id: UUID) -> list[ColumnElement[bool]]:
    """T6 against the joined state row, so one statement reads both from one snapshot."""
    return [
        model.repository_id == repository_id,
        CodeIndexState.repository_id == model.repository_id,
        CodeIndexState.status == IndexStatus.READY,
        _visible(
            model,
            CodeIndexState.generation,
            CodeIndexState.floor_publication,
            CodeIndexState.published_publication,
        ),
    ]


def _older_generation(model: _GraphModel, generation: _Expression[int]) -> ColumnElement[bool]:
    """A row of a generation a purge has left behind."""
    return model.generation < generation


def _abandoned(
    model: _GraphModel,
    generation: _Expression[int],
    published: _Expression[int],
    leased: ColumnElement[int],
) -> ColumnElement[bool]:
    """An unpublished row of this generation, except the lease's own publication."""
    return and_(
        model.generation == generation,
        model.valid_from > published,
        or_(leased.is_(None), model.valid_from != leased),
    )


def _below_floor(
    model: _GraphModel,
    generation: _Expression[int],
    floor: _Expression[int],
    displaced_due: ColumnElement[bool],
) -> ColumnElement[bool]:
    """A row a full build of this generation displaced, past the grace."""
    return and_(displaced_due, model.generation == generation, model.valid_from < floor)


def _closed(
    model: _GraphModel,
    generation: _Expression[int],
    published: _Expression[int],
    displaced_due: ColumnElement[bool],
) -> ColumnElement[bool]:
    """A row an incremental build closed, past the grace.

    `valid_to IS NOT NULL` adds nothing to the comparison; it repeats the predicate of the
    partial index `ix_code_index_files_closed`, so a probe of this branch alone reads only
    the closed rows.
    """
    return and_(
        displaced_due,
        model.generation == generation,
        model.valid_to.is_not(None),
        model.valid_to <= published,
    )


def garbage_branches(
    model: _GraphModel,
    *,
    generation: _Expression[int],
    published: _Expression[int],
    floor: _Expression[int],
    leased: ColumnElement[int],
    displaced_due: ColumnElement[bool],
) -> tuple[ColumnElement[bool], ...]:
    """T7: the rows of `model` no reader sees any more, one condition per kind.

    An older generation; an unpublished row of this generation, except the lease's own
    publication (a NULL `leased` spares nothing); and, once `displaced_due`, a row below
    the floor or closed by the publication. Over SQL expressions of the state:
    `collect_garbage` binds the values it read under FOR SHARE; the leader enumeration
    correlates the state columns and probes each branch alone (#143).
    """
    return (
        _older_generation(model, generation),
        _abandoned(model, generation, published, leased),
        _below_floor(model, generation, floor, displaced_due),
        _closed(model, generation, published, displaced_due),
    )


def garbage_condition(
    model: _GraphModel,
    *,
    generation: _Expression[int],
    published: _Expression[int],
    floor: _Expression[int],
    leased: ColumnElement[int],
    displaced_due: ColumnElement[bool],
) -> ColumnElement[bool]:
    """T7: any of `garbage_branches`."""
    return or_(
        *garbage_branches(
            model,
            generation=generation,
            published=published,
            floor=floor,
            leased=leased,
            displaced_due=displaced_due,
        )
    )


def _text_values(file: IndexedFile) -> Iterator[tuple[str, str, str]]:
    """Every text value `file` writes as (table, column, value); its paths equal `file.path`."""
    yield "code_index_files", "path", file.path
    yield "code_index_files", "blob_sha", file.blob_sha
    for symbol in file.symbols:
        yield "code_index_symbols", "qualified_name", symbol.key.qualified_name
    for edge in file.edges:
        yield "code_index_edges", "target_path", edge.target_path
        yield "code_index_edges", "target_name", edge.target_name
    for record in file.imports:
        yield "code_index_imports", "specifier", record.specifier
        if record.target_path is not None:
            yield "code_index_imports", "target_path", record.target_path


def _index_keys(file: IndexedFile) -> Iterator[tuple[str, tuple[str, ...]]]:
    """The text columns of every btree key each row of `file` adds."""
    yield "code_index_files", (file.path,)
    for symbol in file.symbols:
        yield "code_index_symbols", (symbol.key.path, symbol.key.qualified_name)
    for edge in file.edges:
        yield "code_index_edges", (edge.path, edge.target_path, edge.target_name)
    for record in file.imports:
        yield "code_index_imports", (record.path, record.specifier)
        if record.target_path is not None:
            yield "code_index_imports", (record.target_path,)


def _validate(file: IndexedFile) -> None:
    """Reject the whole file before any write; filtering such files is the indexer's job."""
    paths = {symbol.key.path for symbol in file.symbols}
    paths |= {edge.path for edge in file.edges}
    paths |= {record.path for record in file.imports}
    if paths - {file.path}:
        raise ValueError("every symbol, edge and import of a file must carry the file's path")
    for table, column, value in _text_values(file):
        limit = _TEXT_LIMITS[table, column]
        if len(value) > limit:
            raise ValueError(
                f"{table}.{column} is {len(value)} characters, over its limit of {limit}"
            )
    if any((edge.kind == EdgeKind.IMPORTS) != (edge.target_name == "") for edge in file.edges):
        raise ValueError("only an imports edge has an empty target_name")
    declared = Counter(symbol.key for symbol in file.symbols)
    for symbol_key, count in declared.items():
        if count > 1:
            raise ValueError(
                f"duplicate symbol key {symbol_key.qualified_name!r} ({symbol_key.kind}, "
                f"disambiguator {symbol_key.disambiguator}) in one file"
            )
    for table, key in _index_keys(file):
        size = sum(len(value.encode()) for value in key)
        if size > MAX_INDEX_KEY_BYTES:
            raise ValueError(
                f"a {table} key is {size} UTF-8 bytes, over MAX_INDEX_KEY_BYTES "
                f"({MAX_INDEX_KEY_BYTES})"
            )


def _to_state(row: CodeIndexState) -> IndexState:
    profile = None
    if row.extractor_version is not None:
        profile = IndexProfile(
            extractor_version=row.extractor_version,
            resolver_version=cast(str, row.resolver_version),
            parser_profile_version=cast(str, row.parser_profile_version),
            limits_profile_version=cast(str, row.limits_profile_version),
            default_branch=cast(str, row.indexed_default_branch),
            exclusions_digest=cast(str, row.exclusions_digest),
        )
    return IndexState(
        generation=row.generation,
        status=row.status,
        purge_reason=row.purge_reason,
        error_code=row.error_code,
        last_error_code=row.last_error_code,
        indexed_commit_sha=row.indexed_commit_sha,
        target_commit_sha=row.target_commit_sha,
        reserved_publication=row.reserved_publication,
        published_publication=row.published_publication,
        floor_publication=row.floor_publication,
        published_at=row.published_at,
        last_used_at=row.last_used_at,
        lease_expires_at=row.lease_expires_at,
        file_count=row.file_count,
        symbol_count=row.symbol_count,
        edge_count=row.edge_count,
        stored_bytes=row.stored_bytes,
        pending_bytes=row.pending_bytes,
        profile=profile,
        stats=IndexBuildStats(
            downloaded_bytes=row.downloaded_bytes,
            build_duration_ms=row.build_duration_ms,
            github_request_count=row.github_request_count,
        ),
        updated_at=row.updated_at,
    )


def _to_symbol(row: CodeIndexSymbol) -> IndexedSymbol:
    return IndexedSymbol(
        SymbolKey(row.path, row.qualified_name, row.kind, row.disambiguator),
        row.start_line,
        row.end_line,
    )


def _to_edge(row: CodeIndexEdge) -> IndexedEdge:
    return IndexedEdge(row.path, row.kind, row.target_path, row.target_name)


def _to_import(row: CodeIndexImport) -> ImportRecord:
    return ImportRecord(row.path, row.specifier, row.resolution, row.target_path)


def _holds(state: CodeIndexState | None, fence: IndexFence, now: datetime) -> bool:
    """The fence still owns the live lease of the current generation."""
    return (
        state is not None
        and state.lease_token == fence.token
        and state.generation == fence.generation
        and state.reserved_publication == fence.publication
        and state.lease_expires_at is not None
        and state.lease_expires_at > now
    )


class SqlAlchemyCodeIndexStore:
    """Index rows of one repository per call; flushes, never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _state(
        self,
        repository_id: UUID,
        *,
        lock: bool = False,
        share: bool = False,
        key_share: bool = False,
    ) -> CodeIndexState | None:
        statement = select(CodeIndexState).where(CodeIndexState.repository_id == repository_id)
        if lock:
            statement = statement.with_for_update(read=share, key_share=key_share)
        return cast(
            CodeIndexState | None,
            await self._session.scalar(statement.execution_options(populate_existing=True)),
        )

    async def _admission_lock(self) -> None:
        # Claims, batch writes and publications take it first, before any state row lock,
        # so they run one at a time without a lock-order cycle: a claim counts live
        # leases, and a batch reads the Workspace bytes, with every earlier batch and
        # publication committed (ADR §6.2).
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": CODE_INDEX_ADMISSION_LOCK}
        )

    async def _clock(self) -> datetime:
        # Read after the lock: an expiry compared with now() would use the start of the
        # transaction (ADR §6.2).
        return cast(datetime, await self._session.scalar(select(func.clock_timestamp())))

    async def _fenced(
        self, repository_id: UUID, fence: IndexFence, *, share: bool, key_share: bool = False
    ) -> CodeIndexState:
        state = await self._state(repository_id, lock=True, share=share, key_share=key_share)
        if state is None or not _holds(state, fence, await self._clock()):
            raise IndexLeaseLost("the index lease of this attempt is lost")
        return state

    async def _update_state(self, repository_id: UUID, **values: Any) -> None:
        await self._session.execute(
            sa_update(CodeIndexState)
            .where(CodeIndexState.repository_id == repository_id)
            .values(**values)
            .execution_options(synchronize_session=False)
        )

    async def _delete(
        self,
        repository_id: UUID,
        condition: Callable[[_GraphModel], ColumnElement[bool]],
        limit: int,
    ) -> int:
        """Delete up to `limit` rows matching `condition(model)` across the graph tables."""
        deleted = 0
        for model in _GRAPH_MODELS:
            if deleted >= limit:
                break
            key = tuple(model.__table__.primary_key)
            victims = (
                select(*key)
                .where(model.repository_id == repository_id, condition(model))
                .limit(limit - deleted)
            )
            result = cast(
                CursorResult[Any],
                await self._session.execute(
                    delete(model)
                    .where(model.repository_id == repository_id, sa_tuple(*key).in_(victims))
                    .execution_options(synchronize_session=False)
                ),
            )
            deleted += result.rowcount
        return deleted

    async def get_state(self, repository_id: UUID) -> IndexState | None:
        state = await self._state(repository_id)
        return None if state is None else _to_state(state)

    async def enable(self, repository_id: UUID) -> IndexState:
        created = await self._session.scalar(
            pg_insert(CodeIndexState)
            .values(
                repository_id=repository_id,
                generation=1,
                last_used_at=func.now(),
                wake_requested_at=func.clock_timestamp(),
            )
            .on_conflict_do_nothing(index_elements=[CodeIndexState.repository_id])
            .returning(CodeIndexState.repository_id)
        )
        state = await self._state(repository_id, lock=True)
        if state is None:
            raise RuntimeError("code index state row is missing after enable")
        if created is None and state.purge_reason is not None:
            # The purge already cleared publication and lease; a repeated enable of a
            # live index is a no-op, so it never wipes a `ready` index.
            await self._update_state(
                repository_id,
                generation=state.generation + 1,
                purge_reason=None,
                status=IndexStatus.EMPTY,
                last_used_at=func.now(),
                wake_requested_at=func.clock_timestamp(),
            )
            state = await self._state(repository_id)
        return _to_state(cast(CodeIndexState, state))

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        state = await self._state(repository_id, lock=True)
        if state is None:
            return
        if state.purge_reason is not None:
            # Only a TTL reason yields, keeping the generation. After a TTL purge the leader
            # leaves the `empty` row alone and the next review request (or an enable) brings
            # the index back (ADR §4); `purge_reason` tells TTL from loss of access (ADR §6.1,
            # leader row), so a later removal or toggle-off replaces `ttl`.
            if state.purge_reason == PurgeReason.TTL and reason != PurgeReason.TTL:
                await self._update_state(repository_id, purge_reason=reason)
            return
        # reserved_publication stays monotonic and last_used_at stays for the TTL.
        await self._update_state(
            repository_id,
            generation=state.generation + 1,
            status=IndexStatus.EMPTY,
            purge_reason=reason,
            published_publication=0,
            floor_publication=0,
            lease_token=None,
            lease_expires_at=None,
            wake_requested_at=None,
            indexed_commit_sha=None,
            target_commit_sha=None,
            published_at=None,
            error_code=None,
            last_error_code=None,
            extractor_version=None,
            resolver_version=None,
            parser_profile_version=None,
            limits_profile_version=None,
            indexed_default_branch=None,
            exclusions_digest=None,
            file_count=0,
            symbol_count=0,
            edge_count=0,
            stored_bytes=0,
            pending_bytes=0,
            downloaded_bytes=0,
            build_duration_ms=0,
            github_request_count=0,
        )

    async def purge_unused(self, repository_id: UUID, unused_for: timedelta) -> bool:
        state = await self._state(repository_id, lock=True)
        if (
            state is None
            or state.purge_reason is not None
            or state.status == IndexStatus.FAILED
            or state.last_used_at is None
        ):
            return False
        # Read after the lock: a use committed while this purge waited keeps the index.
        if state.last_used_at >= await self._clock() - unused_for:
            return False
        await self.purge(repository_id, PurgeReason.TTL)
        return True

    async def request_build(self, repository_id: UUID, *, target_commit_sha: str | None) -> bool:
        state = await self._state(repository_id, lock=True)
        if state is None or state.purge_reason is not None or state.status == IndexStatus.FAILED:
            return False
        values: dict[str, Any] = {"wake_requested_at": func.clock_timestamp()}
        if target_commit_sha is not None:
            values["target_commit_sha"] = target_commit_sha
        await self._update_state(repository_id, **values)
        return True

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
        state = await self._state(repository_id, lock=True)
        if (
            state is None
            or state.generation != generation
            or state.purge_reason is not None
            or state.status == IndexStatus.FAILED
            # A trigger after the head read: its target is at least as new.
            or (state.wake_requested_at is not None and state.wake_requested_at > read_at)
        ):
            return False
        # A NULL indexed commit or branch never equals the live head. The versions are
        # those of the locked row, not of the listing the head read followed.
        profile = _to_state(state).profile
        drifted = (
            state.indexed_commit_sha != commit_sha
            or state.indexed_default_branch != branch
            or profile is None
            or not versions_current(profile)
        )
        values: dict[str, Any] = {"target_commit_sha": commit_sha}
        if drifted:
            values["wake_requested_at"] = func.clock_timestamp()
        await self._update_state(repository_id, **values)
        return drifted

    async def clear_wake_request(self, repository_id: UUID, requested_at: datetime) -> bool:
        result = cast(
            CursorResult[Any],
            await self._session.execute(
                sa_update(CodeIndexState)
                .where(
                    CodeIndexState.repository_id == repository_id,
                    CodeIndexState.wake_requested_at == requested_at,
                )
                .values(wake_requested_at=None)
                .execution_options(synchronize_session=False)
            ),
        )
        return result.rowcount == 1

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int, admission: IndexAdmission
    ) -> LeaseClaim | ClaimRefused:
        await self._admission_lock()
        state = await self._state(repository_id, lock=True)
        if (
            state is None
            or state.purge_reason is not None
            or state.generation != generation
            or state.status == IndexStatus.FAILED
        ):
            return ClaimRefused(ClaimRefusal.STALE)
        now = await self._clock()
        if state.lease_expires_at is not None and state.lease_expires_at > now:
            return ClaimRefused(ClaimRefusal.LEASED)
        # The claim drops the pending bytes of an expired attempt, so only its own
        # stored bytes count for this repository.
        workspace_bytes = state.stored_bytes + await self._workspace_bytes(repository_id, now)
        quota = None
        if state.stored_bytes > admission.repository_storage_bytes:
            quota = REPOSITORY_STORAGE_EXCEEDED
        elif workspace_bytes > admission.workspace_storage_bytes:
            quota = WORKSPACE_STORAGE_EXCEEDED
        if quota is not None:
            # This job made no GitHub request: it records zero metrics, not the last job's.
            await self._update_state(
                repository_id,
                **_failure(quota),
                **_stats(
                    IndexBuildStats(downloaded_bytes=0, build_duration_ms=0, github_request_count=0)
                ),
            )
            return ClaimRefused(ClaimRefusal.QUOTA, quota)
        leases, installation_leases = await self._live_leases(repository_id, now)
        if (
            leases >= admission.max_leases
            or installation_leases >= admission.max_installation_leases
        ):
            return ClaimRefused(ClaimRefusal.BUSY)
        # Taking over an expired lease revokes it: its batches compare the token.
        fence = IndexFence(uuid4(), generation, state.reserved_publication + 1)
        await self._update_state(
            repository_id,
            reserved_publication=fence.publication,
            lease_token=fence.token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            pending_bytes=0,
            status=IndexStatus.BUILDING if state.status == IndexStatus.EMPTY else state.status,
        )
        return LeaseClaim(fence, state.target_commit_sha)

    async def _workspace_bytes(self, repository_id: UUID, now: datetime) -> int:
        """Stored bytes of the other repositories of this one's Workspace, and their
        pending bytes under a lease still live at `now`.

        An expired attempt keeps its pending bytes until its repository is claimed again
        or its lease is revoked; they would fail a healthy repository for its Workspace.
        """
        workspace = (
            select(ProviderInstallation.workspace_id)
            .join(Repository, Repository.provider_installation_id == ProviderInstallation.id)
            .where(Repository.id == repository_id)
            .scalar_subquery()
        )
        live_pending = case(
            (
                and_(
                    CodeIndexState.lease_token.is_not(None),
                    CodeIndexState.lease_expires_at > now,
                ),
                CodeIndexState.pending_bytes,
            ),
            else_=0,
        )
        total = await self._session.scalar(
            select(func.coalesce(func.sum(CodeIndexState.stored_bytes + live_pending), 0))
            .join(Repository, Repository.id == CodeIndexState.repository_id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(
                ProviderInstallation.workspace_id == workspace,
                CodeIndexState.repository_id != repository_id,
            )
        )
        return int(total or 0)

    async def _live_leases(self, repository_id: UUID, now: datetime) -> tuple[int, int]:
        """Live leases in all, and in this repository's installation, at `now`."""
        installation = (
            select(Repository.provider_installation_id)
            .where(Repository.id == repository_id)
            .scalar_subquery()
        )
        row = (
            await self._session.execute(
                select(
                    func.count(),
                    func.count().filter(Repository.provider_installation_id == installation),
                )
                .select_from(CodeIndexState)
                .join(Repository, Repository.id == CodeIndexState.repository_id)
                .where(
                    CodeIndexState.lease_token.is_not(None), CodeIndexState.lease_expires_at > now
                )
            )
        ).one()
        return int(row[0]), int(row[1])

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, limit: int) -> int:
        state = await self._fenced(repository_id, fence, share=True)
        published = state.published_publication
        return await self._delete(
            repository_id,
            lambda model: and_(
                model.generation == fence.generation,
                model.valid_from > published,
                model.valid_from < fence.publication,
            ),
            limit,
        )

    async def replace_file(
        self, repository_id: UUID, fence: IndexFence, file: IndexedFile
    ) -> IndexStorage:
        _validate(file)
        # The Workspace bytes this batch returns must include every other batch's pending
        # bytes and every publication's stored bytes, so batches serialize with each
        # other, with claims and with publications on this lock.
        await self._admission_lock()
        # FOR NO KEY UPDATE, not ADR §6.2's FOR SHARE: the batch updates the state row's
        # pending bytes, so it takes the lock it ends up holding. A purge or a
        # publication (FOR UPDATE) still waits for the batch.
        await self._fenced(repository_id, fence, share=False, key_share=True)
        removed = 0
        for model in _GRAPH_MODELS:
            # The rows of an earlier write of this path in this attempt leave the pending
            # bytes, so a rewrite is counted once.
            deleted = await self._session.scalars(
                delete(model)
                .where(
                    model.repository_id == repository_id,
                    model.generation == fence.generation,
                    model.valid_from == fence.publication,
                    model.path == file.path,
                )
                .returning(_row_bytes(model))
                .execution_options(synchronize_session=False)
            )
            removed += sum(deleted)
        row: dict[str, Any] = {
            "repository_id": repository_id,
            "generation": fence.generation,
            "valid_from": fence.publication,
            "path": file.path,
        }
        # Duplicate edges and imports of one file collapse onto their natural key.
        edges = dict.fromkeys(
            (edge.kind, edge.target_path, edge.target_name) for edge in file.edges
        )
        imports: dict[tuple[str, ImportResolution], str | None] = {}
        for record in file.imports:
            imports.setdefault((record.specifier, record.resolution), record.target_path)
        rows: tuple[tuple[_GraphModel, list[dict[str, Any]]], ...] = (
            (CodeIndexFile, [{**row, "blob_sha": file.blob_sha}]),
            (
                CodeIndexSymbol,
                [
                    {
                        **row,
                        "qualified_name": symbol.key.qualified_name,
                        "kind": symbol.key.kind,
                        "disambiguator": symbol.key.disambiguator,
                        "start_line": symbol.start_line,
                        "end_line": symbol.end_line,
                    }
                    for symbol in file.symbols
                ],
            ),
            (
                CodeIndexEdge,
                [
                    {**row, "kind": kind, "target_path": target_path, "target_name": name}
                    for kind, target_path, name in edges
                ],
            ),
            (
                CodeIndexImport,
                [
                    {
                        **row,
                        "specifier": specifier,
                        "resolution": resolution,
                        "target_path": target_path,
                    }
                    for (specifier, resolution), target_path in imports.items()
                ],
            ),
        )
        added = 0
        for model, values in rows:
            if values:
                # executemany, batched by insertmanyvalues: one multi-row VALUES statement
                # would pass PostgreSQL's 65,535 bind parameters at ~7,000 rows.
                await self._session.execute(insert(model), values)
                added += sum(row_bytes(model.__tablename__, value) for value in values)
        storage = (
            await self._session.execute(
                sa_update(CodeIndexState)
                .where(CodeIndexState.repository_id == repository_id)
                .values(pending_bytes=CodeIndexState.pending_bytes + (added - removed))
                .returning(CodeIndexState.stored_bytes, CodeIndexState.pending_bytes)
                .execution_options(synchronize_session=False)
            )
        ).one()
        others = await self._workspace_bytes(repository_id, await self._clock())
        return IndexStorage(
            stored_bytes=storage[0],
            pending_bytes=storage[1],
            workspace_bytes=storage[0] + storage[1] + others,
        )

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
        """`deleted_paths` closes paths of an incremental build; a full one ignores them."""
        # The lease may expire while this transaction is open; a batch of another build
        # waits for it, so it counts these bytes as stored once they are committed.
        await self._admission_lock()
        state = await self._fenced(repository_id, fence, share=False)
        mine, published = fence.publication, state.published_publication
        floor = state.floor_publication
        # Incremental: rows of an abandoned attempt between `published` and `mine` would
        # turn visible, so the caller runs discard_abandoned first.
        if (
            state.indexed_commit_sha != base_commit_sha
            or published >= mine
            or (not full and await self._has_abandoned(repository_id, fence, published))
        ):
            # A miss spends the lease like a publication, so the next wake-up claims at once,
            # and records the job's metrics as a release does.
            await self._update_state(
                repository_id,
                lease_token=None,
                lease_expires_at=None,
                pending_bytes=0,
                **_stats(stats),
            )
            return False
        if not full:
            written = select(CodeIndexFile.path).where(
                CodeIndexFile.repository_id == repository_id,
                CodeIndexFile.generation == fence.generation,
                CodeIndexFile.valid_from == mine,
            )
            for model in _GRAPH_MODELS:
                await self._session.execute(
                    sa_update(model)
                    .where(
                        model.repository_id == repository_id,
                        _visible(model, fence.generation, floor, published),
                        or_(model.path.in_(written), model.path.in_(list(deleted_paths))),
                    )
                    .values(valid_to=mine)
                    .execution_options(synchronize_session=False)
                )
        else:
            floor = mine
        # One aggregate per table: its visible rows and their estimated bytes.
        totals: dict[_GraphModel, tuple[int, int]] = {}
        for model in _GRAPH_MODELS:
            count, size = (
                await self._session.execute(
                    select(func.count(), func.coalesce(func.sum(_row_bytes(model)), 0))
                    .select_from(model)
                    .where(
                        model.repository_id == repository_id,
                        _visible(model, fence.generation, floor, mine),
                    )
                )
            ).one()
            totals[model] = int(count), int(size)

        # The attempt is spent: its fence can no longer write rows that would be visible.
        await self._update_state(
            repository_id,
            published_publication=mine,
            floor_publication=floor,
            status=IndexStatus.READY,
            error_code=None,
            last_error_code=None,
            indexed_commit_sha=commit_sha,
            published_at=func.now(),
            lease_token=None,
            lease_expires_at=None,
            file_count=totals[CodeIndexFile][0],
            symbol_count=totals[CodeIndexSymbol][0],
            edge_count=totals[CodeIndexEdge][0],
            stored_bytes=sum(size for _, size in totals.values()),
            pending_bytes=0,
            extractor_version=profile.extractor_version,
            resolver_version=profile.resolver_version,
            parser_profile_version=profile.parser_profile_version,
            limits_profile_version=profile.limits_profile_version,
            indexed_default_branch=profile.default_branch,
            exclusions_digest=profile.exclusions_digest,
            downloaded_bytes=stats.downloaded_bytes,
            build_duration_ms=stats.build_duration_ms,
            github_request_count=stats.github_request_count,
        )
        return True

    async def mark_failed(
        self,
        repository_id: UUID,
        fence: IndexFence,
        error_code: str,
        *,
        stats: IndexBuildStats | None = None,
    ) -> None:
        if error_code not in FAILED_ERROR_CODES:
            raise ValueError(f"{error_code!r} is not an error code of a failed index")
        await self._fenced(repository_id, fence, share=False)
        values = _failure(error_code)
        if stats is not None:
            values |= _stats(stats)
        await self._update_state(repository_id, **values)

    async def release(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        last_error_code: str | None = None,
        stats: IndexBuildStats | None = None,
    ) -> IndexStatus:
        state = await self._fenced(repository_id, fence, share=False)
        # The last publication stays readable; GC removes this attempt's rows. Its
        # counters and stored bytes stay; only the job metrics are this attempt's.
        status = IndexStatus.READY if state.published_publication > 0 else IndexStatus.EMPTY
        values: dict[str, Any] = {
            "status": status,
            "lease_token": None,
            "lease_expires_at": None,
            "pending_bytes": 0,
        }
        if last_error_code is not None:
            values["last_error_code"] = last_error_code
        if stats is not None:
            values |= _stats(stats)
        await self._update_state(repository_id, **values)
        return status

    async def _has_abandoned(self, repository_id: UUID, fence: IndexFence, published: int) -> bool:
        abandoned = select(
            or_(
                *(
                    select(model.path)
                    .where(
                        model.repository_id == repository_id,
                        model.generation == fence.generation,
                        model.valid_from > published,
                        model.valid_from < fence.publication,
                    )
                    .exists()
                    for model in _GRAPH_MODELS
                )
            )
        )
        return bool(await self._session.scalar(abandoned))

    async def revoke_expired_lease(self, repository_id: UUID) -> bool:
        state = await self._state(repository_id, lock=True)
        if state is None or state.lease_expires_at is None:
            return False
        if state.lease_expires_at > await self._clock():
            return False
        await self._update_state(
            repository_id, lease_token=None, lease_expires_at=None, pending_bytes=0
        )
        return True

    async def collect_garbage(self, repository_id: UUID, limit: int) -> int:
        state = await self._state(repository_id, lock=True, share=True)
        if state is None:
            return 0
        generation = literal(state.generation, BigInteger())
        published = literal(state.published_publication, BigInteger())
        floor = literal(state.floor_publication, BigInteger())
        # The lease's own number is spared, even expired, until revoke_expired_lease.
        leased = literal(
            state.reserved_publication if state.lease_token is not None else None, BigInteger()
        )
        displaced_due = literal(
            state.published_at is not None
            and state.published_at <= await self._clock() - DISPLACED_GRACE,
            Boolean(),
        )
        return await self._delete(
            repository_id,
            lambda model: garbage_condition(
                model,
                generation=generation,
                published=published,
                floor=floor,
                leased=leased,
                displaced_due=displaced_due,
            ),
            limit,
        )

    async def _visible_symbols(
        self, repository_id: UUID, *conditions: ColumnElement[bool]
    ) -> list[CodeIndexSymbol]:
        rows = await self._session.scalars(
            select(CodeIndexSymbol)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexSymbol.repository_id)
            .where(*_readable(CodeIndexSymbol, repository_id), *conditions)
            .order_by(
                CodeIndexSymbol.path,
                CodeIndexSymbol.start_line,
                CodeIndexSymbol.end_line,
                CodeIndexSymbol.qualified_name,
                CodeIndexSymbol.kind,
                CodeIndexSymbol.disambiguator,
            )
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def _visible_edges(
        self, repository_id: UUID, *conditions: ColumnElement[bool]
    ) -> list[CodeIndexEdge]:
        rows = await self._session.scalars(
            select(CodeIndexEdge)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexEdge.repository_id)
            .where(*_readable(CodeIndexEdge, repository_id), *conditions)
            .order_by(
                CodeIndexEdge.path,
                CodeIndexEdge.kind,
                CodeIndexEdge.target_path,
                CodeIndexEdge.target_name,
            )
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def _visible_imports(
        self, repository_id: UUID, *conditions: ColumnElement[bool]
    ) -> list[CodeIndexImport]:
        rows = await self._session.scalars(
            select(CodeIndexImport)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexImport.repository_id)
            .where(*_readable(CodeIndexImport, repository_id), *conditions)
            .order_by(CodeIndexImport.path, CodeIndexImport.specifier, CodeIndexImport.resolution)
            .execution_options(populate_existing=True)
        )
        return list(rows)

    async def _visible_blobs(self, repository_id: UUID, paths: set[str]) -> dict[str, str]:
        rows = await self._session.execute(
            select(CodeIndexFile.path, CodeIndexFile.blob_sha)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexFile.repository_id)
            .where(*_readable(CodeIndexFile, repository_id), CodeIndexFile.path.in_(paths))
        )
        return {path: blob_sha for path, blob_sha in rows}

    async def get_file(self, repository_id: UUID, path: str) -> IndexedFile | None:
        blob_sha = (await self._visible_blobs(repository_id, {path})).get(path)
        if blob_sha is None:
            return None
        return IndexedFile(
            path,
            blob_sha,
            symbols=tuple(await self.file_symbols(repository_id, path)),
            edges=tuple(
                _to_edge(row)
                for row in await self._visible_edges(repository_id, CodeIndexEdge.path == path)
            ),
            imports=tuple(
                _to_import(row)
                for row in await self._visible_imports(repository_id, CodeIndexImport.path == path)
            ),
        )

    async def file_symbols(self, repository_id: UUID, path: str) -> list[IndexedSymbol]:
        rows = await self._visible_symbols(repository_id, CodeIndexSymbol.path == path)
        return [_to_symbol(row) for row in rows]

    async def outgoing_edges(self, repository_id: UUID, path: str) -> list[EdgeTarget]:
        edges = [
            _to_edge(row)
            for row in await self._visible_edges(repository_id, CodeIndexEdge.path == path)
        ]
        if not edges:
            return []
        targets = {edge.target_path for edge in edges}
        blobs = await self._visible_blobs(repository_id, targets)
        names = {edge.target_name for edge in edges if edge.kind is EdgeKind.USES_IMPORTED}
        symbols: dict[tuple[str, str], list[IndexedSymbol]] = {}
        if names:
            for row in await self._visible_symbols(
                repository_id,
                CodeIndexSymbol.path.in_(targets),
                CodeIndexSymbol.qualified_name.in_(names),
            ):
                symbols.setdefault((row.path, row.qualified_name), []).append(_to_symbol(row))
        return [
            EdgeTarget(
                edge,
                blobs.get(edge.target_path),
                ()
                if edge.kind is EdgeKind.IMPORTS
                else tuple(symbols.get((edge.target_path, edge.target_name), ())),
            )
            for edge in edges
        ]

    async def incoming_edges(
        self, repository_id: UUID, target_path: str, target_name: str
    ) -> list[IndexedEdge]:
        rows = await self._visible_edges(
            repository_id,
            CodeIndexEdge.target_path == target_path,
            CodeIndexEdge.target_name == target_name,
        )
        return [_to_edge(row) for row in rows]

    async def importers_of(self, repository_id: UUID, target_path: str) -> list[ImportRecord]:
        rows = await self._visible_imports(
            repository_id, CodeIndexImport.target_path == target_path
        )
        return [_to_import(row) for row in rows]

    async def iter_files(
        self, repository_id: UUID, after: str | None, limit: int
    ) -> list[tuple[str, str]]:
        rows = await self._session.execute(
            select(CodeIndexFile.path, CodeIndexFile.blob_sha)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexFile.repository_id)
            .where(
                *_readable(CodeIndexFile, repository_id),
                true() if after is None else CodeIndexFile.path > after,
            )
            .order_by(CodeIndexFile.path)
            .limit(limit)
        )
        return [(path, blob_sha) for path, blob_sha in rows]

    async def iter_imports(
        self, repository_id: UUID, after: ImportRecord | None, limit: int
    ) -> list[ImportRecord]:
        # The primary key's tail; each bound value takes its column's type.
        key = sa_tuple(CodeIndexImport.path, CodeIndexImport.specifier, CodeIndexImport.resolution)
        rows = await self._session.scalars(
            select(CodeIndexImport)
            .join(CodeIndexState, CodeIndexState.repository_id == CodeIndexImport.repository_id)
            .where(
                *_readable(CodeIndexImport, repository_id),
                true() if after is None else key > (after.path, after.specifier, after.resolution),
            )
            .order_by(*key.clauses)
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return [_to_import(row) for row in rows]

    async def visible_counts(self, repository_id: UUID, paths: Sequence[str]) -> IndexCounts:
        def count(model: _GraphModel) -> ColumnElement[int]:
            return (
                select(func.count())
                .select_from(model)
                .join(CodeIndexState, CodeIndexState.repository_id == model.repository_id)
                .where(*_readable(model, repository_id), model.path.in_(list(paths)))
                .scalar_subquery()
            )

        row = (
            await self._session.execute(
                select(count(CodeIndexFile), count(CodeIndexSymbol), count(CodeIndexEdge))
            )
        ).one()
        return IndexCounts(file_count=row[0], symbol_count=row[1], edge_count=row[2])


class SqlAlchemyCodeIndexUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def index(self) -> SqlAlchemyCodeIndexStore:
        return SqlAlchemyCodeIndexStore(self.session)
