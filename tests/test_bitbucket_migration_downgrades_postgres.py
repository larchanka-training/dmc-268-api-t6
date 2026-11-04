"""Bitbucket migration downgrades preserve only objects from earlier revisions."""

from __future__ import annotations

import pytest
from alembic.config import Config
from sqlalchemy import Connection, inspect

from alembic import command

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_bitbucket_migrations_downgrade_and_reupgrade(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    config = Config("alembic.ini")
    config.attributes["connection"] = db
    command.upgrade(config, "head")
    assert "bitbucket_raw_checkpoint_blobs" in inspect(db).get_table_names()
    assert "raw_checkpoint_bytes" in {
        column["name"] for column in inspect(db).get_columns("code_index_states")
    }

    command.downgrade(config, "20261103_0057")
    assert "bitbucket_raw_checkpoint_blobs" not in inspect(db).get_table_names()
    assert "raw_checkpoint_bytes" not in {
        column["name"] for column in inspect(db).get_columns("code_index_states")
    }
    assert "bitbucket_published_manifests" in inspect(db).get_table_names()
    assert "bitbucket_tree_checkpoint_pages" in inspect(db).get_table_names()
    assert "workspace_id" in {column["name"] for column in inspect(db).get_columns("repositories")}

    command.downgrade(config, "20261103_0056")
    assert "bitbucket_published_manifests" not in inspect(db).get_table_names()
    assert "bitbucket_tree_checkpoint_pages" in inspect(db).get_table_names()

    command.downgrade(config, "20261103_0054")
    assert "bitbucket_tree_checkpoint_pages" not in inspect(db).get_table_names()
    assert "workspace_id" in {column["name"] for column in inspect(db).get_columns("repositories")}

    command.downgrade(config, "20261027_0052")
    repository = inspect(db)
    assert "workspace_id" not in {
        column["name"] for column in repository.get_columns("repositories")
    }
    assert {
        "ix_repositories_available_installation_name_id",
        "ix_repositories_available_workspace_name_id",
    }.isdisjoint({index["name"] for index in repository.get_indexes("repositories")})

    command.upgrade(config, "head")
    tables = inspect(db).get_table_names()
    assert "bitbucket_published_manifests" in tables
    assert "bitbucket_tree_checkpoint_pages" in tables
    assert "bitbucket_raw_checkpoint_blobs" in tables
    assert "raw_checkpoint_bytes" in {
        column["name"] for column in inspect(db).get_columns("code_index_states")
    }
    assert "workspace_id" in {column["name"] for column in inspect(db).get_columns("repositories")}
