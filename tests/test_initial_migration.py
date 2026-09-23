"""Opt-in PostgreSQL checks, isolated in a unique schema per test.

TEST_DATABASE_URL must target a test database whose user can create schemas.
No pre-existing schema is downgraded or removed.
"""

import asyncio
import os
from collections.abc import Iterator
from decimal import Decimal
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, inspect, select, text
from sqlalchemy.dialects.postgresql.base import PGInspector
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.db_metadata import metadata
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.workspaces.infrastructure.models import Workspace


@pytest.fixture
def isolated_database() -> Iterator[tuple[Connection, str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_backend_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                yield connection, database_url, schema
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def migration_config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config


@pytest.mark.integration
def test_initial_migration_round_trip(
    isolated_database: tuple[Connection, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection, _, _ = isolated_database
    # An injected test connection must win over application DATABASE_URL.
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://invalid:invalid@127.0.0.1:1/invalid")
    config = migration_config(connection)
    for _ in range(2):
        command.upgrade(config, "head")
        inspector = inspect(connection)
        assert set(metadata.tables) == set(inspector.get_table_names()) - {"alembic_version"}
        assert {"category", "suggestion"} <= {
            column["name"] for column in inspector.get_columns("findings")
        }
        assert "uq_runs_one_active_per_code_change" in {
            index["name"] for index in inspector.get_indexes("runs")
        }
        assert "ix_usage_events_run_created" in {
            index["name"] for index in inspector.get_indexes("usage_events")
        }
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, metadata) == []
        connection.rollback()
        command.downgrade(config, "base")
        assert set(inspect(connection).get_table_names()) <= {"alembic_version"}
        pg_inspector = inspect(connection)
        assert isinstance(pg_inspector, PGInspector)
        assert (
            pg_inspector.get_enums(schema=connection.scalar(text("SELECT current_schema()"))) == []
        )
        connection.rollback()


@pytest.mark.integration
def test_unit_of_work_commit_and_rollback(
    isolated_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_database
    command.upgrade(migration_config(connection), "head")

    async def exercise_transactions() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with SqlAlchemyUnitOfWork(factory) as uow:
                uow.session.add(Workspace(name="committed", daily_budget_usd=Decimal("1")))
                await uow.commit()
            async with SqlAlchemyUnitOfWork(factory) as uow:
                uow.session.add(Workspace(name="not committed", daily_budget_usd=Decimal("1")))
                await uow.session.flush()
            with pytest.raises(RuntimeError, match="use case failed"):
                async with SqlAlchemyUnitOfWork(factory) as uow:
                    uow.session.add(Workspace(name="failed", daily_budget_usd=Decimal("1")))
                    await uow.session.flush()
                    raise RuntimeError("use case failed")
        finally:
            await engine.dispose()

    asyncio.run(exercise_transactions())
    assert connection.scalars(select(Workspace.name)).all() == ["committed"]
