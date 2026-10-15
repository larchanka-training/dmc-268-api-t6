"""PostgreSQL adapter of the graph index store, fenced per ADR 0001 §6.2 (#141).

Every statement filters by `repository_id`. The store runs on the caller's session and
never commits. Lease expiry is compared with `clock_timestamp()` read after the state
row is locked, never with the transaction start time.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import (
    ColumnElement,
    CursorResult,
    Enum,
    ScalarSelect,
    String,
    and_,
    delete,
    func,
    insert,
    or_,
    select,
    true,
)
from sqlalchemy import tuple_ as sa_tuple
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.application.index_store import (
    EdgeTarget,
    ImportRecord,
    IndexBuildStats,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexLeaseLost,
    IndexProfile,
    IndexState,
)
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import MAX_INDEX_KEY_BYTES, SymbolKey
from app.modules.code_index.infrastructure.models import (
    CodeIndexEdge,
    CodeIndexFile,
    CodeIndexImport,
    CodeIndexState,
    CodeIndexSymbol,
)
from app.modules.reviews.application.ast_resolution import ImportResolution

type _GraphModel = (
    type[CodeIndexFile] | type[CodeIndexSymbol] | type[CodeIndexEdge] | type[CodeIndexImport]
)
type _Bound = int | InstrumentedAttribute[int]

# Deletion order of one portion; the tables have no foreign keys between them.
_GRAPH_MODELS: tuple[_GraphModel, ...] = (
    CodeIndexEdge,
    CodeIndexImport,
    CodeIndexSymbol,
    CodeIndexFile,
)
# Displaced rows stay for in-flight readers this long after a publication (ADR §6.2).
_DISPLACED_GRACE = timedelta(minutes=10)
# Character limits of the graph tables' varchar columns, read from the models.
_TEXT_LIMITS = {
    (model.__tablename__, column.name): column.type.length
    for model in _GRAPH_MODELS
    for column in model.__table__.columns
    if isinstance(column.type, String)
    and not isinstance(column.type, Enum)
    and column.type.length is not None
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
        self, repository_id: UUID, *, lock: bool = False, share: bool = False
    ) -> CodeIndexState | None:
        statement = select(CodeIndexState).where(CodeIndexState.repository_id == repository_id)
        if lock:
            statement = statement.with_for_update(read=share)
        return cast(
            CodeIndexState | None,
            await self._session.scalar(statement.execution_options(populate_existing=True)),
        )

    async def _clock(self) -> datetime:
        # Read after the lock: an expiry compared with now() would use the start of the
        # transaction (ADR §6.2).
        return cast(datetime, await self._session.scalar(select(func.clock_timestamp())))

    async def _fenced(
        self, repository_id: UUID, fence: IndexFence, *, share: bool
    ) -> CodeIndexState:
        state = await self._state(repository_id, lock=True, share=share)
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
            .values(repository_id=repository_id, generation=1, last_used_at=func.now())
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
            indexed_commit_sha=None,
            target_commit_sha=None,
            published_at=None,
            error_code=None,
            last_error_code=None,
            extractor_version=None,
            resolver_version=None,
            parser_profile_version=None,
            limits_profile_version=None,
            file_count=0,
            symbol_count=0,
            edge_count=0,
            downloaded_bytes=0,
            build_duration_ms=0,
            github_request_count=0,
        )

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int
    ) -> IndexFence | None:
        state = await self._state(repository_id, lock=True)
        if state is None or state.purge_reason is not None or state.generation != generation:
            return None
        now = await self._clock()
        if state.lease_expires_at is not None and state.lease_expires_at > now:
            return None
        # Taking over an expired lease revokes it: its batches compare the token.
        fence = IndexFence(uuid4(), generation, state.reserved_publication + 1)
        status = state.status
        if status in (IndexStatus.EMPTY, IndexStatus.FAILED):
            status = IndexStatus.BUILDING
        await self._update_state(
            repository_id,
            reserved_publication=fence.publication,
            lease_token=fence.token,
            lease_expires_at=now + timedelta(seconds=lease_seconds),
            status=status,
            error_code=None,
        )
        return fence

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

    async def replace_file(self, repository_id: UUID, fence: IndexFence, file: IndexedFile) -> None:
        _validate(file)
        await self._fenced(repository_id, fence, share=True)
        for model in _GRAPH_MODELS:
            await self._session.execute(
                delete(model)
                .where(
                    model.repository_id == repository_id,
                    model.generation == fence.generation,
                    model.valid_from == fence.publication,
                    model.path == file.path,
                )
                .execution_options(synchronize_session=False)
            )
        row = {
            "repository_id": repository_id,
            "generation": fence.generation,
            "valid_from": fence.publication,
            "path": file.path,
        }
        await self._session.execute(insert(CodeIndexFile).values(**row, blob_sha=file.blob_sha))
        # executemany, batched by insertmanyvalues: one multi-row VALUES statement would
        # pass PostgreSQL's 65,535 bind parameters at ~7,000 rows.
        if file.symbols:
            await self._session.execute(
                insert(CodeIndexSymbol),
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
            )
        # Duplicate edges and imports of one file collapse onto their natural key.
        edges = dict.fromkeys(
            (edge.kind, edge.target_path, edge.target_name) for edge in file.edges
        )
        if edges:
            await self._session.execute(
                insert(CodeIndexEdge),
                [
                    {**row, "kind": kind, "target_path": target_path, "target_name": name}
                    for kind, target_path, name in edges
                ],
            )
        imports: dict[tuple[str, ImportResolution], str | None] = {}
        for record in file.imports:
            imports.setdefault((record.specifier, record.resolution), record.target_path)
        if imports:
            await self._session.execute(
                insert(CodeIndexImport),
                [
                    {
                        **row,
                        "specifier": specifier,
                        "resolution": resolution,
                        "target_path": target_path,
                    }
                    for (specifier, resolution), target_path in imports.items()
                ],
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
            # A miss spends the lease like a publication, so the next wake-up claims at once.
            await self._update_state(repository_id, lease_token=None, lease_expires_at=None)
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

        def count(model: _GraphModel) -> ScalarSelect[int]:
            return (
                select(func.count())
                .select_from(model)
                .where(
                    model.repository_id == repository_id,
                    _visible(model, fence.generation, floor, mine),
                )
                .scalar_subquery()
            )

        # The attempt is spent: its fence can no longer write rows that would be visible.
        await self._update_state(
            repository_id,
            published_publication=mine,
            floor_publication=floor,
            status=IndexStatus.READY,
            error_code=None,
            indexed_commit_sha=commit_sha,
            published_at=func.now(),
            lease_token=None,
            lease_expires_at=None,
            file_count=count(CodeIndexFile),
            symbol_count=count(CodeIndexSymbol),
            edge_count=count(CodeIndexEdge),
            extractor_version=profile.extractor_version,
            resolver_version=profile.resolver_version,
            parser_profile_version=profile.parser_profile_version,
            limits_profile_version=profile.limits_profile_version,
            downloaded_bytes=stats.downloaded_bytes,
            build_duration_ms=stats.build_duration_ms,
            github_request_count=stats.github_request_count,
        )
        return True

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
        await self._update_state(repository_id, lease_token=None, lease_expires_at=None)
        return True

    async def collect_garbage(self, repository_id: UUID, limit: int) -> int:
        state = await self._state(repository_id, lock=True, share=True)
        if state is None:
            return 0
        generation, published = state.generation, state.published_publication
        floor = state.floor_publication
        # The lease's own number is spared, even expired, until revoke_expired_lease.
        leased = state.reserved_publication if state.lease_token is not None else None
        displaced_due = (
            state.published_at is not None
            and state.published_at <= await self._clock() - _DISPLACED_GRACE
        )

        def garbage(model: _GraphModel) -> ColumnElement[bool]:
            unpublished = and_(
                model.generation == generation,
                model.valid_from > published,
                true() if leased is None else model.valid_from != leased,
            )
            conditions = [model.generation < generation, unpublished]
            if displaced_due:
                conditions.append(
                    and_(
                        model.generation == generation,
                        or_(model.valid_from < floor, model.valid_to <= published),
                    )
                )
            return or_(*conditions)

        return await self._delete(repository_id, garbage, limit)

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


class SqlAlchemyCodeIndexUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def index(self) -> SqlAlchemyCodeIndexStore:
        return SqlAlchemyCodeIndexStore(self.session)
