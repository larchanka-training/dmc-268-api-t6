"""The database of an integration test is a clone of a template migrated to head (#282)."""

from __future__ import annotations

from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.bootstrap.db_metadata import metadata
from tests.template_database import MigratedDatabase, alembic_config, migrated_database

pytest_plugins = ("tests.migrated_postgres",)
pytestmark = pytest.mark.integration


def _schema() -> str:
    return f"test_template_{uuid4().hex}"


def test_a_clone_matches_alembic_upgrade_head(migrated_db: MigratedDatabase) -> None:
    head = ScriptDirectory.from_config(alembic_config()).get_current_head()
    with migrated_db.connect() as connection:
        context = MigrationContext.configure(
            connection, opts={"compare_type": True, "compare_server_default": True}
        )
        assert compare_metadata(context, metadata) == []
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head


def test_a_clone_keeps_its_objects_in_the_schema_of_the_test(
    migrated_db: MigratedDatabase,
) -> None:
    with migrated_db.connect() as connection:
        schemas = set(
            connection.scalars(
                text(
                    "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                    "AND nspname <> 'information_schema'"
                )
            )
        )
        enum_schemas = set(
            connection.scalars(
                text(
                    "SELECT DISTINCT n.nspname FROM pg_type t "
                    "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typtype = 'e'"
                )
            )
        )
    assert schemas == {migrated_db.schema, "public"}
    assert enum_schemas == {migrated_db.schema}


def test_clones_are_independent(test_database_url: str) -> None:
    first_schema, second_schema = _schema(), _schema()
    with (
        migrated_database(test_database_url, first_schema) as first_url,
        migrated_database(test_database_url, second_schema) as second_url,
    ):
        assert make_url(first_url).database != make_url(second_url).database
        with MigratedDatabase(first_url, first_schema).connect() as connection:
            connection.execute(
                text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)"),
                {"id": uuid4()},
            )
            connection.commit()
        with MigratedDatabase(second_url, second_schema).connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM workspaces")) == 0


def test_a_clone_is_dropped_on_exit_with_a_connection_left_open(test_database_url: str) -> None:
    with migrated_database(test_database_url, _schema()) as url:
        engine = create_engine(url, poolclass=NullPool)
        leaked = engine.connect()
        assert leaked.scalar(text("SELECT 1")) == 1
    leaked.invalidate()
    engine.dispose()
    admin = create_engine(test_database_url, poolclass=NullPool)
    try:
        with admin.connect() as connection:
            left = connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :name"),
                {"name": make_url(url).database},
            )
    finally:
        admin.dispose()
    assert left == 0
