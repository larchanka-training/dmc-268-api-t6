"""PostgreSQL evidence for the index purge triggers and their races with a job (#143).

- `installation_repositories.removed` and `installation.deleted` purge the index of the
  affected repositories in the delivery's transaction: the `repositories` rows stay, a
  repeated delivery changes nothing, a purged index keeps its first reason other than
  `ttl`, a sibling's Workspace quota stops counting it, and the GC leader task then
  leaves no graph rows.
- On the indexer harness of `test_code_index_indexer_postgres.py`, a real trigger (the
  toggle off, or a removal) during the download, or run between two write batches of a
  running job, ends it `LEASE_LOST` with no write after the purge, and the GC leader task
  then leaves no graph rows. After the purge and a re-enable, an old message is `STALE`
  and a message of the current generation claims the lease.
- On the store harness, the same triggers, and `deleted` of two repositories, wait while
  a batch transaction holds the state row lock, complete after its commit without a
  deadlock, and the job's next fenced write raises `IndexLeaseLost`.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from functools import partial
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.build_index import BuildStatus
from app.modules.code_index.application.index_store import (
    IndexLeaseLost,
    IndexState,
    IndexStorage,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.vcs import SourceArchive
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from app.modules.repositories.application.installation_repositories import RepositoryReference
from app.modules.repositories.application.repository_settings import (
    RepositorySettingsChange,
    UpdateRepository,
)
from app.modules.repositories.application.sync_installation_repositories import (
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsUnitOfWork,
)
from app.modules.reviews.application.ast_symbols import SourceParser
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from tests.code_index_postgres import GRAPH_ROWS, collect_garbage, graph_rows, publish_index
from tests.test_code_index_indexer_postgres import (
    _PACKAGE,
    _PACKAGE_V2,
    _SETTINGS,
    Indexer,
    Vcs,
    _ActOnParse,
    _Database,
    _raw_rows,
)
from tests.test_code_index_store_postgres import (
    _IMPORTER_BYTES,
    _MODULE_BYTES,
    _claim,
    _enable,
    _importer,
    _Index,
    _migration_config,
    _module,
    _pause_after_lock,
    _purge,
    _repository,
    _rows,
    _sibling,
    _state,
    _wait_until_blocked,
    _write,
)


@dataclass(frozen=True)
class _Fleet:
    # `index.a` and `a2` share the installation `installation` (external ids 101 and
    # 102); `neighbour` is in another installation of A's Workspace; `index.b` (external
    # id 101) belongs to another installation of another Workspace.
    index: _Index
    installation: UUID
    a2: UUID
    neighbour: UUID


@pytest.fixture
def fleet() -> Iterator[_Fleet]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_index_lifecycle_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                command.upgrade(_migration_config(connection), "head")
                a = UUID(_repository(connection, installation_external_id=17))
                b = UUID(_repository(connection, installation_external_id=18))
                installation = connection.scalar(
                    text("SELECT provider_installation_id FROM repositories WHERE id = :id"),
                    {"id": a},
                )
                a2 = uuid4()
                connection.execute(
                    text(
                        "INSERT INTO repositories "
                        "(id, provider_installation_id, external_id, full_name, default_branch, "
                        "web_url) VALUES (:id, :installation, 102, 'octo/web', 'main', "
                        "'https://github.com/octo/web')"
                    ),
                    {"id": a2, "installation": installation},
                )
                connection.commit()
                neighbour = _sibling(connection, a, external_id=103, installation_external_id=19)
                async_engine = create_async_engine(
                    database_url,
                    connect_args={"options": f"-csearch_path={schema}"},
                    poolclass=NullPool,
                )
                try:
                    factory = async_sessionmaker(async_engine, expire_on_commit=False)
                    yield _Fleet(_Index(factory, a, b), installation, a2, neighbour)
                finally:
                    asyncio.run(async_engine.dispose())
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _sync(index: _Index) -> SyncInstallationRepositories:
    return SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(index.factory),
        rule_sets={},
    )


async def _remove(fleet: _Fleet, *external_ids: int, delivery_id: str) -> None:
    """`installation_repositories.removed` of `fleet.installation`."""
    await _sync(fleet.index).disable(
        provider_installation_id=fleet.installation,
        repositories=tuple(
            RepositoryReference(external_id, f"octo/{external_id}", None, None)
            for external_id in external_ids
        ),
        delivery_id=delivery_id,
    )


async def _delete(fleet: _Fleet, *, delivery_id: str) -> None:
    """`installation.deleted` of `fleet.installation`."""
    await _sync(fleet.index).disable(
        provider_installation_id=fleet.installation,
        repositories=(),
        delivery_id=delivery_id,
        all_repositories=True,
    )


async def _available(index: _Index, repository_id: UUID) -> bool | None:
    """`installation_available` of the repository; None when its row is gone."""
    rows = await _rows(
        index,
        "SELECT installation_available FROM repositories WHERE id = :repository",
        repository=repository_id,
    )
    return rows[0][0] if rows else None


def _summary(state: IndexState | None) -> tuple[int, IndexStatus, PurgeReason | None]:
    assert state is not None
    return state.generation, state.status, state.purge_reason


_PURGED = (2, IndexStatus.EMPTY, PurgeReason.REMOVED)
_LIVE = (1, IndexStatus.READY, None)


@pytest.mark.integration
def test_removed_purges_the_index_of_the_named_repository_and_keeps_its_row(
    fleet: _Fleet,
) -> None:
    index = fleet.index

    async def scenario() -> None:
        await publish_index(index, index.a, fleet.a2, index.b)

        await _remove(fleet, 101, delivery_id="removed-1")

        purged = await _state(index, index.a)
        assert _summary(purged) == _PURGED
        assert _summary(await _state(index, fleet.a2)) == _LIVE
        assert _summary(await _state(index, index.b)) == _LIVE
        assert await _available(index, index.a) is False
        # A repeated delivery changes nothing.
        await _remove(fleet, 101, delivery_id="removed-1")
        assert await _state(index, index.a) == purged
        # The purge is logical; the GC leader task deletes the rows.
        assert await graph_rows(index, index.a) == GRAPH_ROWS
        await collect_garbage(index)
        assert await graph_rows(index, index.a) == (0, 0, 0, 0)
        assert await graph_rows(index, fleet.a2) == GRAPH_ROWS
        assert await graph_rows(index, index.b) == GRAPH_ROWS

    asyncio.run(scenario())


@pytest.mark.integration
def test_deleted_purges_the_index_of_every_repository_of_the_installation(
    fleet: _Fleet,
) -> None:
    index = fleet.index

    async def scenario() -> None:
        await publish_index(index, index.a, fleet.a2, index.b)

        await _delete(fleet, delivery_id="deleted-1")

        purged = [await _state(index, repository_id) for repository_id in (index.a, fleet.a2)]
        assert [_summary(state) for state in purged] == [_PURGED, _PURGED]
        assert _summary(await _state(index, index.b)) == _LIVE
        assert [
            await _available(index, repository_id) for repository_id in (index.a, fleet.a2)
        ] == [
            False,
            False,
        ]
        # A repeated delivery changes nothing.
        await _delete(fleet, delivery_id="deleted-1")
        assert [await _state(index, repository_id) for repository_id in (index.a, fleet.a2)] == (
            purged
        )
        await collect_garbage(index)
        assert await graph_rows(index, index.a) == (0, 0, 0, 0)
        assert await graph_rows(index, fleet.a2) == (0, 0, 0, 0)
        assert await graph_rows(index, index.b) == GRAPH_ROWS

    asyncio.run(scenario())


@pytest.mark.integration
def test_a_purged_index_keeps_its_first_reason_other_than_ttl(fleet: _Fleet) -> None:
    index = fleet.index

    async def scenario() -> None:
        await publish_index(index, index.a, fleet.a2)
        # a2 was turned off before the deliveries.
        await _purge(index, fleet.a2, PurgeReason.DISABLED)
        disabled = await _state(index, fleet.a2)
        await _remove(fleet, 101, delivery_id="removed-1")
        removed = await _state(index, index.a)

        # Another delivery, `deleted` after `removed`, finds both indexes purged.
        await _delete(fleet, delivery_id="deleted-1")

        assert _summary(removed) == _PURGED
        assert _summary(disabled) == (2, IndexStatus.EMPTY, PurgeReason.DISABLED)
        assert await _state(index, index.a) == removed
        assert await _state(index, fleet.a2) == disabled

    asyncio.run(scenario())


@pytest.mark.integration
def test_deleted_drops_the_purged_index_from_a_siblings_workspace_quota(fleet: _Fleet) -> None:
    index = fleet.index

    async def scenario() -> None:
        await publish_index(index, index.a)
        await _enable(index, fleet.neighbour)
        fence = await _claim(index, fleet.neighbour)
        [before] = await _write(index, fleet.neighbour, fence, _module("aa01", 1))

        await _delete(fleet, delivery_id="deleted-1")

        [after] = await _write(index, fleet.neighbour, fence, _importer("ab01", 1))
        # Until `deleted`, the Workspace counts A's published a.py and b.py.
        assert before == IndexStorage(
            stored_bytes=0,
            pending_bytes=_MODULE_BYTES,
            workspace_bytes=_MODULE_BYTES + _MODULE_BYTES + _IMPORTER_BYTES,
        )
        assert after == IndexStorage(
            stored_bytes=0,
            pending_bytes=_MODULE_BYTES + _IMPORTER_BYTES,
            workspace_bytes=_MODULE_BYTES + _IMPORTER_BYTES,
        )

    asyncio.run(scenario())


# --- a purge races a running job ---------------------------------------------------------

type _Trigger = Callable[[_Fleet], Awaitable[None]]


async def _turn_off(fleet: _Fleet) -> None:
    """PATCH `ragEnabled=false` of repository A, as the settings API runs it."""
    uow = partial(
        SqlAlchemyRepositorySettingsUnitOfWork, fleet.index.factory, None, allow_unscoped=True
    )
    changed = await UpdateRepository(uow).execute(
        fleet.index.a, RepositorySettingsChange(rag_enabled=False)
    )
    assert changed is not None


async def _remove_a(fleet: _Fleet) -> None:
    """`installation_repositories.removed` of repository A, a new delivery each time."""
    await _remove(fleet, 101, delivery_id=f"removed-{uuid4()}")


_TRIGGERS = [
    pytest.param(_turn_off, PurgeReason.DISABLED, id="toggle-off"),
    pytest.param(_remove_a, PurgeReason.REMOVED, id="removed"),
]


@dataclass
class _TriggerOnArchive(Vcs):
    """The job's source, running `trigger` just before the download."""

    trigger: Callable[[], Awaitable[None]] | None = None

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        if self.trigger is not None:
            # A lock held across the download would fail here, not hang the run.
            await asyncio.wait_for(self.trigger(), 30)
        return await super().archive(commit_sha, max_bytes=max_bytes)


