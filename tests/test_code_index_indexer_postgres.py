"""PostgreSQL evidence for the graph indexer (#142).

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
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.infrastructure.repository_reader import (
    SqlAlchemyIndexRepositoryReader,
)


@pytest.fixture
def code_index_database() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_indexer_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _repository(connection: Connection, installation_external_id: int, full_name: str) -> UUID:
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
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
            "VALUES (:id, :installation, 101, :full_name, 'main', 'https://github.com/x')"
        ),
        {"id": repository_id, "installation": installation_id, "full_name": full_name},
    )
    return repository_id


@dataclass(frozen=True)
class _Database:
    factory: async_sessionmaker[AsyncSession]
    a: UUID
    b: UUID


@pytest.fixture
def database(code_index_database: Connection) -> Iterator[_Database]:
    """Two repositories in their own installations, committed for async sessions."""
    connection = code_index_database
    repository_a = _repository(connection, 17, "octo/indexed-a")
    repository_b = _repository(connection, 18, "octo/indexed-b")
    connection.commit()
    schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        yield _Database(
            async_sessionmaker(engine, expire_on_commit=False), repository_a, repository_b
        )
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_the_repository_reader_names_the_repository_and_its_installation(
    database: _Database,
) -> None:
    reader = SqlAlchemyIndexRepositoryReader(database.factory)

    async def scenario() -> tuple[IndexedRepository | None, ...]:
        return (
            await reader.get(database.a),
            await reader.get(database.b),
            await reader.get(uuid4()),
        )

    assert asyncio.run(scenario()) == (
        IndexedRepository(database.a, "github", 17, "octo/indexed-a"),
        IndexedRepository(database.b, "github", 18, "octo/indexed-b"),
        None,
    )
