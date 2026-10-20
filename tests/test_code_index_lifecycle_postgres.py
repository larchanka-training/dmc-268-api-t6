"""PostgreSQL evidence for the index purge on repository removal and installation deletion (#143).

`installation_repositories.removed` and `installation.deleted` purge the index of the
affected repositories in the delivery's transaction: the `repositories` rows stay, a
repeated delivery changes nothing, a purged index keeps its first reason other than
`ttl`, a sibling's Workspace quota stops counting it, and the GC leader task then leaves
no graph rows.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.index_store import IndexState, IndexStorage
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from app.modules.repositories.application.installation_repositories import RepositoryReference
from app.modules.repositories.application.sync_installation_repositories import (
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from tests.code_index_postgres import GRAPH_ROWS, collect_garbage, graph_rows, publish_index
from tests.test_code_index_store_postgres import (
    _IMPORTER_BYTES,
    _MODULE_BYTES,
    _claim,
    _enable,
    _importer,
    _Index,
    _migration_config,
    _module,
    _purge,
    _repository,
    _rows,
    _sibling,
    _state,
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