@dataclass
class _Indexer(Indexer):
    """The harness indexer; its next job runs `on_download` in the download, once."""

    on_download: Callable[[], Awaitable[None]] | None = None

    def _source(self, repository: IndexedRepository) -> Vcs:
        job = _TriggerOnArchive(self.branch, trigger=self.on_download)
        self.on_download = None
        self.jobs.append(job)
        return job


def _race(
    fleet: _Fleet,
    scenario: Callable[[_Indexer, _Database], Awaitable[None]],
    *,
    batch_files: int = _SETTINGS.batch_files,
    parser: Callable[[SourceParser], SourceParser] | None = None,
) -> None:
    database = _Database(fleet.index.factory, fleet.index.a, fleet.index.b)

    async def run() -> None:
        real = SubprocessSourceParser(attempt_cpu_seconds=None)
        try:
            used = parser(real) if parser is not None else real
            settings = replace(_SETTINGS, batch_files=batch_files)
            await scenario(_Indexer(database, used, settings=settings), database)
        finally:
            await real.aclose()

    asyncio.run(run())


async def _purged_and_collected(
    fleet: _Fleet, indexer: Indexer, database: _Database, reason: PurgeReason
) -> None:
    """The index of A is purged by `reason`; after the GC leader task no rows remain."""
    state = await indexer.state(database.a)
    assert (state.generation, state.status, state.purge_reason) == (
        2,
        IndexStatus.EMPTY,
        reason,
    )
    await collect_garbage(fleet.index)
    assert await _raw_rows(database, database.a) == (0, 0, 0, 0)


