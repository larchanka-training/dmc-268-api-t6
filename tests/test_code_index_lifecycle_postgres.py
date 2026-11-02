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
- End to end through the real update triggers (the webhook dispatcher with the index
  refresh, the toggle, the converge pass and the head reconcile), the wake-up dispatch,
  the job and the GC leader task: two merges wake one incremental build up to the second;
  other pull request events, pushes elsewhere and a deleted ref change nothing; a push to
  the default branch sets the target and the marker; a changed default branch, a direct
  push and both lost-wake-up states of a job are reconciled into a build, an equal head is
  not; a reconnected or rebound repository is re-armed in a new generation and built in
  full; a late push never moves the index back and a force push builds in full; off and
  on during a job fence it out and leave only the rows of the new generation.
- The races of the wake-up outbox and the toggle: a push between a wake-up and the clear
  of its marker keeps the marker for the next dispatch; a trigger during a job is woken
  once the job has published; a request racing a purge waits for its row lock and requests
  nothing; converge disarming an index while a PATCH turns RAG on ends either purged and
  re-armed or live, never live with RAG off.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildStatus,
    FullBuildReason,
    IndexVersions,
    limits_profile_version,
)
from app.modules.code_index.application.index_refresh import RequestIndexRefresh
from app.modules.code_index.application.index_store import (
    IndexLeaseLost,
    IndexState,
    IndexStorage,
)
from app.modules.code_index.application.leader_tasks import (
    ConvergeIndexes,
    DispatchIndexWakeups,
    ReconcileIndexHeads,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.vcs import (
    Ancestry,
    BranchHead,
    Comparison,
    RepositorySnapshotSource,
    SourceArchive,
    VcsAccessDenied,
)
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from app.modules.code_index.infrastructure.index_refresh import SqlAlchemyIndexRefreshUnitOfWork
from app.modules.code_index.infrastructure.maintenance import (
    SqlAlchemyCodeIndexMaintenanceUnitOfWork,
)
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
    GitHubInstallationResolver,
    InstallationOnboardingHandler,
)
from app.modules.repositories.application.installation_repositories import (
    RepositoryReference,
    RepositorySnapshot,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.repository_settings import (
    RepositorySettingsChange,
    UpdateRepository,
)
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsUnitOfWork,
)
from app.modules.reviews.application.ast_symbols import SourceParser
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestProjectionStatus,
)
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.workspaces.application.link_github_installations import GitHubInstallation
from app.modules.workspaces.infrastructure.github_repository_rebinding import (
    SqlAlchemyGitHubRepositoryRebinding,
)
from tests.code_index_postgres import GRAPH_ROWS, collect_garbage, graph_rows, publish_index
from tests.test_code_index_indexer_postgres import (
    _PACKAGE,
    _PACKAGE_V2,
    _SETTINGS,
    _VERSIONS,
    Branch,
    Indexer,
    Link,
    Tree,
    Vcs,
    Wakeups,
    _ActOnParse,
    _changes,
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
    _marker,
    _migration_config,
    _module,
    _pause_after_lock,
    _purge,
    _release,
    _repository,
    _rows,
    _sibling,
    _state,
    _wait_until_blocked,
    _write,
)
from tests.test_github_installation_dispatch import _merge_delivery, _push_delivery


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
class _Branch(Branch):
    """The default branch; a force push replaces its head with an unrelated commit."""

    orphans: dict[str, Tree] = field(default_factory=dict)

    def force_push(self, tree: Tree) -> str:
        sha, old = self.commits.pop()
        self.orphans[sha] = old
        rewritten = f"{0xF0 + len(self.orphans):02x}" * 20
        self.commits.append((rewritten, dict(tree)))
        return rewritten


