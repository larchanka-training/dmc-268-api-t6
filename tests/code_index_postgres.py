"""Shared PostgreSQL fixtures and helpers of the code index tests (#141, #142, #143).

The `code_index_database` and `index` fixtures migrate a schema of their own to head;
a test file loads them with `pytest_plugins = ("tests.code_index_postgres",)`. The store
harness writes, claims and publishes through `SqlAlchemyCodeIndexStore`; the purge
helpers publish an index, count its stored rows and run the GC leader task as the
worker leader runs it.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    CodeIndexStore,
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
    IndexedEdge,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexProfile,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.application.leader_tasks import CollectIndexGarbage
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
from app.modules.code_index.domain.keys import (
    SymbolKey,
)
from app.modules.code_index.infrastructure.index_store import (
    SqlAlchemyCodeIndexStore,
    SqlAlchemyCodeIndexUnitOfWork,
)
from app.modules.code_index.infrastructure.maintenance import (
    SqlAlchemyCodeIndexMaintenanceUnitOfWork,
)
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import SymbolKind


@pytest.fixture
def code_index_database() -> Iterator[Connection]:
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
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _migration_config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _repository(connection: Connection, installation_external_id: int = 17) -> str:
    workspace_id, installation_id, repository_id = str(uuid4()), str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external_id, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id, "external_id": installation_external_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, 101, 'octo/repo', 'main', "
            "'https://github.com/octo/repo')"
        ),
        {"id": repository_id, "installation": installation_id},
    )
    return repository_id


def _sibling(
    connection: Connection,
    repository_id: UUID,
    *,
    external_id: int,
    installation_external_id: int | None = None,
) -> UUID:
    """Another repository in `repository_id`'s installation, or, given an installation
    external id, in a new installation of its Workspace; committed for async sessions."""
    installation = connection.scalar(
        text("SELECT provider_installation_id FROM repositories WHERE id = :id"),
        {"id": repository_id},
    )
    if installation_external_id is not None:
        installation = connection.scalar(
            text(
                "INSERT INTO provider_installations "
                "(id, workspace_id, provider, external_id, metadata) "
                "SELECT gen_random_uuid(), workspace_id, 'github', :external_id, "
                '\'{"account_login": "octo"}\' FROM provider_installations WHERE id = :id '
                "RETURNING id"
            ),
            {"id": installation, "external_id": installation_external_id},
        )
    sibling = uuid4()
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, :external_id, 'octo/sibling', 'main', "
            "'https://github.com/octo/sibling')"
        ),
        {"id": sibling, "installation": installation, "external_id": external_id},
    )
    connection.commit()
    return sibling


C1, C2, C3 = "1" * 40, "2" * 40, "3" * 40

_DIGEST = "d" * 64

_PROFILE = IndexProfile("facts-3", "resolver-2", "parser-1", "limits-1", "main", _DIGEST)

_STATS = IndexBuildStats(downloaded_bytes=48_213, build_duration_ms=1_250, github_request_count=3)

_PROFILE_FIELDS = {
    "extractor_version": "facts-3",
    "resolver_version": "resolver-2",
    "parser_profile_version": "parser-1",
    "limits_profile_version": "limits-1",
    "default_branch": "main",
    "exclusions_digest": _DIGEST,
}

_NO_STATS = {
    "downloaded_bytes": 0,
    "build_duration_ms": 0,
    "github_request_count": 0,
    "http_status": None,
}

# ADR 0001 §7: 2 jobs in all, 1 per installation, 512 MiB per repository, 2 GiB per Workspace.
_ADMISSION = IndexAdmission(
    max_leases=2,
    max_installation_leases=1,
    repository_storage_bytes=512 * 1024 * 1024,
    workspace_storage_bytes=2 * 1024 * 1024 * 1024,
)

_STALE = ClaimRefused(ClaimRefusal.STALE)

_LEASED = ClaimRefused(ClaimRefusal.LEASED)

# Estimated bytes (160 per row + w(c) · UTF-8 bytes) of the rows of `_module` and
# `_importer`:
# - `_module`: file a.py/aa01 160+2·4+4 = 172, symbol foo 160+2·4+2·3 = 174;
# - `_importer`: file 172, symbol Bar 174, imports edge 160+2·4+3·4 = 180, uses edge
#   180+3·3 = 189, import `.a` 160+2·4+2·2+2·4 = 180, import `requests` 160+2·4+2·8 = 184.
_MODULE_BYTES = 346

_IMPORTER_BYTES = 1_079

_STATS_FIELDS = {
    "downloaded_bytes": 48_213,
    "build_duration_ms": 1_250,
    "github_request_count": 3,
    "http_status": None,
}

_COUNT_ROWS = (
    "SELECT (SELECT count(*) FROM code_index_files WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_symbols WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_edges WHERE repository_id = :repository), "
    "(SELECT count(*) FROM code_index_imports WHERE repository_id = :repository)"
)

_FILE_ROWS = (
    "SELECT generation, valid_from, valid_to, path, blob_sha FROM code_index_files "
    "WHERE repository_id = :repository ORDER BY generation, valid_from, path"
)

# 2, 2 and 4 UTF-8 bytes per character.
_E, _A, _WIDE = "é", "ä", "\U0001d51e"


@dataclass(frozen=True)
class _Index:
    factory: async_sessionmaker[AsyncSession]
    a: UUID
    b: UUID

    def uow(self) -> SqlAlchemyCodeIndexUnitOfWork:
        return SqlAlchemyCodeIndexUnitOfWork(self.factory)


@pytest.fixture
def index(code_index_database: Connection) -> Iterator[_Index]:
    """Two workspace/installation/repository sets, committed so async sessions see them."""
    connection = code_index_database
    repository_a = UUID(_repository(connection, installation_external_id=17))
    repository_b = UUID(_repository(connection, installation_external_id=18))
    connection.commit()
    schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        yield _Index(async_sessionmaker(engine, expire_on_commit=False), repository_a, repository_b)
    finally:
        asyncio.run(engine.dispose())


def _symbol(
    path: str, name: str, start: int, end: int, kind: SymbolKind = SymbolKind.FUNCTION
) -> IndexedSymbol:
    return IndexedSymbol(SymbolKey(path, name, kind, 0), start, end)


def _uses(path: str, target_path: str, name: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.USES_IMPORTED, target_path, name)


def _imports(path: str, target_path: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.IMPORTS, target_path, "")


def _module(blob: str, start: int) -> IndexedFile:
    """`a.py` defining `foo` at line `start`."""
    return IndexedFile("a.py", blob, symbols=(_symbol("a.py", "foo", start, start + 1),))


def _importer(blob: str, start: int) -> IndexedFile:
    """`b.py` declaring class `Bar`; it imports `a.py`, uses its `foo` and imports `requests`."""
    return IndexedFile(
        "b.py",
        blob,
        symbols=(_symbol("b.py", "Bar", start, start + 4, SymbolKind.CLASS),),
        edges=(_imports("b.py", "a.py"), _uses("b.py", "a.py", "foo")),
        imports=(
            ImportRecord("b.py", ".a", ImportResolution.RESOLVED, "a.py"),
            ImportRecord("b.py", "requests", ImportResolution.EXTERNAL, None),
        ),
    )


async def _enable(index: _Index, repository_id: UUID) -> IndexState:
    async with index.uow() as uow:
        state = await uow.index.enable(repository_id)
        await uow.commit()
    return state


async def _purge(
    index: _Index, repository_id: UUID, reason: PurgeReason = PurgeReason.DISABLED
) -> None:
    async with index.uow() as uow:
        await uow.index.purge(repository_id, reason)
        await uow.commit()


async def _try_claim(
    index: _Index, repository_id: UUID, generation: int, admission: IndexAdmission = _ADMISSION
) -> LeaseClaim | ClaimRefused:
    async with index.uow() as uow:
        claim = await uow.index.claim_lease(repository_id, generation, 600, admission)
        # Committed on a refusal too: a QUOTA refusal writes `failed`.
        await uow.commit()
    return claim


async def _claim(
    index: _Index, repository_id: UUID, generation: int = 1, admission: IndexAdmission = _ADMISSION
) -> IndexFence:
    claim = await _try_claim(index, repository_id, generation, admission)
    assert isinstance(claim, LeaseClaim)
    return claim.fence


async def _release(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    last_error_code: str | None = None,
    stats: IndexBuildStats | None = None,
) -> IndexStatus:
    async with index.uow() as uow:
        status = await uow.index.release(
            repository_id, fence, last_error_code=last_error_code, stats=stats
        )
        await uow.commit()
    return status


async def _write(
    index: _Index, repository_id: UUID, fence: IndexFence, *files: IndexedFile
) -> list[IndexStorage]:
    """One committed batch per file; the storage each batch returned."""
    storages: list[IndexStorage] = []
    for file in files:
        async with index.uow() as uow:
            storages.append(await uow.index.replace_file(repository_id, fence, file))
            await uow.commit()
    return storages


async def _publish(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    commit: str,
    *,
    base: str | None = None,
    full: bool = True,
    deleted: Sequence[str] = (),
    stats: IndexBuildStats = _STATS,
) -> bool:
    async with index.uow() as uow:
        published = await uow.index.publish(
            repository_id,
            fence,
            base_commit_sha=base,
            commit_sha=commit,
            full=full,
            deleted_paths=deleted,
            profile=_PROFILE,
            stats=stats,
        )
        await uow.commit()
    return published


async def _build(
    index: _Index,
    repository_id: UUID,
    commit: str,
    *files: IndexedFile,
    base: str | None = None,
    full: bool = True,
    deleted: Sequence[str] = (),
) -> None:
    fence = await _claim(index, repository_id)
    await _write(index, repository_id, fence, *files)
    assert await _publish(
        index, repository_id, fence, commit, base=base, full=full, deleted=deleted
    )


async def _read[T](index: _Index, read: Callable[[CodeIndexStore], Awaitable[T]]) -> T:
    async with index.uow() as uow:
        return await read(uow.index)


async def _state(index: _Index, repository_id: UUID) -> IndexState | None:
    return await _read(index, lambda store: store.get_state(repository_id))


async def _reads(index: _Index, repository_id: UUID) -> tuple[object, ...]:
    """Every read method of the store, on the paths and names both repositories share."""
    async with index.uow() as uow:
        store = uow.index
        return (
            await store.get_file(repository_id, "a.py"),
            await store.file_symbols(repository_id, "b.py"),
            await store.outgoing_edges(repository_id, "b.py"),
            await store.incoming_edges(repository_id, "a.py", "foo"),
            await store.importers_of(repository_id, "a.py"),
        )


async def _collect(index: _Index, repository_id: UUID, limit: int) -> list[int]:
    """Run garbage collection in portions until one deletes nothing."""
    collected: list[int] = []
    while not collected or collected[-1]:
        async with index.uow() as uow:
            collected.append(await uow.index.collect_garbage(repository_id, limit))
            await uow.commit()
    return collected


async def _rows(index: _Index, statement: str, **values: object) -> list[tuple[Any, ...]]:
    async with index.factory() as session:
        return [tuple(row) for row in await session.execute(text(statement), values)]


async def _execute(index: _Index, statement: str, repository_id: UUID) -> None:
    async with index.factory.begin() as session:
        await session.execute(text(statement), {"repository": repository_id})


async def _expire_lease(index: _Index, repository_id: UUID) -> None:
    await _execute(
        index,
        "UPDATE code_index_states SET lease_expires_at = clock_timestamp() - interval '1 second' "
        "WHERE repository_id = :repository",
        repository_id,
    )


async def _wait_until_blocked(
    index: _Index, blocker_pid: int, waiters: int, *tasks: asyncio.Task[Any]
) -> None:
    """Wait until `waiters` sessions wait on `blocker_pid`, directly or behind another waiter.

    A second waiter on one row queues behind the first for the tuple lock, so the chain
    is followed rather than only the direct blocker.
    """
    async with asyncio.timeout(5), index.factory() as observer:
        while (
            await observer.scalar(
                text(
                    "WITH RECURSIVE blocked(pid) AS ("
                    "SELECT pid FROM pg_stat_activity WHERE :pid = ANY(pg_blocking_pids(pid)) "
                    "UNION SELECT a.pid FROM pg_stat_activity a "
                    "JOIN blocked b ON b.pid = ANY(pg_blocking_pids(a.pid))) "
                    "SELECT count(*) FROM blocked"
                ),
                {"pid": blocker_pid},
            )
            < waiters
        ):
            assert not any(task.done() for task in tasks), "a session did not wait for the lock"
            await observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            await asyncio.sleep(0.01)
    assert not any(task.done() for task in tasks)


def _pause_after_lock(
    monkeypatch: pytest.MonkeyPatch, store: SqlAlchemyCodeIndexStore
) -> tuple[asyncio.Event, asyncio.Event]:
    """Hold `store` at its clock read, which every fenced step makes after its row lock."""
    locked, release = asyncio.Event(), asyncio.Event()
    clock = store._clock

    async def paused() -> datetime:
        locked.set()
        await release.wait()
        return await clock()

    monkeypatch.setattr(store, "_clock", paused)
    return locked, release


def _fields(state: IndexState | None) -> dict[str, object]:
    """The state without its clock values, which tests compare to the database clock."""
    assert state is not None
    fields = dataclasses.asdict(state)
    for name in ("published_at", "last_used_at", "lease_expires_at", "updated_at"):
        del fields[name]
    return fields


_TTL = timedelta(days=30)


async def _purge_unused(index: _Index, repository_id: UUID) -> bool:
    async with index.uow() as uow:
        purged = await uow.index.purge_unused(repository_id, _TTL)
        await uow.commit()
    return purged


async def _unused_for_31_days(index: _Index, repository_id: UUID) -> None:
    await _execute(
        index,
        "UPDATE code_index_states SET last_used_at = clock_timestamp() - interval '31 days' "
        "WHERE repository_id = :repository",
        repository_id,
    )


async def _mark_failed(
    index: _Index,
    repository_id: UUID,
    fence: IndexFence,
    error_code: str,
    stats: IndexBuildStats | None = None,
) -> None:
    async with index.uow() as uow:
        await uow.index.mark_failed(repository_id, fence, error_code, stats=stats)
        await uow.commit()


async def _marker(index: _Index, repository_id: UUID) -> datetime | None:
    [(marker,)] = await _rows(
        index,
        "SELECT wake_requested_at FROM code_index_states WHERE repository_id = :repository",
        repository=repository_id,
    )
    return cast(datetime | None, marker)


# A published index: two files, each with one symbol, one edge and one import record.
GRAPH_ROWS = (2, 2, 2, 2)


async def publish_index(index: _Index, *repository_ids: UUID) -> None:
    """Enable each index and publish rows in every graph table."""
    for repository_id in repository_ids:
        await _enable(index, repository_id)
        await _build(index, repository_id, C1, _module("aa01", 1), _importer("ab01", 1))


async def graph_rows(index: _Index, repository_id: UUID) -> tuple[int, ...]:
    """The stored files, symbols, edges and imports of `repository_id`, visible or not."""
    [rows] = await _rows(index, _COUNT_ROWS, repository=repository_id)
    return rows


async def collect_garbage(index: _Index) -> None:
    """One run of the GC leader task, as the worker leader runs it."""
    await CollectIndexGarbage(
        uow=partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory), batch_rows=100
    ).run(10.0)
