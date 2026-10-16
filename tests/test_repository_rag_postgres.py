"""The repository RAG toggle on PostgreSQL: off by default, onboarding never turns it on (#145)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
    SyncInstallationRepositories,
)
from app.modules.repositories.infrastructure.installation_repository_unit_of_work import (
    SqlAlchemyInstallationRepositoriesUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, REPO_A, Env, portal_schema

_RAG_COLUMN = (
    "SELECT is_nullable, column_default FROM information_schema.columns "
    "WHERE table_schema = current_schema() AND table_name = 'repositories' "
    "AND column_name = 'rag_enabled'"
)


@pytest.fixture
def rag_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_repository_rag_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


@pytest.fixture
def rag_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


def _sync(factory: async_sessionmaker[AsyncSession]) -> SyncInstallationRepositories:
    return SyncInstallationRepositories(
        uow_factory=lambda: SqlAlchemyInstallationRepositoriesUnitOfWork(factory),
        rule_sets=load_default_rule_sets(Path("review/rules")),
    )


async def _onboard(sync: SyncInstallationRepositories, external_id: int, name: str) -> UUID:
    (result,) = await sync.execute(
        provider_installation_id=INSTALL_A,
        repositories=(
            RepositoryOnboardingInput(
                RepositorySnapshot(external_id, name, "main", f"https://github.test/{name}"),
                {"Python": 100},
            ),
        ),
    )
    return result.rule_version.repository_id


@pytest.mark.integration
def test_existing_repository_reads_rag_disabled_after_the_upgrade(
    rag_schema: Connection,
) -> None:
    connection = rag_schema
    config = _config(connection)
    command.upgrade(config, "20261015_0032")
    workspace, installation, repository = str(uuid4()), str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'legacy', 0)"),
        {"id": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations (id, workspace_id, provider, external_id, "
            "metadata) VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)"
        ),
        {"id": installation, "workspace": workspace},
    )
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) VALUES (:id, :installation, 101, 'octo/legacy', 'main', "
            "'https://github.test/octo/legacy')"
        ),
        {"id": repository, "installation": installation},
    )
    connection.commit()

    command.upgrade(config, "20261016_0033")

    assert connection.execute(text(_RAG_COLUMN)).one() == ("NO", "false")
    assert (
        connection.scalar(
            text("SELECT rag_enabled FROM repositories WHERE id = :id"), {"id": repository}
        )
        is False
    )
    connection.commit()
    command.downgrade(config, "20261015_0032")
    assert connection.execute(text(_RAG_COLUMN)).all() == []


@pytest.mark.integration
def test_onboarding_a_new_repository_leaves_rag_disabled(rag_env: Env) -> None:
    async def exercise() -> object:
        engine = rag_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            repository = await _onboard(_sync(factory), 103, "octo/new")
            async with factory() as session:
                return await session.scalar(
                    text("SELECT rag_enabled FROM repositories WHERE id = :id"),
                    {"id": repository},
                )
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) is False


@pytest.mark.integration
def test_onboarding_an_existing_repository_again_keeps_rag_enabled(rag_env: Env) -> None:
    async def exercise() -> tuple[UUID, object]:
        engine = rag_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE repositories SET rag_enabled = true WHERE id = :id"),
                    {"id": REPO_A},
                )
            repository = await _onboard(_sync(factory), 101, "octo/a")
            async with factory() as session:
                enabled = await session.scalar(
                    text("SELECT rag_enabled FROM repositories WHERE id = :id"),
                    {"id": REPO_A},
                )
            return repository, enabled
        finally:
            await engine.dispose()

    assert asyncio.run(exercise()) == (REPO_A, True)