@dataclass
class _TriggerOnArchive(Vcs):
    """The job's source, running `trigger` just before the download.

    Its head is on the default branch `name`; a base a force push left behind compares
    as diverged.
    """

    trigger: Callable[[], Awaitable[None]] | None = None
    name: str = "main"

    async def head(self) -> BranchHead | None:
        head = await super().head()
        return None if head is None else BranchHead(self.name, head.commit_sha)

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        orphans = self.branch.orphans if isinstance(self.branch, _Branch) else {}
        if base_sha not in orphans:
            return await super().compare(base_sha, head_sha)
        self.requests += 1
        self.compares += 1
        changes = _changes(orphans[base_sha], self.branch.tree(head_sha))
        return Comparison(Ancestry.DIVERGED, changes, complete=True)

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        if self.trigger is not None:
            # A lock held across the download would fail here, not hang the run.
            await asyncio.wait_for(self.trigger(), 30)
        return await super().archive(commit_sha, max_bytes=max_bytes)


@dataclass
class _Indexer(Indexer):
    """The harness indexer; its next job runs `on_download` in the download, once."""

    branch: _Branch = field(default_factory=_Branch)
    on_download: Callable[[], Awaitable[None]] | None = None
    default_branch: str = "main"

    @property
    def versions(self) -> IndexVersions:
        """The versions its jobs record, as `Indexer.build` composes them."""
        return replace(_VERSIONS, limits_profile_version=limits_profile_version(self.limits))

    def _source(self, repository: IndexedRepository) -> Vcs:
        job = _TriggerOnArchive(self.branch, trigger=self.on_download, name=self.default_branch)
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


# --- the update triggers end to end (#143) -------------------------------------------------

# A file `_PACKAGE_V2` lacks, so a third commit differs from the second.
_PACKAGE_V3: Tree = {**_PACKAGE_V2, "extra.py": b"def extra():\n    return 2\n"}
_ACCOUNT = 4242


async def _turn_on(fleet: _Fleet) -> None:
    """PATCH `ragEnabled=true` of repository A, as the settings API runs it."""
    uow = partial(
        SqlAlchemyRepositorySettingsUnitOfWork, fleet.index.factory, None, allow_unscoped=True
    )
    changed = await UpdateRepository(uow).execute(
        fleet.index.a, RepositorySettingsChange(rag_enabled=True)
    )
    assert changed is not None and changed.rag_enabled


async def _deliver(index: _Index, delivery: VerifiedGitHubDelivery) -> str | None:
    """A webhook delivery through the dispatcher with the real index refresh; the PR
    projection is not under test and always applies. Returns the delivery's detail."""

    class Projected:
        async def execute(self, event: PullRequestEvent) -> PullRequestProjectionStatus:
            return PullRequestProjectionStatus.PROJECTED

    adapter = GitHubWebhookDispatchAdapter(
        GitHubInstallationDeliveryDispatcher(
            resolver=cast(GitHubInstallationResolver, None),
            onboarding=cast(InstallationOnboardingHandler, None),
            pull_request_projector=Projected(),
            index_refresh=RequestIndexRefresh(
                partial(SqlAlchemyIndexRefreshUnitOfWork, index.factory)
            ),
        )
    )
    return (await adapter.execute(delivery.to_receipt())).detail


def _maintenance(index: _Index) -> Callable[[], SqlAlchemyCodeIndexMaintenanceUnitOfWork]:
    return partial(SqlAlchemyCodeIndexMaintenanceUnitOfWork, index.factory)


async def _dispatch(index: _Index) -> list[tuple[UUID, int]]:
    """One run of the wake-up dispatch; the wake-ups it sent."""
    wakeups = Wakeups()
    await DispatchIndexWakeups(uow=_maintenance(index), wakeups=wakeups).run(10.0)
    return wakeups.woken


async def _reconcile(index: _Index, indexer: _Indexer) -> None:
    """One run of the head reconcile over the indexer's branch."""

    def sources() -> Callable[[IndexedRepository], RepositorySnapshotSource]:
        return lambda repository: _TriggerOnArchive(indexer.branch, name=indexer.default_branch)

    await ReconcileIndexHeads(
        uow=_maintenance(index), sources=sources, versions=indexer.versions, batch=50
    ).run(10.0)


async def _converge(index: _Index) -> None:
    await ConvergeIndexes(uow=_maintenance(index)).run(10.0)


async def _built(fleet: _Fleet, indexer: _Indexer, tree: Tree) -> str:
    """Repository A turned on, woken by the dispatch and built in full at `tree`."""
    await _turn_on(fleet)
    head = indexer.branch.push(tree)
    assert await _dispatch(fleet.index) == [(fleet.index.a, 1)]
    outcome = await indexer.build(fleet.index.a)
    assert (outcome.status, outcome.mode, outcome.commit_sha) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        head,
    )
    return head


