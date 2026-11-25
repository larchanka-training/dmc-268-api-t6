"""The #511 data revision retires duplicate live installations and leaves the rest."""

from __future__ import annotations

import asyncio
import importlib.util
from collections.abc import Coroutine
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from sqlalchemy import TextClause, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from tests.template_database import MigratedDatabase

pytest_plugins = ("tests.migrated_postgres",)

_REVISION = importlib.util.spec_from_file_location(
    "retire_duplicate_axyi_installations",
    Path(__file__).resolve().parents[1]
    / "alembic/versions/20261125_0069_retire_duplicate_axyi_installations.py",
)
assert _REVISION is not None and _REVISION.loader is not None
_MIGRATION = importlib.util.module_from_spec(_REVISION)
_REVISION.loader.exec_module(_MIGRATION)
RETIRE = cast(TextClause, _MIGRATION.RETIRE_DUPLICATE_INSTALLATIONS)

_OLDEST = 169030002
_MIDDLE = 172223009
_NEWEST = 172686059
_OTHER = 173181621
_Factory = async_sessionmaker[AsyncSession]


def _install(
    external_id: int,
    *,
    deleted: bool,
    repo: UUID,
) -> tuple[UUID, int, bool, UUID]:
    return (UUID(int=external_id), external_id, deleted, repo)


async def _seed(factory: _Factory, rows: tuple[tuple[UUID, int, bool, UUID], ...]) -> None:
    async with factory.begin() as session:
        for installation_id, external_id, deleted, repo_id in rows:
            workspace_id = UUID(int=external_id + 1)
            await session.execute(
                text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 0)"),
                {"id": workspace_id, "name": f"GitHub: row-{external_id}"},
            )
            await session.execute(
                text(
                    "INSERT INTO provider_installations "
                    "(id, workspace_id, provider, external_id, metadata, deleted_at) "
                    "VALUES (:id, :workspace, 'github', :external, '{}'::jsonb, "
                    "CASE WHEN :deleted THEN clock_timestamp() ELSE NULL END)"
                ),
                {
                    "id": installation_id,
                    "workspace": workspace_id,
                    "external": external_id,
                    "deleted": deleted,
                },
            )
            await session.execute(
                text(
                    "INSERT INTO repositories "
                    "(id, provider_installation_id, external_id, full_name, "
                    "default_branch, web_url, installation_available) "
                    "VALUES (:id, :installation, :external, :name, 'main', :url, true)"
                ),
                {
                    "id": repo_id,
                    "installation": installation_id,
                    "external": external_id,
                    "name": f"axyi/repo-{external_id}",
                    "url": f"https://github.test/axyi/repo-{external_id}",
                },
            )
            await session.execute(
                text(
                    "INSERT INTO github_user_repository_access "
                    "(github_user_id, provider_installation_id, repository_external_id) "
                    "VALUES (511, :installation, :external)"
                ),
                {"installation": installation_id, "external": external_id},
            )


async def _retire(factory: _Factory) -> None:
    async with factory.begin() as session:
        await session.execute(RETIRE)


def _run[T](coro: Coroutine[object, object, T]) -> T:
    import selectors

    return asyncio.run(
        coro,
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def _state(factory: _Factory, external_id: int) -> tuple[bool, bool, int]:
    async def read() -> tuple[bool, bool, int]:
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT installation.deleted_at IS NOT NULL, "
                        "repository.installation_available, "
                        "(SELECT count(*) FROM github_user_repository_access AS access "
                        " WHERE access.provider_installation_id = installation.id) "
                        "FROM provider_installations AS installation "
                        "JOIN repositories AS repository "
                        "  ON repository.provider_installation_id = installation.id "
                        "WHERE installation.external_id = :external"
                    ),
                    {"external": external_id},
                )
            ).one()
            return (bool(row[0]), bool(row[1]), int(row[2]))

    return _run(read())


@pytest.mark.integration
def test_duplicate_live_installations_retire_all_but_the_newest(
    migrated_db: MigratedDatabase,
) -> None:
    database_url, schema = migrated_db.url, migrated_db.schema
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rows = (
        _install(_OLDEST, deleted=False, repo=UUID("00000000-0000-4000-8000-000000000611")),
        _install(_MIDDLE, deleted=False, repo=UUID("00000000-0000-4000-8000-000000000612")),
        _install(_NEWEST, deleted=False, repo=UUID("00000000-0000-4000-8000-000000000613")),
        _install(_OTHER, deleted=False, repo=UUID("00000000-0000-4000-8000-000000000614")),
    )
    try:
        _run(_seed(factory, rows))
        _run(_retire(factory))
        assert _state(factory, _OLDEST) == (True, False, 0)
        assert _state(factory, _MIDDLE) == (True, False, 0)
        assert _state(factory, _NEWEST) == (False, True, 1)
        assert _state(factory, _OTHER) == (False, True, 1)
    finally:
        _run(engine.dispose())


@pytest.mark.integration
def test_one_live_installation_among_the_three_is_left_unchanged(
    migrated_db: MigratedDatabase,
) -> None:
    database_url, schema = migrated_db.url, migrated_db.schema
    engine = create_async_engine(
        database_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    rows = (
        _install(_OLDEST, deleted=True, repo=UUID("00000000-0000-4000-8000-000000000621")),
        _install(_NEWEST, deleted=False, repo=UUID("00000000-0000-4000-8000-000000000622")),
    )
    try:
        _run(_seed(factory, rows))
        _run(_retire(factory))
        assert _state(factory, _OLDEST) == (True, True, 1)
        assert _state(factory, _NEWEST) == (False, True, 1)
    finally:
        _run(engine.dispose())
