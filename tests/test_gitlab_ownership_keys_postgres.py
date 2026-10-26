"""Ownership FK targets are additive and preserve deployed GitHub schema data."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import uuid4

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import Connection, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.db_metadata import metadata

_WORKSPACE = "00000000-0000-0000-0000-000000000001"
_OTHER_WORKSPACE = "00000000-0000-0000-0000-000000000002"
_INSTALLATION = "00000000-0000-0000-0000-000000000003"
_OTHER_INSTALLATION = "00000000-0000-0000-0000-000000000004"
_REPOSITORY = "00000000-0000-0000-0000-000000000005"


@pytest.fixture
def ownership_schema() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_gitlab_ownership_{uuid4().hex}"
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


def _seed(connection: Connection) -> None:
    connection.execute(
        text(
            "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES "
            "(:workspace, 'legacy', 7), (:other_workspace, 'other', 9)"
        ),
        {"workspace": _WORKSPACE, "other_workspace": _OTHER_WORKSPACE},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) VALUES "
            "(:installation, :workspace, 'github', 17, CAST(:metadata AS jsonb)), "
            "(:other_installation, :other_workspace, 'gitlab', 17, '{}')"
        ),
        {
            "installation": _INSTALLATION,
            "workspace": _WORKSPACE,
            "metadata": '{"account_login":"octo"}',
            "other_installation": _OTHER_INSTALLATION,
            "other_workspace": _OTHER_WORKSPACE,
        },
    )
    connection.execute(
        text(
            "INSERT INTO repositories (id, provider_installation_id, external_id, full_name, "
            "default_branch, web_url, enabled, installation_available, branch_filter) VALUES "
            "(:repository, :installation, 42, 'octo/repo', 'release', "
            "'https://github.com/octo/repo', false, false, ARRAY['release/*'])"
        ),
        {"repository": _REPOSITORY, "installation": _INSTALLATION},
    )
    connection.commit()


@pytest.mark.integration
def test_installation_ownership_tuple_is_a_referenceable_tenant_key(
    ownership_schema: Connection,
) -> None:
    connection = ownership_schema
    command.upgrade(_config(connection), "head")
    _seed(connection)
    connection.execute(
        text(
            "CREATE TABLE installation_owner_probe (installation_id UUID, workspace_id UUID, "
            "provider VARCHAR(50), FOREIGN KEY (installation_id, workspace_id, provider) "
            "REFERENCES provider_installations (id, workspace_id, provider))"
        )
    )
    insert = text(
        "INSERT INTO installation_owner_probe VALUES (:installation, :workspace, :provider)"
    )
    connection.execute(
        insert,
        {
            "installation": _INSTALLATION,
            "workspace": _WORKSPACE,
            "provider": "github",
        },
    )
    for installation, workspace, provider in (
        (_INSTALLATION, _OTHER_WORKSPACE, "github"),
        (_INSTALLATION, _WORKSPACE, "gitlab"),
        (str(uuid4()), _WORKSPACE, "github"),
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert,
                {
                    "installation": installation,
                    "workspace": workspace,
                    "provider": provider,
                },
            )
    for assignment in ("workspace_id=:other_workspace", "provider='other'"):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE provider_installations SET {assignment} WHERE id=:installation"),
                {"installation": _INSTALLATION, "other_workspace": _OTHER_WORKSPACE},
            )


@pytest.mark.integration
def test_repository_ownership_tuple_is_a_referenceable_project_key(
    ownership_schema: Connection,
) -> None:
    connection = ownership_schema
    command.upgrade(_config(connection), "head")
    _seed(connection)
    connection.execute(
        text(
            "CREATE TABLE repository_owner_probe (repository_id UUID, installation_id UUID, "
            "project_id BIGINT, FOREIGN KEY (repository_id, installation_id, project_id) "
            "REFERENCES repositories (id, provider_installation_id, external_id))"
        )
    )
    insert = text(
        "INSERT INTO repository_owner_probe VALUES (:repository, :installation, :project)"
    )
    connection.execute(
        insert,
        {
            "repository": _REPOSITORY,
            "installation": _INSTALLATION,
            "project": 42,
        },
    )
    for repository, installation, project in (
        (_REPOSITORY, _OTHER_INSTALLATION, 42),
        (_REPOSITORY, _INSTALLATION, 43),
        (str(uuid4()), _INSTALLATION, 42),
    ):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                insert,
                {
                    "repository": repository,
                    "installation": installation,
                    "project": project,
                },
            )
    for assignment in ("provider_installation_id=:other_installation", "external_id=43"):
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(f"UPDATE repositories SET {assignment} WHERE id=:repository"),
                {"repository": _REPOSITORY, "other_installation": _OTHER_INSTALLATION},
            )


def _legacy_rows(connection: Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [
            tuple(row) for row in connection.execute(text(f"SELECT * FROM {table} ORDER BY id"))
        ]
        for table in ("workspaces", "provider_installations", "repositories")
    }


@pytest.mark.integration
def test_ownership_key_migration_preserves_legacy_rows_keys_and_metadata(
    ownership_schema: Connection,
) -> None:
    connection = ownership_schema
    config = _config(connection)
    command.upgrade(config, "20261017_0034")
    _seed(connection)
    legacy = _legacy_rows(connection)
    connection.commit()

    for _ in range(2):
        command.upgrade(config, "20261026_0041")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261026_0041"
        installation_keys = {
            key["name"]: key["column_names"]
            for key in inspect(connection).get_unique_constraints("provider_installations")
        }
        repository_keys = {
            key["name"]: key["column_names"]
            for key in inspect(connection).get_unique_constraints("repositories")
        }
        assert installation_keys["uq_provider_installations_owner"] == [
            "id",
            "workspace_id",
            "provider",
        ]
        assert repository_keys["uq_repositories_owner"] == [
            "id",
            "provider_installation_id",
            "external_id",
        ]
        assert ["provider", "external_id"] in installation_keys.values()
        assert ["provider_installation_id", "external_id"] in repository_keys.values()
        assert _legacy_rows(connection) == legacy
        # Natural IDs remain unique independently of the newly referenceable UUID tuples.
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text("UPDATE provider_installations SET provider='github' WHERE id=:id"),
                {"id": _OTHER_INSTALLATION},
            )
        with pytest.raises(IntegrityError), connection.begin_nested():
            connection.execute(
                text(
                    "INSERT INTO repositories (id, provider_installation_id, external_id, "
                    "full_name, default_branch, web_url) VALUES (:id, :installation, 42, "
                    "'octo/duplicate', 'main', 'https://github.com/octo/duplicate')"
                ),
                {"id": str(uuid4()), "installation": _INSTALLATION},
            )
        connection.commit()

        command.downgrade(config, "20261017_0034")
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "20261017_0034"
        assert "uq_provider_installations_owner" not in {
            key["name"]
            for key in inspect(connection).get_unique_constraints("provider_installations")
        }
        assert "uq_repositories_owner" not in {
            key["name"] for key in inspect(connection).get_unique_constraints("repositories")
        }
        assert _legacy_rows(connection) == legacy
        connection.commit()

    command.upgrade(config, "head")
    assert _legacy_rows(connection) == legacy
    context = MigrationContext.configure(
        connection, opts={"compare_type": True, "compare_server_default": True}
    )
    assert compare_metadata(context, metadata) == []