@pytest.mark.integration
@pytest.mark.parametrize(("trigger", "reason"), _TRIGGERS)
def test_a_purge_during_the_download_loses_the_lease_and_writes_nothing(
    fleet: _Fleet, trigger: _Trigger, reason: PurgeReason
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        await indexer.enable(database.a)
        indexer.branch.push(_PACKAGE)
        assert (await indexer.build(database.a)).status is BuildStatus.PUBLISHED
        published = await _raw_rows(database, database.a)
        indexer.branch.push(_PACKAGE_V2)
        indexer.on_download = partial(trigger, fleet)

        outcome = await indexer.build(database.a)

        assert outcome.status is BuildStatus.LEASE_LOST
        assert outcome.written_files == 0
        # Nothing written after the purge: the rows are still the published ones.
        assert await _raw_rows(database, database.a) == published
        await _purged_and_collected(fleet, indexer, database, reason)

    _race(fleet, scenario)


@pytest.mark.integration
@pytest.mark.parametrize(("trigger", "reason"), _TRIGGERS)
def test_a_purge_run_sequentially_between_write_batches_loses_the_lease_and_writes_nothing_more(
    fleet: _Fleet, trigger: _Trigger, reason: PurgeReason
) -> None:
    # The parser runs the trigger after the first batch committed and before the second
    # one opens, so no batch transaction is open; the concurrent case is on the store
    # harness below.
    after_purge: list[tuple[int, ...]] = []
    database = _Database(fleet.index.factory, fleet.index.a, fleet.index.b)

    async def purge() -> None:
        await trigger(fleet)
        after_purge.append(await _raw_rows(database, database.a))

    async def scenario(indexer: _Indexer, database: _Database) -> None:
        await indexer.enable(database.a)
        indexer.branch.push({f"m{index}.py": f"x{index} = 1\n".encode() for index in range(3)})

        outcome = await indexer.build(database.a)

        assert outcome.status is BuildStatus.LEASE_LOST
        # The first batch was written before the purge, and nothing after it.
        assert after_purge == [(1, 1, 0, 0)]
        assert await _raw_rows(database, database.a) == after_purge[0]
        await _purged_and_collected(fleet, indexer, database, reason)

    _race(
        fleet,
        scenario,
        batch_files=1,
        parser=lambda real: _ActOnParse(real, purge, call=2),
    )


@pytest.mark.integration
@pytest.mark.parametrize(("trigger", "reason"), _TRIGGERS)
def test_an_old_message_after_a_purge_and_a_re_enable_is_stale(
    fleet: _Fleet, trigger: _Trigger, reason: PurgeReason
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        await indexer.enable(database.a)
        indexer.branch.push(_PACKAGE)
        assert (await indexer.build(database.a)).status is BuildStatus.PUBLISHED
        await trigger(fleet)
        await _purged_and_collected(fleet, indexer, database, reason)
        # Re-enabled at store level: no purge reason refuses the old message any more,
        # only its generation does.
        await indexer.enable(database.a)
        enabled = await indexer.state(database.a)

        outcome = await indexer.build(database.a, generation=1)

        assert outcome.status is BuildStatus.STALE
        assert await indexer.state(database.a) == enabled
        assert (enabled.generation, enabled.purge_reason) == (3, None)
        assert (await indexer.build(database.a)).status is BuildStatus.PUBLISHED

    _race(fleet, scenario)


# --- a purge waits for an open batch transaction -------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize(("trigger", "reason"), _TRIGGERS)
def test_a_purge_waits_for_an_open_batch_and_then_fences_the_job_out(
    fleet: _Fleet, trigger: _Trigger, reason: PurgeReason, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = fleet.index

    async def purge() -> None:
        await trigger(fleet)

    async def scenario() -> None:
        await _enable(index, index.a)
        fence = await _claim(index, index.a)
        async with index.uow() as batch:
            batch_pid = await batch.session.scalar(text("SELECT pg_backend_pid()"))
            store = batch.index
            locked, release = _pause_after_lock(monkeypatch, store)
            writing = asyncio.create_task(store.replace_file(index.a, fence, _module("aa01", 1)))
            # The batch holds the admission lock and the state row lock.
            await asyncio.wait_for(locked.wait(), 5)
            purging = asyncio.create_task(purge())
            await _wait_until_blocked(index, batch_pid, 1, purging)
            release.set()
            await writing
            await batch.commit()
        # No deadlock: the trigger completes once the batch has committed.
        await asyncio.wait_for(purging, 5)

        assert _summary(await _state(index, index.a)) == (2, IndexStatus.EMPTY, reason)
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(index.a, fence, _importer("ab01", 1))
        # The batch committed before the purge; its rows are garbage of generation 1.
        assert await graph_rows(index, index.a) == (1, 1, 0, 0)
        await collect_garbage(index)
        assert await graph_rows(index, index.a) == (0, 0, 0, 0)

    asyncio.run(scenario())


@pytest.mark.integration
def test_deleted_waits_for_an_open_batch_of_one_of_its_repositories(
    fleet: _Fleet, monkeypatch: pytest.MonkeyPatch
) -> None:
    index = fleet.index

    async def scenario() -> None:
        await publish_index(index, index.a)
        await _enable(index, fleet.a2)
        fence = await _claim(index, fleet.a2)
        async with index.uow() as batch:
            batch_pid = await batch.session.scalar(text("SELECT pg_backend_pid()"))
            store = batch.index
            locked, release = _pause_after_lock(monkeypatch, store)
            writing = asyncio.create_task(store.replace_file(fleet.a2, fence, _module("aa01", 1)))
            await asyncio.wait_for(locked.wait(), 5)
            deleting = asyncio.create_task(_delete(fleet, delivery_id="deleted-1"))
            await _wait_until_blocked(index, batch_pid, 1, deleting)
            release.set()
            await writing
            await batch.commit()
        await asyncio.wait_for(deleting, 5)

        assert _summary(await _state(index, index.a)) == _PURGED
        assert _summary(await _state(index, fleet.a2)) == _PURGED
        async with index.uow() as uow:
            with pytest.raises(IndexLeaseLost):
                await uow.index.replace_file(fleet.a2, fence, _importer("ab01", 1))
        await collect_garbage(index)
        assert await graph_rows(index, index.a) == (0, 0, 0, 0)
        assert await graph_rows(index, fleet.a2) == (0, 0, 0, 0)

    asyncio.run(scenario())
