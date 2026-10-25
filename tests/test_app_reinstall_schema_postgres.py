"""Migration evidence for verified App-account identity and lifecycle foundations."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command


@pytest.fixture
def reinstall_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_reinstall_schema_{uuid4().hex}"
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


def _upgrade(connection: Connection, revision: str) -> None:
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, revision)


def _installation(connection: Connection, external_id: int) -> str:
    workspace_id, installation_id = str(uuid4()), str(uuid4())
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'legacy', 7)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id, "external": external_id},
    )
    return installation_id


def test_reinstall_schema_has_one_upgrade_head_after_deployed_policy_schema() -> None:
    graph = ScriptDirectory.from_config(Config("alembic.ini"))
    assert graph.get_heads() == ["20261025_0040"]
    assert [revision.revision for revision in graph.iterate_revisions("head", "20261012_0030")] == [
        "20261025_0040",
        "20261024_0039",
        "20261024_0038",
        "20261023_0038",
        "20261020_0037",
        "20261019_0036",
        "20261018_0035",
        "20261017_0034",
        "20261016_0033",
        "20261015_0032",
        "20261014_0031",
        "20261010_0030",
        "20261010_0029",
    ]


@pytest.mark.integration
@pytest.mark.parametrize("starting_revision", ["20261007_0028", "20261012_0030"])
def test_existing_schema_upgrade_keeps_pause_and_unknown_identity(
    reinstall_schema: Connection,
    starting_revision: str,
) -> None:
    connection = reinstall_schema
    _upgrade(connection, starting_revision)
    deleted = _installation(connection, 17)
    partial = _installation(connection, 18)
    active = _installation(connection, 19)
    for external_id, installation_id, enabled in (
        (101, deleted, False),
        (102, partial, False),
        (103, active, False),
        (104, active, True),
        (105, partial, True),
        (106, partial, True),
        (107, deleted, True),
        (108, partial, False),
    ):
        connection.execute(
            text(
                "INSERT INTO repositories "
                "(id, provider_installation_id, external_id, full_name, "
                "default_branch, web_url, enabled) "
                "VALUES (:id, :installation, :external, 'octo/repo', 'main', "
                "'https://github.com/octo/repo', :enabled)"
            ),
            {
                "id": str(uuid4()),
                "installation": installation_id,
                "external": external_id,
                "enabled": enabled,
            },
        )
    connection.execute(
        text(
            "INSERT INTO github_installation_access_revocations "
            "(provider_installation_id, repository_external_id, revoked_at) "
            "VALUES (:deleted, 0, '2026-10-07T12:00:00Z'), "
            "(:partial, 102, '2026-10-07T12:00:00Z'), "
            "(:partial, 105, '2026-10-07T12:00:00Z'), "
            "(:partial, 106, '2026-10-07T12:00:00Z'), "
            "(:partial, 108, '2026-10-07T12:00:00Z')"
        ),
        {"deleted": deleted, "partial": partial},
    )
    connection.execute(
        text(
            "INSERT INTO github_user_repository_access "
            "(github_user_id, provider_installation_id, repository_external_id, created_at) "
            "VALUES (41, :partial, 105, '2026-10-08T12:00:00Z'), "
            "(41, :partial, 108, '2026-10-08T12:00:00Z')"
        ),
        {"partial": partial},
    )
    connection.commit()

    _upgrade(connection, "head")

    rows = connection.execute(
        text(
            "SELECT external_id, enabled, installation_available "
            "FROM repositories ORDER BY external_id"
        )
    ).all()
    assert [tuple(row) for row in rows] == [
        (101, False, False),
        (102, False, False),
        (103, False, True),
        (104, True, True),
        (105, True, True),
        (106, True, True),
        (107, True, False),
        (108, False, True),
    ]
    installations = connection.execute(
        text(
            "SELECT external_id, account_external_id, deleted_at IS NOT NULL "
            "FROM provider_installations ORDER BY external_id"
        )
    ).all()
    assert [tuple(row) for row in installations] == [
        (17, None, True),
        (18, None, False),
        (19, None, False),
    ]
    assert connection.scalar(text("SELECT daily_budget_usd FROM workspaces LIMIT 1")) == 7


@pytest.mark.integration
def test_fresh_schema_enforces_positive_bigint_account_and_nullable_legacy(
    reinstall_schema: Connection,
) -> None:
    connection = reinstall_schema
    _upgrade(connection, "head")
    installation_id = _installation(connection, 17)
    assert tuple(
        connection.execute(
            text("SELECT account_external_id, deleted_at FROM provider_installations WHERE id=:id"),
            {"id": installation_id},
        ).one()
    ) == (None, None)
    for invalid in (0, -1):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE provider_installations SET account_external_id=:account WHERE id=:id"),
                {"account": invalid, "id": installation_id},
            )
    connection.execute(
        text(
            "UPDATE provider_installations SET account_external_id=9223372036854775807 WHERE id=:id"
        ),
        {"id": installation_id},
    )
    assert (
        connection.scalar(text("SELECT account_external_id FROM provider_installations"))
        == 9223372036854775807
    )


@pytest.mark.integration
def test_data_bearing_downgrade_keeps_unavailable_repositories_disabled(
    reinstall_schema: Connection,
) -> None:
    connection = reinstall_schema
    _upgrade(connection, "head")
    installation = _installation(connection, 17)
    connection.execute(
        text(
            "UPDATE provider_installations SET account_external_id=700, "
            "deleted_at=clock_timestamp()"
        )
    )
    for external_id, enabled, available in (
        (101, True, False),
        (102, False, False),
        (103, False, True),
        (104, True, True),
    ):
        connection.execute(
            text(
                "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
                "default_branch, web_url, enabled, installation_available) "
                "VALUES (:id, :installation, :external, 'octo/repo', 'main', "
                "'https://github.com/octo/repo', :enabled, :available)"
            ),
            {
                "id": str(uuid4()),
                "installation": installation,
                "external": external_id,
                "enabled": enabled,
                "available": available,
            },
        )
    connection.commit()
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.downgrade(config, "20261007_0028")
    assert [
        tuple(row)
        for row in connection.execute(
            text("SELECT external_id, enabled FROM repositories ORDER BY external_id")
        ).all()
    ] == [(101, False), (102, False), (103, False), (104, True)]
