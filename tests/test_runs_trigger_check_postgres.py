"""The runs.trigger CHECK revision keeps existing Runs and refuses unknown triggers (#174)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command

_ROWS = "SELECT id, to_jsonb(r) FROM runs r ORDER BY id"
_TRIGGERS = "SELECT id, trigger FROM runs"
_VERSION = "SELECT version_num FROM alembic_version"


@pytest.fixture
def trigger_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_runs_trigger_{uuid4().hex}"
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


def _seed_parents(connection: Connection) -> dict[str, UUID]:
    ids = {
        "workspace": uuid4(),
        "installation": uuid4(),
        "repository": uuid4(),
        "rule": uuid4(),
        "prompt": uuid4(),
        "change": uuid4(),
    }
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'legacy', 0)"),
        {"id": ids["workspace"]},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations (id, workspace_id, provider, external_id, "
            "metadata) VALUES (:id, :workspace, 'github', 17, '{}'::jsonb)"
        ),
        {"id": ids["installation"], "workspace": ids["workspace"]},
    )
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url) VALUES (:id, :installation, 101, 'octo/legacy', 'main', "
            "'https://github.test/octo/legacy')"
        ),
        {"id": ids["repository"], "installation": ids["installation"]},
    )
    connection.execute(
        text(
            "INSERT INTO rule_versions (id, repository_id, version, rules, checksum) "
            "VALUES (:id, :repository, 1, '[]'::jsonb, :checksum)"
        ),
        {"id": ids["rule"], "repository": ids["repository"], "checksum": "a" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO prompt_versions (id, key, version, content, checksum) "
            "VALUES (:id, 'review.system', 1, 'system', :checksum)"
        ),
        {"id": ids["prompt"], "checksum": "c" * 64},
    )
    connection.execute(
        text(
            "INSERT INTO code_changes (id, repository_id, external_id, external_number, title, "
            "source_branch, target_branch, base_sha, head_sha, state, web_url) VALUES (:id, "
            ":repository, 901, 1, 'PR', 'feature', 'main', :base, :head, 'open', "
            "'https://github.test/pull/1')"
        ),
        {"id": ids["change"], "repository": ids["repository"], "base": "b" * 40, "head": "h" * 40},
    )
    return ids


def _add_run(connection: Connection, parents: dict[str, UUID], trigger: str, key: str) -> UUID:
    run_id = uuid4()
    connection.execute(
        text(
            "INSERT INTO runs (id, code_change_id, origin_provider_installation_id, base_sha, "
            "head_sha, state, trigger, idempotency_key, engine, rule_version_id, "
            "prompt_version_id, available_at) VALUES (:id, :change, :installation, :base, "
            ":head, 'succeeded', :trigger, :key, 'fast', :rule, :prompt, now())"
        ),
        {
            "id": run_id,
            "change": parents["change"],
            "installation": parents["installation"],
            "base": "b" * 40,
            "head": "h" * 40,
            "trigger": trigger,
            "key": key,
            "rule": parents["rule"],
            "prompt": parents["prompt"],
        },
    )
    return run_id


@pytest.mark.integration
def test_trigger_check_round_trips_spec_rows_and_refuses_an_unknown_trigger(
    trigger_schema: Connection,
) -> None:
    connection = trigger_schema
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20261018_0035")
    parents = _seed_parents(connection)
    webhook = _add_run(connection, parents, "webhook", "1" * 64)
    rerun = _add_run(connection, parents, "rerun", "2" * 64)
    before = connection.execute(text(_ROWS)).tuples().all()
    connection.commit()

    command.upgrade(config, "20261019_0036")
    assert connection.scalar(text(_VERSION)) == "20261019_0036"
    assert dict(connection.execute(text(_TRIGGERS)).tuples().all()) == {
        webhook: "webhook",
        rerun: "rerun",
    }
    assert connection.execute(text(_ROWS)).tuples().all() == before
    connection.commit()

    command.downgrade(config, "20261018_0035")
    assert connection.scalar(text(_VERSION)) == "20261018_0035"
    assert dict(connection.execute(text(_TRIGGERS)).tuples().all()) == {
        webhook: "webhook",
        rerun: "rerun",
    }
    assert connection.execute(text(_ROWS)).tuples().all() == before
    push = _add_run(connection, parents, "push", "3" * 64)
    connection.commit()

    with pytest.raises(IntegrityError) as error:
        command.upgrade(config, "20261019_0036")
    connection.rollback()
    assert getattr(error.value.orig, "sqlstate", None) == "23514"
    name = getattr(getattr(error.value.orig, "diag", None), "constraint_name", None)
    assert name == "ck_runs_trigger"
    assert connection.scalar(text(_VERSION)) == "20261018_0035"
    assert dict(connection.execute(text(_TRIGGERS)).tuples().all()) == {
        webhook: "webhook",
        rerun: "rerun",
        push: "push",
    }
