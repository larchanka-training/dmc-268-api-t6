"""PostgreSQL evidence for the graph hints a review reads (#144, ADR 0001 §5, §6.2).

The retriever reads the state row and the incoming edges of every target in one
REPEATABLE READ read-only transaction of its own, which it closes before it returns: a
publication committed during the read does not change it, and only the run's repository
rows are read. An index without a READY row in that read gives its fresh status and no
hints, also when the gate found it usable a moment before.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator, Sequence
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import (
    ImportRecord,
    IndexedEdge,
    IndexedFile,
)
from app.modules.code_index.domain.enums import EdgeKind, PurgeReason, RagIndexStatus
from app.modules.code_index.infrastructure.graph_retriever import SqlAlchemyGraphRetriever
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.code_index.infrastructure.review_index import SqlAlchemyReviewIndexUnitOfWork
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget
from app.modules.reviews.application.rag_index import OpenReviewIndex
from tests.test_code_index_store_postgres import (
    C1,
    C2,
    _build,
    _claim,
    _enable,
    _importer,
    _imports,
    _Index,
    _mark_failed,
    _migration_config,
    _module,
    _publish,
    _purge,
    _repository,
    _uses,
    _write,
)

_RAG_OFF = "UPDATE repositories SET rag_enabled = false WHERE id = :repository"
# The changed symbol `foo` of `a.py`, and a module-level change of `a.py`.
_FOO = GraphTarget("a.py", "foo")
_MODULE = GraphTarget("a.py", "")


@pytest.fixture
def index() -> Iterator[_Index]:
    """Two repositories with RAG on, each in a Workspace and installation of its own."""
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_index_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                command.upgrade(_migration_config(connection), "head")
                repository_a = UUID(_repository(connection, installation_external_id=17))
                repository_b = UUID(_repository(connection, installation_external_id=18))
                # `_repository` leaves the toggle off.
                connection.execute(text("UPDATE repositories SET rag_enabled = true"))
                connection.commit()
                async_engine = create_async_engine(
                    database_url,
                    connect_args={"options": f"-csearch_path={schema}"},
                    poolclass=NullPool,
                )
                try:
                    factory = async_sessionmaker(async_engine, expire_on_commit=False)
                    yield _Index(factory, repository_a, repository_b)
                finally:
                    asyncio.run(async_engine.dispose())
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _user(blob: str) -> IndexedFile:
    """`c.py`, a second importer of `a.py` that also uses its `foo`."""
    return IndexedFile(
        "c.py",
        blob,
        edges=(_imports("c.py", "a.py"), _uses("c.py", "a.py", "foo")),
        imports=(ImportRecord("c.py", ".a", ImportResolution.RESOLVED, "a.py"),),
    )


def _uses_foo(path: str) -> GraphHint:
    return GraphHint(path, EdgeKind.USES_IMPORTED, "a.py", "foo")


def _imports_a(path: str) -> GraphHint:
    return GraphHint(path, EdgeKind.IMPORTS, "a.py", "")


async def _hints(
    index: _Index, repository_id: UUID, targets: Sequence[GraphTarget] = (_FOO, _MODULE)
) -> GraphHints:
    return await SqlAlchemyGraphRetriever(index.factory).read_hints(repository_id, targets)


def _seen(hints: GraphHints) -> tuple[object, ...]:
    """The status, the indexed commit and the stored code of the read, and its hints."""
    return (
        hints.index.status,
        hints.indexed_commit_sha,
        hints.index.last_error_code,
        hints.hints,
    )


type _EdgeRead = tuple[UUID, str, str]


def _spy_edge_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[_EdgeRead], asyncio.Event, asyncio.Event]:
    """Record every edge read, and hold each one until `release` is set.

    `reading` is set at the first edge read, after the state read took the snapshot.
    """
    reads: list[_EdgeRead] = []
    reading, release = asyncio.Event(), asyncio.Event()
    release.set()
    incoming_edges = SqlAlchemyCodeIndexStore.incoming_edges

    async def spied(
        store: SqlAlchemyCodeIndexStore, repository_id: UUID, target_path: str, target_name: str
    ) -> list[IndexedEdge]:
        reads.append((repository_id, target_path, target_name))
        reading.set()
        await release.wait()
        return await incoming_edges(store, repository_id, target_path, target_name)

    monkeypatch.setattr(SqlAlchemyCodeIndexStore, "incoming_edges", spied)
    return reads, reading, release


@pytest.mark.integration
def test_hints_are_the_runs_repository_edges_at_its_indexed_commit(index: _Index) -> None:
    async def scenario() -> None:
        # The same paths in both repositories; only `b` has the second user `c.py`.
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        await _enable(index, index.b)
        await _build(index, index.b, C2, _module("ba01", 11), _importer("bb01", 11), _user("bc01"))

        a_hints = (RagIndexStatus.READY, C1, None, (_uses_foo("b.py"), _imports_a("b.py")))
        assert _seen(await _hints(index, index.a)) == a_hints
        assert _seen(await _hints(index, index.b)) == (
            RagIndexStatus.READY,
            C2,
            None,
            (_uses_foo("b.py"), _uses_foo("c.py"), _imports_a("b.py"), _imports_a("c.py")),
        )
        # A repeated target is read once; the order of the targets is kept.
        assert (await _hints(index, index.a, (_MODULE, _FOO, _MODULE))).hints == (
            _imports_a("b.py"),
            _uses_foo("b.py"),
        )

        # `b` loses its READY row; `a`, with the same paths, reads on.
        await _purge(index, index.b)
        assert _seen(await _hints(index, index.b)) == (RagIndexStatus.EMPTY, None, None, ())
        assert _seen(await _hints(index, index.a)) == a_hints

        # A stale index is still READY: it answers for the commit it was built for.
        async with index.uow() as uow:
            assert await uow.index.request_build(index.a, target_commit_sha=C2)
            await uow.commit()
        assert _seen(await _hints(index, index.a)) == (RagIndexStatus.STALE, *a_hints[1:])

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_publication_committed_during_the_read_does_not_change_it(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        _, reading, release = _spy_edge_reads(monkeypatch)
        release.clear()
        read = asyncio.create_task(_hints(index, index.a))
        await asyncio.wait_for(reading.wait(), 5)

        # C2 adds `c.py`, a second user of `foo`, and commits while the read is held.
        fence = await _claim(index, index.a)
        await _write(index, index.a, fence, _user("ac01"))
        assert await _publish(index, index.a, fence, C2, base=C1, full=False)
        release.set()

        assert _seen(await asyncio.wait_for(read, 5)) == (
            RagIndexStatus.READY,
            C1,
            None,
            (_uses_foo("b.py"), _imports_a("b.py")),
        )
        assert _seen(await _hints(index, index.a)) == (
            RagIndexStatus.READY,
            C2,
            None,
            (_uses_foo("b.py"), _uses_foo("c.py"), _imports_a("b.py"), _imports_a("c.py")),
        )

    asyncio.run(scenario())


@pytest.mark.integration
def test_the_read_is_one_repeatable_read_read_only_transaction_closed_on_return(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        async with index.factory() as session:
            schema = await session.scalar(text("SELECT current_schema()"))
        seen: list[tuple[object, ...]] = []
        sessions: list[AsyncSession] = []
        incoming_edges = SqlAlchemyCodeIndexStore.incoming_edges

        async def spied(
            store: SqlAlchemyCodeIndexStore,
            repository_id: UUID,
            target_path: str,
            target_name: str,
        ) -> list[IndexedEdge]:
            session = store._session
            sessions.append(session)
            seen.append(
                (
                    await session.scalar(text("SHOW transaction_isolation")),
                    await session.scalar(text("SHOW transaction_read_only")),
                )
            )
            return await incoming_edges(store, repository_id, target_path, target_name)

        monkeypatch.setattr(SqlAlchemyCodeIndexStore, "incoming_edges", spied)
        # A pooled engine with pre-ping, as the worker builds it: the second read checks
        # out a pinged connection.
        engine = create_async_engine(
            os.environ["TEST_DATABASE_URL"],
            connect_args={"options": f"-csearch_path={schema}"},
            pool_pre_ping=True,
        )
        try:
            retriever = SqlAlchemyGraphRetriever(async_sessionmaker(engine, expire_on_commit=False))
            for _ in range(2):
                hints = await retriever.read_hints(index.a, (_FOO,))
                assert hints.hints == (_uses_foo("b.py"),)
        finally:
            await engine.dispose()

        assert seen == [("repeatable read", "on"), ("repeatable read", "on")]
        assert [session.in_transaction() for session in sessions] == [False, False]

    asyncio.run(scenario())


@pytest.mark.integration
def test_an_index_without_a_ready_row_gives_its_fresh_status_and_no_hints(
    index: _Index, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        reads, _, _ = _spy_edge_reads(monkeypatch)
        # No state row.
        assert _seen(await _hints(index, index.a)) == (RagIndexStatus.EMPTY, None, None, ())
        await _enable(index, index.a)
        assert _seen(await _hints(index, index.a)) == (RagIndexStatus.EMPTY, None, None, ())
        fence = await _claim(index, index.a)
        assert _seen(await _hints(index, index.a)) == (RagIndexStatus.BUILDING, None, None, ())
        await _write(index, index.a, fence, _module("aa01", 1), _importer("ab01", 1))
        assert await _publish(index, index.a, fence, C1)
        # A failed rebuild drops the publication; its rows stay until GC.
        await _mark_failed(index, index.a, await _claim(index, index.a), "limit_exceeded:edges")
        assert _seen(await _hints(index, index.a)) == (
            RagIndexStatus.FAILED,
            None,
            "limit_exceeded:edges",
            (),
        )
        # A published index purged by the TTL.
        await _enable(index, index.b)
        await _build(index, index.b, C2, _module("ba01", 11), _importer("bb01", 11))
        await _purge(index, index.b, PurgeReason.TTL)
        assert _seen(await _hints(index, index.b)) == (RagIndexStatus.EMPTY, None, None, ())
        # None of these reads looked for edges.
        assert reads == []

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_toggle_off_after_the_gate_gives_not_ready_with_the_fresh_status(index: _Index) -> None:
    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), _importer("ab01", 1))
        gate = OpenReviewIndex(partial(SqlAlchemyReviewIndexUnitOfWork, index.factory))
        readiness = await gate.execute(index.a)
        assert readiness.index is not None
        assert readiness.index.status is RagIndexStatus.READY

        # The toggle-off commits between the READ COMMITTED gate and the graph read.
        async with index.factory() as toggle:
            await toggle.execute(text(_RAG_OFF), {"repository": index.a})
            await SqlAlchemyCodeIndexStore(toggle).purge(index.a, PurgeReason.DISABLED)
            await toggle.commit()

        hints = await _hints(index, index.a)
        assert _seen(hints) == (RagIndexStatus.EMPTY, None, None, ())
        assert hints.index.not_ready_reason is RagIndexStatus.EMPTY

    asyncio.run(scenario())
