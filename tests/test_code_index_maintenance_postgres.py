"""PostgreSQL evidence for the index leader's enumeration (#143).

`with_garbage` lists the repositories garbage collection has work in (an expired lease,
rows of an old generation or of a dead attempt, displaced rows past their grace) and
spares a live lease; once it lists a repository no more, every row left is visible.
`unused` lists the live indexes past the TTL. Both page by `repository_id`. One run of
the GC leader task revokes a crashed attempt's lease and deletes its rows.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import IndexedFile
from app.modules.code_index.application.leader_tasks import CollectIndexGarbage
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.infrastructure.maintenance import (
    SqlAlchemyCodeIndexMaintenanceUnitOfWork,
)
from tests.test_code_index_store_postgres import (
    _COUNT_ROWS,
    _TTL,
    C1,
    C2,
    _build,
    _claim,
    _collect,
    _enable,
    _execute,
    _expire_lease,
    _importer,
    _Index,
    _mark_failed,
    _migration_config,
    _module,
    _purge,
    _read,
    _repository,
    _rows,
    _state,
    _unused_for_31_days,
    _write,
)


@dataclass(frozen=True)
class _Fleet:
    index: _Index
    # Five repositories, each in a Workspace and installation of its own, sorted as
    # PostgreSQL orders uuid values.
    ids: tuple[UUID, ...]


@pytest.fixture
def fleet() -> Iterator[_Fleet]:
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
                ids = tuple(
                    sorted(
                        UUID(_repository(connection, installation_external_id=17 + number))
                        for number in range(5)
                    )
                )
                connection.commit()
                async_engine = create_async_engine(
                    database_url,
                    connect_args={"options": f"-csearch_path={schema}"},
                    poolclass=NullPool,
                )
                try:
                    factory = async_sessionmaker(async_engine, expire_on_commit=False)
                    yield _Fleet(_Index(factory, ids[0], ids[1]), ids)
                finally:
                    asyncio.run(async_engine.dispose())
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


async def _with_garbage(index: _Index, after: UUID | None = None, limit: int = 10) -> list[UUID]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.with_garbage(after, limit)


async def _unused(index: _Index, after: UUID | None = None, limit: int = 10) -> list[UUID]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.unused(_TTL, after, limit)


async def _revoke(index: _Index, repository_id: UUID) -> bool:
    async with index.uow() as uow:
        revoked = await uow.index.revoke_expired_lease(repository_id)
        await uow.commit()
    return revoked


async def _raw_and_visible(
    index: _Index, repository_id: UUID
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """The stored files, symbols, edges and imports of `repository_id`, and the visible ones."""
    [raw] = await _rows(index, _COUNT_ROWS, repository=repository_id)
    state = await _state(index, repository_id)
    assert state is not None
    imports = await _read(index, lambda store: store.iter_imports(repository_id, None, 100))
    return raw, (state.file_count, state.symbol_count, state.edge_count, len(imports))


@pytest.mark.integration
def test_with_garbage_selects_an_expired_lease_and_spares_a_live_one(fleet: _Fleet) -> None:
    async def scenario() -> None:
        index, (first, second) = fleet.index, fleet.ids[:2]
        for repository_id in (first, second):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module("aa01", 1))
        assert await _with_garbage(index) == []
        assert await _raw_and_visible(index, first) == ((1, 1, 0, 0), (1, 1, 0, 0))

        live = await _claim(index, first)
        await _write(index, first, live, IndexedFile("c.py", "ac02"))
        # The rows of a live lease are not garbage.
        assert await _with_garbage(index) == []
        await _expire_lease(index, first)
        assert await _with_garbage(index) == [first]
        await _claim(index, second)
        await _expire_lease(index, second)
        assert await _with_garbage(index) == [first, second]
        assert await _with_garbage(index, limit=1) == [first]
        assert await _with_garbage(index, after=first) == [second]
        assert await _with_garbage(index, after=second) == []

        assert await _revoke(index, second) is True
        assert await _with_garbage(index) == [first]
        assert await _revoke(index, first) is True
        # The revoked attempt left c.py behind.
        assert await _with_garbage(index) == [first]
        assert await _collect(index, first, 100) == [1, 0]
        assert await _with_garbage(index) == []
        for repository_id in (first, second):
            assert await _raw_and_visible(index, repository_id) == ((1, 1, 0, 0), (1, 1, 0, 0))

    asyncio.run(scenario())


@pytest.mark.integration
def test_with_garbage_selects_displaced_rows_after_ten_minutes_and_a_purged_index(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index, (first, second) = fleet.index, fleet.ids[:2]
        for repository_id in (first, second):
            await _enable(index, repository_id)
            await _build(index, repository_id, C1, _module("aa01", 1), _importer("ab01", 1))
        # The incremental build closes a.py of publication 1: displaced, still readable.
        await _build(index, first, C2, _module("aa02", 3), base=C1, full=False)
        assert await _with_garbage(index) == []
        await _execute(
            index,
            "UPDATE code_index_states SET published_at = published_at - interval '11 minutes' "
            "WHERE repository_id = :repository",
            first,
        )
        assert await _with_garbage(index) == [first]
        # The file a.py/aa01 and its symbol.
        assert await _collect(index, first, 100) == [2, 0]
        assert await _with_garbage(index) == []
        assert await _raw_and_visible(index, first) == ((2, 2, 2, 2), (2, 2, 2, 2))

        await _purge(index, second, PurgeReason.DISABLED)
        assert await _with_garbage(index) == [second]
        assert await _collect(index, second, 100) == [8, 0]
        assert await _with_garbage(index) == []
        assert await _raw_and_visible(index, second) == ((0, 0, 0, 0), (0, 0, 0, 0))

    asyncio.run(scenario())


@pytest.mark.integration
def test_with_garbage_selects_a_failed_index_until_its_rows_are_gone(fleet: _Fleet) -> None:
    async def scenario() -> None:
        index, first = fleet.index, fleet.ids[0]
        await _enable(index, first)
        await _build(index, first, C1, _module("aa01", 1))
        fence = await _claim(index, first)
        await _mark_failed(index, first, fence, "archive_unsafe")
        assert await _with_garbage(index) == [first]
        assert await _collect(index, first, 100) == [2, 0]
        assert await _with_garbage(index) == []
        assert await _raw_and_visible(index, first) == ((0, 0, 0, 0), (0, 0, 0, 0))

    asyncio.run(scenario())


@pytest.mark.integration
def test_unused_lists_live_indexes_past_the_ttl_by_repository_id(fleet: _Fleet) -> None:
    async def scenario() -> None:
        index = fleet.index
        old_ready, purged, old_empty, failed, fresh = fleet.ids
        for repository_id in fleet.ids:
            await _enable(index, repository_id)
        await _build(index, old_ready, C1, _module("aa01", 1))
        await _purge(index, purged, PurgeReason.DISABLED)
        fence = await _claim(index, failed)
        await _mark_failed(index, failed, fence, "limit_exceeded:edges")
        for repository_id in (old_ready, purged, old_empty, failed):
            await _unused_for_31_days(index, repository_id)
        assert await _unused(index) == [old_ready, old_empty]
        assert await _unused(index, limit=1) == [old_ready]
        assert await _unused(index, after=old_ready) == [old_empty]
        assert await _unused(index, after=old_empty) == []

        await _purge(index, old_ready, PurgeReason.TTL)
        assert await _unused(index) == [old_empty]

    asyncio.run(scenario())


@pytest.mark.integration
def test_one_gc_run_revokes_an_expired_lease_and_deletes_the_rows_of_its_attempt(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index, first = fleet.index, fleet.ids[0]
        await _enable(index, first)
        fence = await _claim(index, first)
        await _write(index, first, fence, _module("aa01", 1), _importer("ab01", 1))
        # The job crashed: its lease expires with two unpublished files.
        await _expire_lease(index, first)
        assert await _raw_and_visible(index, first) == ((2, 2, 2, 2), (0, 0, 0, 0))

        remains = await CollectIndexGarbage(
            uow=partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory), batch_rows=100
        ).run(10.0)

        assert remains is False
        assert await _raw_and_visible(index, first) == ((0, 0, 0, 0), (0, 0, 0, 0))
        state = await _state(index, first)
        assert state is not None and state.lease_expires_at is None
        assert await _with_garbage(index) == []

    asyncio.run(scenario())
