"""PostgreSQL evidence for the index leader's enumeration (#143).

`with_garbage` lists the repositories garbage collection has work in (an expired lease,
rows of an old generation or of a dead attempt, displaced rows past their grace) and
spares a live lease; once it lists a repository no more, every row left is visible.
`unused` lists the live indexes past the TTL. Both page by `repository_id`. One run of
the GC leader task revokes a crashed attempt's lease and deletes its rows.

The wake-up and converge listings: `wake_requests` lists marked rows without a live
lease, oldest marker first; `heads` lists the reconcile candidates of enabled, available
GitHub repositories; `to_rearm` lists the repositories whose index is missing or purged by
a removal or a toggle; `to_disarm` lists the live indexes whose RAG is off. The converge
pass re-checks each candidate under a share lock of its repository row, so a re-arm never
resurrects an index a concurrent toggle turned off, and its purges leave GC no row to keep.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import IndexedFile
from app.modules.code_index.application.leader_tasks import (
    CollectIndexGarbage,
    ConvergeIndexes,
)
from app.modules.code_index.application.maintenance import (
    DisarmCandidate,
    HeadCandidate,
    WakeRequest,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
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
    _marker,
    _migration_config,
    _module,
    _purge,
    _read,
    _repository,
    _rows,
    _state,
    _unused_for_31_days,
    _wait_until_blocked,
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
def test_with_garbage_selects_rows_below_the_floor_after_ten_minutes(fleet: _Fleet) -> None:
    async def scenario() -> None:
        index, first = fleet.index, fleet.ids[0]
        await _enable(index, first)
        await _build(index, first, C1, _module("aa01", 1), _importer("ab01", 1))
        # A full build of the same generation raises the floor above every earlier row;
        # those rows stay unclosed, so only the floor makes them garbage.
        await _build(index, first, C2, _module("aa02", 3), base=C1)
        assert await _with_garbage(index) == []
        await _execute(
            index,
            "UPDATE code_index_states SET published_at = published_at - interval '11 minutes' "
            "WHERE repository_id = :repository",
            first,
        )
        assert await _with_garbage(index) == [first]
        assert await _collect(index, first, 100) == [8, 0]
        assert await _with_garbage(index) == []
        assert await _raw_and_visible(index, first) == ((1, 1, 0, 0), (1, 1, 0, 0))

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


# The wake-up and converge listings (#143).


async def _github_repositories(index: _Index, *external_ids: int) -> list[IndexedRepository]:
    """A Workspace, a GitHub installation and an RAG-enabled repository per installation
    external id, committed; sorted as PostgreSQL orders uuid values."""
    repositories = []
    async with index.factory.begin() as session:
        for external_id in external_ids:
            workspace, installation, repository = uuid4(), uuid4(), uuid4()
            await session.execute(
                text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
                {"id": workspace},
            )
            await session.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata) "
                    "VALUES (:id, :workspace, 'github', :external_id, "
                    '\'{"account_login": "octo"}\')'
                ),
                {"id": installation, "workspace": workspace, "external_id": external_id},
            )
            await session.execute(
                text(
                    "INSERT INTO repositories (id, provider_installation_id, external_id, "
                    "full_name, default_branch, web_url, rag_enabled) "
                    "VALUES (:id, :installation, 101, 'octo/repo', 'main', "
                    "'https://github.com/octo/repo', true)"
                ),
                {"id": repository, "installation": installation},
            )
            repositories.append(IndexedRepository(repository, "github", external_id, "octo/repo"))
    return sorted(repositories, key=lambda repository: repository.repository_id)


_RAG_OFF = "UPDATE repositories SET rag_enabled = false WHERE id = :repository"
_UNAVAILABLE = "UPDATE repositories SET installation_available = false WHERE id = :repository"
_INSTALLATION = (
    "UPDATE provider_installations SET {} "
    "WHERE id = (SELECT provider_installation_id FROM repositories WHERE id = :repository)"
)
_DELETED = _INSTALLATION.format("deleted_at = now()")
_GITLAB = _INSTALLATION.format("provider = 'gitlab'")


async def _wake_requests(index: _Index, limit: int = 10) -> list[WakeRequest]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.wake_requests(limit)


async def _heads(index: _Index, after: UUID | None = None, limit: int = 10) -> list[HeadCandidate]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.heads(after, limit)


async def _to_rearm(index: _Index, limit: int = 10) -> list[UUID]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.to_rearm(limit)


async def _to_disarm(index: _Index, limit: int = 10) -> list[DisarmCandidate]:
    async with SqlAlchemyCodeIndexMaintenanceUnitOfWork(index.factory) as uow:
        return await uow.maintenance.to_disarm(limit)


async def _requested(index: _Index, repository_id: UUID) -> datetime:
    marker = await _marker(index, repository_id)
    assert marker is not None
    return marker


@pytest.mark.integration
def test_wake_requests_skip_a_live_lease_and_list_it_once_the_lease_expired(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index = fleet.index
        # Enabled in this order, so their markers are in this order.
        first, leased, last, unmarked = fleet.ids[3], fleet.ids[0], fleet.ids[4], fleet.ids[1]
        for repository_id in (first, leased, last, unmarked):
            await _enable(index, repository_id)
        async with index.uow() as uow:
            assert await uow.index.clear_wake_request(unmarked, await _requested(index, unmarked))
            await uow.commit()
        await _claim(index, leased)
        requests = {
            repository_id: WakeRequest(repository_id, 1, await _requested(index, repository_id))
            for repository_id in (first, leased, last)
        }

        assert await _wake_requests(index) == [requests[first], requests[last]]
        assert await _wake_requests(index, limit=1) == [requests[first]]
        await _expire_lease(index, leased)
        assert await _wake_requests(index) == [requests[first], requests[leased], requests[last]]

    asyncio.run(scenario())


@pytest.mark.integration
def test_heads_list_live_indexes_of_enabled_available_github_repositories(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index = fleet.index
        repositories = await _github_repositories(index, *range(301, 309))
        empty, rag_off, unavailable, deleted, gitlab, purged, failed, ready = repositories
        for repository in repositories:
            await _enable(index, repository.repository_id)
        await _build(index, ready.repository_id, C1, _module("aa01", 1))
        await _execute(index, _RAG_OFF, rag_off.repository_id)
        await _execute(index, _UNAVAILABLE, unavailable.repository_id)
        await _execute(index, _DELETED, deleted.repository_id)
        await _execute(index, _GITLAB, gitlab.repository_id)
        await _purge(index, purged.repository_id, PurgeReason.REMOVED)
        fence = await _claim(index, failed.repository_id)
        await _mark_failed(index, failed.repository_id, fence, "archive_unsafe")

        [(before,)] = await _rows(index, "SELECT clock_timestamp()")
        listed = await _heads(index)
        [(after,)] = await _rows(index, "SELECT clock_timestamp()")

        assert [(head.repository, head.generation) for head in listed] == [(empty, 1), (ready, 1)]
        assert all(before < head.listed_at < after for head in listed)
        assert [head.repository for head in await _heads(index, limit=1)] == [empty]
        assert [head.repository for head in await _heads(index, after=empty.repository_id)] == [
            ready
        ]
        assert await _heads(index, after=ready.repository_id) == []

    asyncio.run(scenario())


@pytest.mark.integration
def test_to_rearm_lists_missing_and_removed_or_disabled_indexes_of_enabled_repositories(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index = fleet.index
        # The fleet's repositories have RAG off and no state row: never candidates.
        repositories = await _github_repositories(index, *range(401, 409))
        missing, removed, disabled, ttl, live, unavailable, deleted, gitlab = (
            repository.repository_id for repository in repositories
        )
        for repository_id, reason in (
            (removed, PurgeReason.REMOVED),
            (disabled, PurgeReason.DISABLED),
            (ttl, PurgeReason.TTL),
        ):
            await _enable(index, repository_id)
            await _purge(index, repository_id, reason)
        await _enable(index, live)
        await _execute(index, _UNAVAILABLE, unavailable)
        await _execute(index, _DELETED, deleted)
        await _execute(index, _GITLAB, gitlab)

        assert await _to_rearm(index) == [missing, removed, disabled]
        assert await _to_rearm(index, limit=2) == [missing, removed]
        # A disabled repository with a purged index is no candidate either.
        await _execute(index, _RAG_OFF, removed)
        assert await _to_rearm(index) == [missing, disabled]

    asyncio.run(scenario())


@pytest.mark.integration
def test_to_disarm_lists_live_indexes_whose_rag_is_off_with_the_purge_reason(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index = fleet.index
        repositories = await _github_repositories(index, *range(501, 508))
        rag_off, unavailable, deleted, both, failed, purged, enabled = (
            repository.repository_id for repository in repositories
        )
        for repository_id in (rag_off, unavailable, deleted, both, failed, purged, enabled):
            await _enable(index, repository_id)
        fence = await _claim(index, failed)
        await _mark_failed(index, failed, fence, "archive_unsafe")
        await _purge(index, purged, PurgeReason.DISABLED)
        for repository_id in (rag_off, both, purged):
            await _execute(index, _RAG_OFF, repository_id)
        for repository_id in (unavailable, both, failed):
            await _execute(index, _UNAVAILABLE, repository_id)
        await _execute(index, _DELETED, deleted)

        # RAG turned off wins over a lost installation: `disabled`.
        assert await _to_disarm(index) == [
            DisarmCandidate(rag_off, PurgeReason.DISABLED),
            DisarmCandidate(unavailable, PurgeReason.REMOVED),
            DisarmCandidate(deleted, PurgeReason.REMOVED),
            DisarmCandidate(both, PurgeReason.DISABLED),
            DisarmCandidate(failed, PurgeReason.REMOVED),
        ]
        assert await _to_disarm(index, limit=1) == [DisarmCandidate(rag_off, PurgeReason.DISABLED)]

    asyncio.run(scenario())


def _converge(index: _Index) -> ConvergeIndexes:
    return ConvergeIndexes(uow=partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory))


async def _state_row(index: _Index, repository_id: UUID) -> tuple[object, ...]:
    [row] = await _rows(
        index,
        "SELECT generation, status::text, purge_reason::text, wake_requested_at IS NOT NULL "
        "FROM code_index_states WHERE repository_id = :repository",
        repository=repository_id,
    )
    return row


@pytest.mark.integration
def test_a_rearm_waits_for_a_toggle_off_and_does_not_resurrect_the_index(fleet: _Fleet) -> None:
    async def scenario() -> None:
        index = fleet.index
        [repository] = await _github_repositories(index, 601)
        repository_id = repository.repository_id
        await _enable(index, repository_id)
        await _purge(index, repository_id, PurgeReason.REMOVED)
        assert await _to_rearm(index) == [repository_id]

        # PATCH `ragEnabled=false` in flight: the flag and the purge in one transaction.
        async with index.factory() as toggle:
            await toggle.execute(text(_RAG_OFF), {"repository": repository_id})
            await SqlAlchemyCodeIndexStore(toggle).purge(repository_id, PurgeReason.DISABLED)
            toggle_pid = await toggle.scalar(text("SELECT pg_backend_pid()"))
            run = asyncio.create_task(_converge(index).run(10.0))
            await _wait_until_blocked(index, toggle_pid, 1, run)
            await toggle.commit()
        assert await run is False

        # The re-check read the committed toggle: no re-arm, no marker.
        assert await _state_row(index, repository_id) == (2, "empty", "removed", False)
        assert await _to_rearm(index) == []

        await _execute(
            index,
            "UPDATE repositories SET rag_enabled = true WHERE id = :repository",
            repository_id,
        )
        assert await _converge(index).run(10.0) is False
        assert await _state_row(index, repository_id) == (3, "empty", None, True)

    asyncio.run(scenario())


@pytest.mark.integration
def test_converge_purges_live_indexes_whose_rag_is_off_and_gc_leaves_no_rows(
    fleet: _Fleet,
) -> None:
    async def scenario() -> None:
        index = fleet.index
        repositories = await _github_repositories(index, *range(701, 705))
        rag_off, unavailable, deleted, enabled = (
            repository.repository_id for repository in repositories
        )
        for number, repository_id in enumerate((rag_off, unavailable, deleted, enabled)):
            await _enable(index, repository_id)
            await _build(
                index, repository_id, C1, _module(f"a{number}01", 1), _importer(f"b{number}01", 1)
            )
        await _execute(index, _RAG_OFF, rag_off)
        await _execute(index, _UNAVAILABLE, unavailable)
        await _execute(index, _DELETED, deleted)

        assert await _converge(index).run(10.0) is False
        assert [
            await _state_row(index, repository_id)
            for repository_id in (rag_off, unavailable, deleted, enabled)
        ] == [
            (2, "empty", "disabled", False),
            (2, "empty", "removed", False),
            (2, "empty", "removed", False),
            (1, "ready", None, True),
        ]
        assert (
            await CollectIndexGarbage(
                uow=partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory), batch_rows=100
            ).run(10.0)
            is False
        )
        assert [
            await _rows(index, _COUNT_ROWS, repository=repository_id)
            for repository_id in (rag_off, unavailable, deleted, enabled)
        ] == [[(0, 0, 0, 0)], [(0, 0, 0, 0)], [(0, 0, 0, 0)], [(2, 2, 2, 2)]]

    asyncio.run(scenario())
