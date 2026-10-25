"""Publication pins migrate without inventing settings for historical runs."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from tests.portal_postgres import RUN_DONE, _seed


@pytest.fixture
def migration_schema() -> Iterator[Connection]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test publication pin migration")
    schema = f"test_publication_pins_{uuid4().hex}"
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


@pytest.mark.integration
def test_publication_pin_migration_preserves_history_and_repository_enum(
    migration_schema: Connection,
) -> None:
    connection = migration_schema
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20261023_0038")
    _seed(connection)
    connection.execute(
        text("""
        INSERT INTO run_policy_snapshots (
            run_id, snapshot_id, repository_id, schema_version, policy_base_sha,
            ignore_patterns, ignore_digest, parser_profile, parser_version, matcher_version,
            limits_version, builtin_exclusions_version, priority_matcher_version,
            service_defaults_rule_version_id, defaults_checksum, prompt_bindings,
            execution_profile, policy_digest
        ) SELECT id, gen_random_uuid(),
            (SELECT repository_id FROM code_changes WHERE id=code_change_id),
            1, base_sha, '[]'::jsonb, repeat('0', 64), 'p', 'p', 'm', 'l', 'b', 'p',
            rule_version_id, repeat('1', 64), '{}'::jsonb, '{}'::jsonb, repeat('2', 64)
          FROM runs WHERE id=:run
    """),
        {"run": RUN_DONE},
    )
    connection.commit()
    command.upgrade(config, "20261024_0038")
    for table in ("runs", "run_policy_snapshots"):
        assert connection.execute(
            text(
                f"SELECT severity_threshold, max_comments FROM {table} WHERE "
                + ("id" if table == "runs" else "run_id")
                + "=:run"
            ),
            {"run": RUN_DONE},
        ).one() == (None, None)
        for values in (
            "severity_threshold='all', max_comments=NULL",
            "severity_threshold=NULL, max_comments=2",
            "severity_threshold='warning', max_comments=0",
            "severity_threshold='critical', max_comments=-1",
        ):
            with pytest.raises(IntegrityError), connection.begin_nested():
                connection.execute(text(f"UPDATE {table} SET {values}"))
        connection.execute(text(f"UPDATE {table} SET severity_threshold='warning', max_comments=1"))
    connection.commit()
    command.downgrade(config, "20261023_0038")
    # Removing the publication pins must retain main's review-trigger migration.
    assert (
        connection.scalar(
            text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema=current_schema() AND table_name='code_index_states' "
                "AND column_name IN ('review_full_requested_at', 'review_full_requested_sha')"
            )
        )
        == 2
    )
    # Repository owns the enum and its settings survive removal of the run columns.
    assert connection.execute(
        text("SELECT severity_threshold::text, max_comments FROM repositories LIMIT 1")
    ).one() == ("all", 10)
    connection.commit()
    command.upgrade(config, "20261024_0038")
    assert connection.execute(
        text("SELECT severity_threshold, max_comments FROM runs WHERE id=:run"), {"run": RUN_DONE}
    ).one() == (None, None)