async def _generations(index: _Index, repository_id: UUID) -> list[tuple[int]]:
    """The generations of the repository's stored file rows."""
    return cast(
        list[tuple[int]],
        await _rows(
            index,
            "SELECT DISTINCT generation FROM code_index_files WHERE repository_id = :repository",
            repository=repository_id,
        ),
    )


@pytest.mark.integration
def test_ac1_two_merges_before_the_dispatch_wake_one_incremental_build_up_to_the_second(
    fleet: _Fleet,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        first = indexer.branch.push(_PACKAGE_V2)
        first_detail = await _deliver(index, _merge_delivery(merge_commit_sha=first))
        second = indexer.branch.push(_PACKAGE_V3)
        second_detail = await _deliver(index, _merge_delivery(merge_commit_sha=second))
        target = (await indexer.state(index.a)).target_commit_sha

        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert first_detail == second_detail == "action=closed projected index=requested"
        assert target == second
        assert woken == [(index.a, 1)]
        # The change set is the compare from the indexed commit, not either PR's diff.
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL,
            second,
        )
        assert indexer.jobs[-1].compares == 1
        assert (await indexer.state(index.a)).indexed_commit_sha == second
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac1_other_pull_request_events_leave_the_row_and_the_marker_alone(fleet: _Fleet) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        merge = indexer.branch.push(_PACKAGE_V2)
        before = await indexer.state(index.a)

        details = [
            await _deliver(index, delivery)
            for delivery in (
                _merge_delivery(base="feature", merge_commit_sha=merge),
                _merge_delivery(merged=False, merge_commit_sha=merge),
                _merge_delivery("opened", merged=False),
                _merge_delivery("synchronize", merged=False),
                _merge_delivery("edited", merged=False),
            )
        ]

        assert details == [
            "action=closed projected",
            "action=closed projected",
            "action=opened projected",
            # Without a run trigger the dispatcher defers it, after the projection.
            "action=synchronize",
            "action=edited projected",
        ]
        assert await indexer.state(index.a) == before
        assert await _marker(index, index.a) is None
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac7_a_push_to_the_default_branch_sets_the_target_and_the_marker(fleet: _Fleet) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        pushed = indexer.branch.push(_PACKAGE_V2)
        before = await indexer.state(index.a)
        elsewhere = [
            await _deliver(index, delivery)
            for delivery in (
                _push_delivery("refs/heads/feature", after=pushed),
                _push_delivery("refs/tags/main", after=pushed),
                _push_delivery("refs/heads/main", after="0" * 40),
                _push_delivery("refs/heads/main", after="0" * 40, deleted=True),
            )
        ]
        unchanged = await indexer.state(index.a)
        unmarked = await _marker(index, index.a)

        detail = await _deliver(index, _push_delivery("refs/heads/main", after=pushed))
        requested = await indexer.state(index.a)
        marker = await _marker(index, index.a)
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert elsewhere == ["not_default_branch", "not_default_branch", None, None]
        assert (unchanged, unmarked) == (before, None)
        assert detail == "index=requested"
        assert requested.target_commit_sha == pushed
        assert marker is not None
        assert woken == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL,
            pushed,
        )

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac2_a_changed_default_branch_is_reconciled_into_a_full_build_of_the_new_branch(
    fleet: _Fleet,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        head = await _built(fleet, indexer, _PACKAGE)
        indexer.default_branch = "develop"

        await _reconcile(index, indexer)
        target = (await indexer.state(index.a)).target_commit_sha
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)
        state = await indexer.state(index.a)

        assert target == head
        assert woken == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.full_reason, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            FullBuildReason.BRANCH_CHANGED,
            head,
        )
        assert state.profile is not None and state.profile.default_branch == "develop"
        # The repositories row keeps its stored default branch; the reconcile reads the
        # live one.
        assert await _rows(
            index,
            "SELECT default_branch FROM repositories WHERE id = :repository",
            repository=index.a,
        ) == [("main",)]
        await _reconcile(index, indexer)
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac3_the_reconcile_wakes_a_direct_push_and_leaves_an_equal_head_alone(
    fleet: _Fleet,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        head = await _built(fleet, indexer, _PACKAGE)

        await _reconcile(index, indexer)
        equal = await indexer.state(index.a)
        equal_marker = await _marker(index, index.a)
        equal_woken = await _dispatch(index)
        # A direct push: no delivery, only the reconcile sees it.
        pushed = indexer.branch.push(_PACKAGE_V2)
        await _reconcile(index, indexer)
        behind = await indexer.state(index.a)
        behind_woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert (equal.indexed_commit_sha, equal.target_commit_sha) == (head, head)
        assert (equal_marker, equal_woken) == (None, [])
        assert (behind.indexed_commit_sha, behind.target_commit_sha) == (head, pushed)
        assert behind_woken == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL,
            pushed,
        )

    _race(fleet, scenario)


