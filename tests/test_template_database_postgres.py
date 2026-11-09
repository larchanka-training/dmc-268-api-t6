"""The database of an integration test is a clone of a template migrated to head (#282)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool

from app.bootstrap.db_metadata import metadata
from tests.template_database import migrated_database


@pytest.fixture
def database_url() -> Iterator[str]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    yield url


def _schema() -> str:
    return f"test_template_{uuid4().hex}"


@pytest.mark.integration
def test_a_clone_matches_alembic_upgrade_head_in_the_schema_of_the_test(
    database_url: str,
) -> None:
    schema = _schema()
    head = ScriptDirectory.from_config(Config("alembic.ini")).get_current_head()
    with migrated_database(database_url, schema) as clone_url:
        engine = create_engine(
            clone_url, connect_args={"options": f"-csearch_path={schema}"}, poolclass=NullPool
        )
        try:
            with engine.connect() as connection:
                context = MigrationContext.configure(
                    connection, opts={"compare_type": True, "compare_server_default": True}
                )
                assert compare_metadata(context, metadata) == []
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == head
                schemas = set(
                    connection.scalars(
                        text(
                            "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg\\_%' "
                            "AND nspname <> 'information_schema'"
                        )
                    )
                )
                assert schemas == {schema, "public"}
                enum_schemas = set(
                    connection.scalars(
                        text(
                            "SELECT DISTINCT n.nspname FROM pg_type t "
                            "JOIN pg_namespace n ON n.oid = t.typnamespace WHERE t.typtype = 'e'"
                        )
                    )
                )
                assert enum_schemas == {schema}
        finally:
            engine.dispose()


@pytest.mark.integration
def test_clones_are_independent_and_dropped_on_exit(database_url: str) -> None:
    first_schema, second_schema = _schema(), _schema()
    with (
        migrated_database(database_url, first_schema) as first_url,
        migrated_database(database_url, second_schema) as second_url,
    ):
        assert make_url(first_url).database != make_url(second_url).database
        first = create_engine(
            first_url, connect_args={"options": f"-csearch_path={first_schema}"}, poolclass=NullPool
        )
        second = create_engine(
            second_url,
            connect_args={"options": f"-csearch_path={second_schema}"},
            poolclass=NullPool,
        )
        try:
            with first.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'W', 0)"
                    ),
                    {"id": uuid4()},
                )
            with second.connect() as connection:
                assert connection.scalar(text("SELECT count(*) FROM workspaces")) == 0
            # A connection the test leaves open does not keep its database alive.
            leaked = first.connect()
        finally:
            second.dispose()
    names = {make_url(first_url).database, make_url(second_url).database}
    leaked.invalidate()
    first.dispose()
    admin = create_engine(database_url, poolclass=NullPool)
    try:
        with admin.connect() as connection:
            left = set(
                connection.scalars(
                    text("SELECT datname FROM pg_database WHERE datname = ANY(:names)"),
                    {"names": list(names)},
                )
            )
    finally:
        admin.dispose()
    assert left == set()
