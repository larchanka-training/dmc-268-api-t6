"""Shared Bitbucket connection PostgreSQL fixture and Alembic config."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.schema import CreateSchema, DropSchema


@pytest.fixture
def bitbucket_connection_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL connection tests")
    schema = f"test_bitbucket_connection_{uuid4().hex}"
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


def _config(connection: Connection) -> Config:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    return config