@pytest.mark.integration
@pytest.mark.parametrize("published", [False, True], ids=["empty", "ready"])
def test_ac3_the_reconcile_recovers_an_index_whose_wake_up_was_lost(
    fleet: _Fleet, published: bool
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        indexed = await _built(fleet, indexer, _PACKAGE) if published else None
        if not published:
            await _turn_on(fleet)
            # The enable's wake-up went out, and its message was lost.
            assert await _dispatch(index) == [(index.a, 1)]
        head = indexer.branch.push(_PACKAGE_V2 if published else _PACKAGE)
        # A job failed transiently and released its lease: no message is left.
        fence = await _claim(index, index.a)
        await _release(index, index.a, fence, last_error_code="vcs_unavailable")
        lost = await indexer.state(index.a)
        lost_marker = await _marker(index, index.a)

        await _reconcile(index, indexer)
        target = (await indexer.state(index.a)).target_commit_sha
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert (lost.status, lost.indexed_commit_sha, lost.last_error_code) == (
            IndexStatus.READY if published else IndexStatus.EMPTY,
            indexed,
            "vcs_unavailable",
        )
        assert lost_marker is None
        assert target == head
        assert woken == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL if published else BuildMode.FULL,
            head,
        )

    _race(fleet, scenario)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("rules", "code"),
    [
        pytest.param(b"x" * 65_537, "policy_document_too_large", id="over-the-limit"),
        pytest.param(Link("README.md"), "policy_document_type_unsupported", id="not-a-blob"),
    ],
)
def test_ac3_the_reconcile_rebuilds_a_policy_refusal_once_and_then_only_on_a_new_head(
    fleet: _Fleet, rules: bytes | Link, code: str
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _turn_on(fleet)
        head = indexer.branch.push({**_PACKAGE, ".review/rules.md": rules})
        assert await _dispatch(index) == [(index.a, 1)]
        refused = await indexer.build(index.a)

        # The enable wrote no target, so only the first pass finds the head new.
        woken: list[list[tuple[UUID, int]]] = []
        rebuilt = []
        for _ in range(2):
            await _reconcile(index, indexer)
            woken.append(await _dispatch(index))
            if woken[-1]:
                rebuilt.append(await indexer.build(index.a))
        state = await indexer.state(index.a)
        fixed = indexer.branch.push(_PACKAGE)
        await _reconcile(index, indexer)
        fixed_woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert (refused.status, refused.error_code) == (BuildStatus.RELEASED, code)
        assert woken == [[(index.a, 1)], []]
        assert [(item.status, item.error_code) for item in rebuilt] == [
            (BuildStatus.RELEASED, code)
        ]
        assert (
            state.status,
            state.indexed_commit_sha,
            state.target_commit_sha,
            state.last_error_code,
        ) == (IndexStatus.EMPTY, None, head, code)
        assert fixed_woken == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            fixed,
        )
        assert len(indexer.jobs) == 3

    _race(fleet, scenario)


