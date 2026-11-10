"""A database per integration test, cloned from a template migrated once per process (#282).

``alembic upgrade head`` into an empty schema costs about as much as the test itself, and the
migrated state is the same for every test. So each process (a local run, a CI shard, an xdist
worker) migrates one template database on first use, and every test gets its own copy:
``CREATE DATABASE … TEMPLATE`` copies files in tens of milliseconds. The user of
``TEST_DATABASE_URL`` needs ``CREATEDB``. Tests of the migrations themselves (downgrades,
intermediate revisions) keep running alembic.
"""

from __future__ import annotations

import atexit
import contextlib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema

from alembic import command

_ROOT = Path(__file__).resolve().parents[1]
# The template keeps the migrated objects here; a clone renames the schema to the test's.
_TEMPLATE_SCHEMA = "migrated"
# The migrated template of each server URL, and every template this process created: only
# those are dropped, since parallel runs may share one server.
_templates: dict[str, str] = {}
_created: list[tuple[str, str]] = []


def _execute(database_url: str, statement: str) -> None:
    """Run a database-level statement, which PostgreSQL refuses inside a transaction."""
    engine = create_engine(database_url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(text(statement))
    finally:
        engine.dispose()


def alembic_config() -> Config:
    """The project's alembic config with absolute paths: a test may run in another directory."""
    config = Config(str(_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(_ROOT / "alembic"))
    return config


def _template(database_url: str) -> str:
    template = _templates.get(database_url)
    if template is not None:
        return template
    template = f"tmpl_{uuid4().hex}"
    _execute(database_url, f'CREATE DATABASE "{template}"')
    _created.append((database_url, template))
    config = alembic_config()
    engine = create_engine(make_url(database_url).set(database=template), poolclass=NullPool)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(_TEMPLATE_SCHEMA))
            connection.execute(text(f'SET search_path TO "{_TEMPLATE_SCHEMA}"'))
            connection.commit()
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            connection.commit()
    finally:
        # CREATE DATABASE … TEMPLATE refuses while anyone is connected to the template.
        engine.dispose()
    _templates[database_url] = template
    return template


@dataclass(frozen=True)
class MigratedDatabase:
    """The URL of a ``migrated_database`` and the schema that holds its objects."""

    url: str
    schema: str

    @contextlib.contextmanager
    def connect(self) -> Iterator[Connection]:
        """A connection with ``search_path`` on ``schema``; it rolls back on exit."""
        engine = create_engine(self.url, poolclass=NullPool)
        try:
            with engine.connect() as connection:
                connection.execute(text(f'SET search_path TO "{self.schema}"'))
                connection.commit()
                try:
                    yield connection
                finally:
                    connection.rollback()
        finally:
            engine.dispose()


@contextlib.contextmanager
def migrated_database(database_url: str, schema: str) -> Iterator[str]:
    """A new database at alembic head with its objects in ``schema``; yields its URL.

    The URL keeps the server, credentials and query of ``database_url``. On exit the database
    is dropped, with any connection still open to it.
    """
    template = _template(database_url)
    clone = make_url(database_url).set(database=f"test_{uuid4().hex}")
    _execute(database_url, f'CREATE DATABASE "{clone.database}" TEMPLATE "{template}"')
    try:
        engine = create_engine(clone, poolclass=NullPool)
        try:
            with engine.begin() as connection:
                connection.execute(text(f'ALTER SCHEMA "{_TEMPLATE_SCHEMA}" RENAME TO "{schema}"'))
        finally:
            engine.dispose()
        yield clone.render_as_string(hide_password=False)
    finally:
        _execute(database_url, f'DROP DATABASE IF EXISTS "{clone.database}" WITH (FORCE)')


@contextlib.contextmanager
def migrated_connection(database_url: str, schema: str) -> Iterator[Connection]:
    """A connection to a ``migrated_database`` with ``search_path`` on ``schema``.

    ``connection.engine.url`` is the new database's URL; the connection rolls back on exit.
    """
    with (
        migrated_database(database_url, schema) as url,
        MigratedDatabase(url, schema).connect() as connection,
    ):
        yield connection


@atexit.register
def _drop_templates() -> None:
    for database_url, template in _created:
        _execute(database_url, f'DROP DATABASE IF EXISTS "{template}" WITH (FORCE)')
