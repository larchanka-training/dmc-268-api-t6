"""Prompt pin columns migrate without inventing pins for historical runs (#130)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from tests.portal_postgres import PROMPT, RUN_DONE, _seed

_PIN_COLUMNS = ("conventions_prompt_version_id", "verifier_prompt_version_id")


@pytest.fixture
def migration_schema() -> Iterator[Connection]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test prompt pin migration")
    schema = f"test_prompt_pins_{uuid4().hex}"
    engine = create_engine(url)
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


def _run_columns(connection: Connection) -> set[str]:
    return {column["name"] for column in inspect(connection).get_columns("runs")}


@pytest.mark.integration
def test_prompt_pin_migration_keeps_history_null_and_reverts(
    migration_schema: Connection,
) -> None:
    connection = migration_schema
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20261024_0038")
    _seed(connection)
    connection.commit()
    command.upgrade(config, "20261024_0039")
    assert connection.execute(
        text(
            "SELECT conventions_prompt_version_id, verifier_prompt_version_id "
            "FROM runs WHERE id=:run"
        ),
        {"run": RUN_DONE},
    ).one() == (None, None)
    for column in _PIN_COLUMNS:
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE runs SET {column}=:prompt WHERE id=:run"),
                {"prompt": uuid4(), "run": RUN_DONE},
            )
        # Each column pins on its own: one may stay NULL while the other is set.
        connection.execute(
            text(f"UPDATE runs SET {column}=:prompt WHERE id=:run"),
            {"prompt": PROMPT, "run": RUN_DONE},
        )
        connection.execute(text(f"UPDATE runs SET {column}=NULL WHERE id=:run"), {"run": RUN_DONE})
    connection.commit()
    command.downgrade(config, "20261024_0038")
    assert _run_columns(connection).isdisjoint(_PIN_COLUMNS)
    connection.commit()
    command.upgrade(config, "20261024_0039")
    assert set(_PIN_COLUMNS) <= _run_columns(connection)
