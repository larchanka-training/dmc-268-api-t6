"""PostgreSQL parity of the frozen retriever of the #146 eval with the SQL one (Task 3).

``SqlAlchemyGraphRetriever`` and ``FrozenGraphRetriever`` get the same edges, among them
importer paths whose order depends on the collation of the database (``__init__`` before a
lowercase letter, an uppercase path, ``-`` and ``_``); their hints must be equal. The frozen
one orders by code point, so this is where a database with another collation shows.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.modules.code_index.application.index_store import IndexedEdge, IndexedFile
from app.modules.code_index.infrastructure.graph_retriever import SqlAlchemyGraphRetriever
from app.modules.reviews.application.graph_retriever import GraphTarget
from review.scripts.rag_snapshot import (
    Extractor,
    FrozenGraphRetriever,
    IndexSnapshot,
    SnapshotFile,
    parse_snapshot,
    render_snapshot,
)
from tests.code_index_postgres import (
    C1,
    _build,
    _enable,
    _imports,
    _Index,
    _module,
    _repository,
    _uses,
)
from tests.template_database import MigratedDatabase

pytest_plugins = ("tests.migrated_postgres",)

_FOO = GraphTarget("a.py", "foo")
_MODULE = GraphTarget("a.py", "")
_MISS = GraphTarget("a.py", "nope")
# Importers of ``a.py`` whose relative order depends on the collation.
_IMPORTERS = (
    "pkg/__init__.py",
    "pkg/api.py",
    "pkg/Api2.py",
    "Zeta.py",
    "alpha.py",
    "b-c.py",
    "b_c.py",
    "b.py",
    "B.py",
)


@pytest.fixture
def index(migrated_db: MigratedDatabase) -> Iterator[_Index]:
    """One repository with RAG on."""
    database_url = migrated_db.url
    schema = migrated_db.schema
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                repository_a = UUID(_repository(connection, installation_external_id=17))
                repository_b = UUID(_repository(connection, installation_external_id=18))
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
    finally:
        engine.dispose()


def _importer(path: str) -> IndexedFile:
    return IndexedFile(path, "bb01", edges=(_imports(path, "a.py"), _uses(path, "a.py", "foo")))


def _frozen(files: tuple[IndexedFile, ...]) -> FrozenGraphRetriever:
    """The snapshot of the same rows as ``index.json`` holds them, through its own bytes."""
    edges = sorted(
        (edge for file in files for edge in file.edges),
        key=lambda edge: (edge.path, edge.kind.value, edge.target_path, edge.target_name),
    )
    paths = sorted(file.path for file in files)
    snapshot = IndexSnapshot(
        schema_version=1,
        indexed_commit_sha=C1,
        extractor=Extractor("limits", "parser", "exclusions"),
        file_count=len(paths),
        symbol_count=0,
        files=tuple(SnapshotFile(path, "bb01") for path in paths),
        edges=tuple(IndexedEdge(e.path, e.kind, e.target_path, e.target_name) for e in edges),
    )
    return FrozenGraphRetriever(parse_snapshot(render_snapshot(snapshot)))


@pytest.mark.integration
def test_the_frozen_retriever_returns_the_hints_of_the_sql_retriever(index: _Index) -> None:
    importers = tuple(_importer(path) for path in _IMPORTERS)
    frozen = _frozen((IndexedFile("a.py", "aa01"), *importers))

    async def scenario() -> None:
        await _enable(index, index.a)
        await _build(index, index.a, C1, _module("aa01", 1), *importers)
        sql = SqlAlchemyGraphRetriever(index.factory)
        for targets in (
            (_FOO,),
            (_MODULE,),
            (_FOO, _MODULE),
            (_MODULE, _FOO, _MODULE),
            (_MISS, _FOO),
            (),
        ):
            expected = await sql.read_hints(index.a, targets)
            actual = await frozen.read_hints(index.a, targets)
            assert actual.hints == expected.hints, targets
            assert actual.indexed_commit_sha == expected.indexed_commit_sha
            assert actual.index.status == expected.index.status
        # Not vacuous: every importer is a hint.
        assert len((await sql.read_hints(index.a, (_FOO,))).hints) == len(_IMPORTERS)

    asyncio.run(scenario())