@pytest.mark.integration
def test_static_token_refusal_does_not_repeat_the_full_walk_on_later_reconciles(
    fleet: _Fleet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        await _turn_on(fleet)
        head = indexer.branch.push(_PACKAGE)
        assert await _dispatch(fleet.index) == [(database.a, 1)]

        async def deny_download() -> None:
            raise VcsAccessDenied(
                "Bitbucket source is not accessible (HTTP 401)", http_status=401, pin_head=True
            )

        indexer.on_download = deny_download
        with caplog.at_level("INFO"):
            first = await indexer.build(database.a)
        assert (first.status, first.error_code) == (BuildStatus.RELEASED, "vcs_access_denied")
        assert first.http_status == 401
        assert "http_status=401" in caplog.text
        first_reads = indexer.jobs[0].requests

        # The terminal refusal records the observed head even when enable had no target.
        for _ in range(3):
            await _reconcile(fleet.index, indexer)
            assert await _dispatch(fleet.index) == []
        state = await indexer.state(database.a)
        assert (state.target_commit_sha, state.last_error_code) == (head, "vcs_access_denied")
        assert state.last_error_http_status == 401
        assert len(indexer.jobs) == 1
        assert indexer.jobs[0].requests == first_reads

    _race(fleet, scenario)


@pytest.mark.integration
def test_rules_redirect_status_is_logged_and_persisted_as_terminal(
    fleet: _Fleet, caplog: pytest.LogCaptureFixture
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        await _turn_on(fleet)
        head = indexer.branch.push(_PACKAGE)
        indexer.branch.policy_errors[head] = PolicyError(
            PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
            "Bitbucket rules redirect to an unsupported blob (HTTP 302)",
            http_status=302,
        )
        assert await _dispatch(fleet.index) == [(database.a, 1)]
        with caplog.at_level("INFO"):
            outcome = await indexer.build(database.a)
        state = await indexer.state(database.a)

        assert (outcome.status, outcome.error_code, outcome.http_status) == (
            BuildStatus.RELEASED,
            "policy_document_type_unsupported",
            302,
        )
        assert "http_status=302" in caplog.text
        assert state.last_error_code == "policy_document_type_unsupported"
        assert state.last_error_http_status == 302

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac3_a_push_after_a_policy_refusal_is_rebuilt_by_the_reconcile_when_its_wake_up_is_lost(
    fleet: _Fleet,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        indexed = await _built(fleet, indexer, _PACKAGE)
        refused_head = indexer.branch.push({**_PACKAGE_V2, ".review/rules.md": b"x" * 65_537})
        await _deliver(index, _push_delivery("refs/heads/main", after=refused_head))
        assert await _dispatch(index) == [(index.a, 1)]
        refused = await indexer.build(index.a)
        head = indexer.branch.push(_PACKAGE_V3)
        detail = await _deliver(index, _push_delivery("refs/heads/main", after=head))
        # The push's wake-up went out, and its message was lost.
        lost = await _dispatch(index)
        requested = await indexer.state(index.a)

        await _reconcile(index, indexer)
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert (refused.status, refused.error_code) == (
            BuildStatus.RELEASED,
            "policy_document_too_large",
        )
        assert detail == "index=requested"
        assert lost == [(index.a, 1)]
        assert (
            requested.indexed_commit_sha,
            requested.target_commit_sha,
            requested.last_error_code,
        ) == (indexed, head, None)
        assert woken == [(index.a, 1)]
        assert (outcome.status, outcome.commit_sha) == (BuildStatus.PUBLISHED, head)

    _race(fleet, scenario)


async def _add(fleet: _Fleet) -> None:
    """`installation_repositories.added` of repository A again."""
    await SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(fleet.index.factory),
        rule_sets=load_default_rule_sets(Path("review/rules")),
    ).execute(
        provider_installation_id=fleet.installation,
        repositories=(
            RepositoryOnboardingInput(
                RepositorySnapshot(101, "octo/repo", "main", "https://github.com/octo/repo"),
                {"Python": 100},
            ),
        ),
    )


async def _rebind(fleet: _Fleet) -> None:
    """The same account installs the App again (#113): A moves to the new installation."""
    async with fleet.index.factory.begin() as session:
        installation = uuid4()
        await session.execute(
            text(
                "INSERT INTO provider_installations "
                "(id, workspace_id, provider, external_id, account_external_id, metadata) "
                "SELECT :id, workspace_id, 'github', 20, account_external_id, metadata "
                "FROM provider_installations WHERE id = :old"
            ),
            {"id": installation, "old": fleet.installation},
        )
        target = await session.get(ProviderInstallation, installation)
        assert target is not None
        await SqlAlchemyGitHubRepositoryRebinding(session).rebind(
            target, GitHubInstallation(20, "octo", (101,), _ACCOUNT)
        )


async def _remove_then_add(fleet: _Fleet) -> None:
    await _remove(fleet, 101, delivery_id="removed-1")
    await _add(fleet)


async def _delete_then_rebind(fleet: _Fleet) -> None:
    async with fleet.index.factory.begin() as session:
        await session.execute(
            text("UPDATE provider_installations SET account_external_id = :account WHERE id = :id"),
            {"account": _ACCOUNT, "id": fleet.installation},
        )
    await _delete(fleet, delivery_id="deleted-1")
    await _rebind(fleet)


@pytest.mark.integration
@pytest.mark.parametrize(
    "reconnect",
    [pytest.param(_remove_then_add, id="added"), pytest.param(_delete_then_rebind, id="rebind")],
)
def test_ac5_a_reconnected_repository_is_re_armed_and_built_in_full(
    fleet: _Fleet, reconnect: Callable[[_Fleet], Awaitable[None]]
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        head = await _built(fleet, indexer, _PACKAGE)

        await reconnect(fleet)
        reconnected = await indexer.state(index.a)
        available = await _available(index, index.a)
        await _converge(index)
        rearmed = await indexer.state(index.a)
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)
        await collect_garbage(index)

        assert _summary(reconnected) == _PURGED
        assert available is True
        assert _summary(rearmed) == (3, IndexStatus.EMPTY, None)
        assert woken == [(index.a, 3)]
        assert (outcome.status, outcome.mode, outcome.full_reason, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            FullBuildReason.NO_INDEX,
            head,
        )
        assert await _generations(index, index.a) == [(3,)]

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac9_a_late_push_never_moves_the_index_back(fleet: _Fleet) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        a = indexer.branch.push(_PACKAGE_V2)
        b = indexer.branch.push(_PACKAGE_V3)
        # The push of B is delivered first, the push of A late.
        await _deliver(index, _push_delivery("refs/heads/main", after=b))
        await _deliver(index, _push_delivery("refs/heads/main", after=a))
        late = await indexer.state(index.a)

        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)
        built = await indexer.state(index.a)
        await _reconcile(index, indexer)
        reconciled = await indexer.state(index.a)

        assert late.target_commit_sha == a
        assert woken == [(index.a, 1)]
        # The job builds the head, not the target.
        assert (outcome.status, outcome.commit_sha) == (BuildStatus.PUBLISHED, b)
        assert (built.indexed_commit_sha, built.target_commit_sha) == (b, a)
        assert (reconciled.indexed_commit_sha, reconciled.target_commit_sha) == (b, b)
        assert await _marker(index, index.a) is None
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac9_a_force_push_builds_in_full(fleet: _Fleet) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        rewritten = indexer.branch.force_push(_PACKAGE_V2)

        detail = await _deliver(index, _push_delivery("refs/heads/main", after=rewritten))
        woken = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert detail == "index=requested"
        assert woken == [(index.a, 1)]
        assert indexer.jobs[-1].compares == 1
        assert (outcome.status, outcome.mode, outcome.full_reason, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            FullBuildReason.NOT_AHEAD,
            rewritten,
        )
        assert (await indexer.state(index.a)).indexed_commit_sha == rewritten

    _race(fleet, scenario)


@pytest.mark.integration
def test_ac8_off_and_on_during_a_job_fence_it_out_and_only_the_new_generation_remains(
    fleet: _Fleet,
) -> None:
    # The parser runs the toggles after the first batch committed and before the second
    # one opens: the job waits in its parse while the toggle goes off and on again.
    toggled: list[tuple[int, ...]] = []
    database = _Database(fleet.index.factory, fleet.index.a, fleet.index.b)

    async def off_and_on() -> None:
        await _turn_off(fleet)
        await _turn_on(fleet)
        toggled.append(await _raw_rows(database, database.a))

    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _turn_on(fleet)
        assert await _dispatch(index) == [(index.a, 1)]
        head = indexer.branch.push({f"m{n}.py": f"x{n} = 1\n".encode() for n in range(3)})

        old = await indexer.build(index.a)
        rearmed = await indexer.state(index.a)
        woken = await _dispatch(index)
        new = await indexer.build(index.a)
        stale = await indexer.build(index.a, generation=1)
        await collect_garbage(index)

        assert old.status is BuildStatus.LEASE_LOST
        # The first batch was written before the toggles, and nothing after them.
        assert toggled == [(1, 1, 0, 0)]
        assert _summary(rearmed) == (3, IndexStatus.EMPTY, None)
        assert woken == [(index.a, 3)]
        assert (new.status, new.mode, new.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.FULL,
            head,
        )
        assert stale.status is BuildStatus.STALE
        assert await _generations(index, index.a) == [(3,)]
        assert await _raw_rows(database, database.a) == (3, 3, 0, 0)

    _race(
        fleet,
        scenario,
        batch_files=1,
        parser=lambda real: _ActOnParse(real, off_and_on, call=2),
    )


# --- the races of the wake-up outbox and the toggle (#143) ---------------------------------


@pytest.mark.integration
def test_a_push_between_a_wake_up_and_its_clear_keeps_the_marker_for_the_next_dispatch(
    fleet: _Fleet,
) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        first = indexer.branch.push(_PACKAGE_V2)
        await _deliver(index, _push_delivery("refs/heads/main", after=first))
        later: list[str] = []

        @dataclass
        class PushOnWake(Wakeups):
            async def wake(self, repository_id: UUID, generation: int) -> None:
                await super().wake(repository_id, generation)
                # The push lands after the wake-up and before the marker is cleared.
                later.append(indexer.branch.push(_PACKAGE_V3))
                await _deliver(index, _push_delivery("refs/heads/main", after=later[0]))

        wakeups = PushOnWake()
        await DispatchIndexWakeups(uow=_maintenance(index), wakeups=wakeups).run(10.0)
        survived = await _marker(index, index.a)
        target = (await indexer.state(index.a)).target_commit_sha
        woken_again = await _dispatch(index)
        outcome = await indexer.build(index.a)

        assert wakeups.woken == [(index.a, 1)]
        assert survived is not None
        assert target == later[0]
        assert woken_again == [(index.a, 1)]
        assert (outcome.status, outcome.mode, outcome.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL,
            later[0],
        )
        assert (await indexer.state(index.a)).indexed_commit_sha == later[0]
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_a_trigger_during_a_job_is_woken_once_the_job_has_published(fleet: _Fleet) -> None:
    async def scenario(indexer: _Indexer, database: _Database) -> None:
        index = fleet.index
        await _built(fleet, indexer, _PACKAGE)
        first = indexer.branch.push(_PACKAGE_V2)
        await _deliver(index, _push_delivery("refs/heads/main", after=first))
        assert await _dispatch(index) == [(index.a, 1)]
        during: list[object] = []

        async def push_during_the_job() -> None:
            second = indexer.branch.push(_PACKAGE_V3)
            await _deliver(index, _push_delivery("refs/heads/main", after=second))
            # The job holds the lease: the dispatch leaves the marker for later.
            during.extend([second, await _marker(index, index.a) is not None])
            during.append(await _dispatch(index))

        indexer.on_download = push_during_the_job
        running = await indexer.build(index.a)
        woken = await _dispatch(index)
        following = await indexer.build(index.a)

        second = during[0]
        assert during == [second, True, []]
        assert (running.status, running.commit_sha) == (BuildStatus.PUBLISHED, first)
        assert woken == [(index.a, 1)]
        assert (following.status, following.mode, following.commit_sha) == (
            BuildStatus.PUBLISHED,
            BuildMode.INCREMENTAL,
            second,
        )
        assert await _dispatch(index) == []

    _race(fleet, scenario)


@pytest.mark.integration
def test_an_index_request_racing_a_purge_waits_for_its_lock_and_requests_nothing(
    fleet: _Fleet,
) -> None:
    index = fleet.index

    async def scenario() -> None:
        await _turn_on(fleet)
        request = RequestIndexRefresh(partial(SqlAlchemyIndexRefreshUnitOfWork, index.factory))
        # The toggle off, held open after its purge took the state row lock.
        async with SqlAlchemyRepositorySettingsUnitOfWork(
            index.factory, None, allow_unscoped=True
        ) as toggle:
            toggle_pid = await toggle.session.scalar(text("SELECT pg_backend_pid()"))
            await toggle.repositories.update_repository(
                index.a, RepositorySettingsChange(rag_enabled=False)
            )
            await toggle.index_purger.purge(index.a, PurgeReason.DISABLED)
            # The delivery still resolves the repository with RAG on, then waits.
            requesting = asyncio.create_task(request.execute(17, 101, "c" * 40))
            await _wait_until_blocked(index, toggle_pid, 1, requesting)
            await toggle.commit()
        requested = await asyncio.wait_for(requesting, 5)
        state = await _state(index, index.a)

        assert requested is False
        assert _summary(state) == (2, IndexStatus.EMPTY, PurgeReason.DISABLED)
        assert state is not None and state.target_commit_sha is None
        assert await _marker(index, index.a) is None

    asyncio.run(scenario())


async def _rag_in_effect(index: _Index, repository_id: UUID) -> bool:
    [(in_effect,)] = await _rows(
        index,
        "SELECT rag_enabled AND installation_available FROM repositories WHERE id = :repository",
        repository=repository_id,
    )
    return cast(bool, in_effect)


async def _disarm_first(fleet: _Fleet) -> None:
    """Converge locks the `repositories` row and purges; the PATCH waits, then re-arms."""
    index = fleet.index
    async with _maintenance(index)() as converge:
        converge_pid = await converge.session.scalar(text("SELECT pg_backend_pid()"))
        reason = await converge.maintenance.disarm_reason(index.a)
        assert reason is PurgeReason.DISABLED
        await converge.index.purge(index.a, reason)
        turning_on = asyncio.create_task(_turn_on(fleet))
        await _wait_until_blocked(index, converge_pid, 1, turning_on)
        await converge.commit()
    await asyncio.wait_for(turning_on, 5)


async def _toggle_first(fleet: _Fleet) -> None:
    """The PATCH holds the `repositories` row; converge lists the index, waits, and its
    re-check finds RAG on."""
    index = fleet.index
    async with SqlAlchemyRepositorySettingsUnitOfWork(
        index.factory, None, allow_unscoped=True
    ) as toggle:
        toggle_pid = await toggle.session.scalar(text("SELECT pg_backend_pid()"))
        await toggle.repositories.update_repository(
            index.a, RepositorySettingsChange(rag_enabled=True)
        )
        await toggle.index_enabler.enable(index.a)
        converging = asyncio.create_task(_converge(index))
        await _wait_until_blocked(index, toggle_pid, 1, converging)
        await toggle.commit()
    await asyncio.wait_for(converging, 5)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("race", "expected"),
    [
        pytest.param(_disarm_first, (3, IndexStatus.EMPTY, None), id="disarm-first"),
        pytest.param(_toggle_first, (1, IndexStatus.READY, None), id="toggle-first"),
    ],
)
def test_converge_racing_a_patch_that_turns_rag_on_never_leaves_a_live_index_with_rag_off(
    fleet: _Fleet,
    race: Callable[[_Fleet], Awaitable[None]],
    expected: tuple[int, IndexStatus, PurgeReason | None],
) -> None:
    index = fleet.index

    async def scenario() -> None:
        # A live index whose RAG is off: converge's disarm candidate.
        await publish_index(index, index.a)
        assert await _dispatch(index) == [(index.a, 1)]
        assert not await _rag_in_effect(index, index.a)

        await race(fleet)
        state = await _state(index, index.a)
        marker = await _marker(index, index.a)

        assert _summary(state) == expected
        assert await _rag_in_effect(index, index.a)
        # Purged then re-armed carries the re-arm's wake-up marker.
        assert (marker is not None) is (expected[1] is IndexStatus.EMPTY)

    asyncio.run(scenario())
