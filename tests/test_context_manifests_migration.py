"""The ``context_manifests`` table migrates up, down and up again (#130 F1-6)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from tests.portal_postgres import RUN_DONE, _seed
from tests.test_context_manifests_postgres import INSERT_POLICY_SNAPSHOT, policy_snapshot_values

SNAPSHOT_ID = UUID(int=0x5E)


@pytest.fixture
def migration_schema() -> Iterator[Connection]:
    url = os.environ.get("TEST_DATABASE_URL")
    if url is None:
        pytest.skip("set TEST_DATABASE_URL to test the context manifest migration")
    schema = f"test_context_manifests_{uuid4().hex}"
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


def _insert(
    connection: Connection,
    *,
    stage: str = "review",
    recovery: str = "none",
    snapshot: UUID | None = SNAPSHOT_ID,
    without: str | None = None,
) -> None:
    values: dict[str, object] = {
        "rendered_messages_sha256": "1" * 64,
        "prompt_checksum": "2" * 64,
        "model_profile_version": "3" * 64,
    }
    if without is not None:
        values[without] = None
    connection.execute(
        text(
            "INSERT INTO context_manifests (id, run_id, attempt, call_id, stage, recovery_kind, "
            "policy_snapshot_id, digest, rendered_messages_sha256, prompt_checksum, "
            "model_profile_version, block_count, input_tokens, blocks) VALUES (:id, :run, 1, "
            ":call, :stage, :recovery, :snapshot, :digest, :rendered_messages_sha256, "
            ":prompt_checksum, :model_profile_version, 0, 0, '[]')"
        ),
        {
            "id": uuid4(),
            "run": RUN_DONE,
            "call": uuid4(),
            "stage": stage,
            "recovery": recovery,
            "snapshot": snapshot,
            "digest": "0" * 64,
            **values,
        },
    )


@pytest.mark.integration
def test_context_manifest_migration_creates_checks_and_reverts(
    migration_schema: Connection,
) -> None:
    connection = migration_schema
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20261024_0039")
    _seed(connection)
    connection.execute(text(INSERT_POLICY_SNAPSHOT), policy_snapshot_values(RUN_DONE, SNAPSHOT_ID))
    connection.commit()

    for _ in range(2):
        command.upgrade(config, "20261025_0040")
        assert "context_manifests" in inspect(connection).get_table_names()
        assert {
            index["name"] for index in inspect(connection).get_indexes("context_manifests")
        } >= {"ix_context_manifests_run_attempt"}
        _insert(connection)
        row = connection.execute(
            text("SELECT omissions, schema_version, created_at FROM context_manifests")
        ).one()
        assert (row[0], row[1]) == ([], 1) and row[2] is not None
        with pytest.raises(IntegrityError), connection.begin_nested():
            _insert(connection, stage="conventions_v2")
        with pytest.raises(IntegrityError), connection.begin_nested():
            _insert(connection, recovery="retry_later")
        # Every manifest names a stored snapshot (CVS §8.3) and the CVS §5.1 identities.
        for snapshot in (None, uuid4()):
            with pytest.raises(IntegrityError), connection.begin_nested():
                _insert(connection, snapshot=snapshot)
        for column in ("rendered_messages_sha256", "prompt_checksum", "model_profile_version"):
            with pytest.raises(IntegrityError), connection.begin_nested():
                _insert(connection, without=column)
        for stage in ("conventions", "verification"):
            _insert(connection, stage=stage)
        for recovery in ("retry", "repair", "fallback"):
            _insert(connection, recovery=recovery)
        # The manifests of a run and of its snapshot go with them.
        foreign_keys = inspect(connection).get_foreign_keys("context_manifests")
        assert sorted(
            (
                key["referred_table"],
                tuple(key["constrained_columns"]),
                tuple(key["referred_columns"]),
                key["options"].get("ondelete"),
            )
            for key in foreign_keys
        ) == [
            ("run_policy_snapshots", ("policy_snapshot_id",), ("snapshot_id",), "CASCADE"),
            ("runs", ("run_id",), ("id",), "CASCADE"),
        ]
        assert {
            constraint["column_names"][0]
            for constraint in inspect(connection).get_unique_constraints("context_manifests")
        } == {"call_id"}
        connection.commit()
        command.downgrade(config, "20261024_0039")
        assert "context_manifests" not in inspect(connection).get_table_names()
