"""Shared Bitbucket connection PostgreSQL fixture and Alembic config."""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command


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


@dataclass(frozen=True)
class PublicConnectionPageSeed:
    workspace: UUID
    foreign_workspace: UUID
    installation: UUID


def seed_public_connection_pages(db: Connection) -> PublicConnectionPageSeed:
    """Seed 102 Bitbucket bindings plus foreign and GitHub decoys for public pages."""
    command.upgrade(_config(db), "head")
    workspace, foreign_workspace = uuid4(), uuid4()
    installation, foreign_installation, github_installation = uuid4(), uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 7)"),
        [
            {"id": workspace, "name": "main"},
            {"id": foreign_workspace, "name": "foreign"},
        ],
    )
    db.execute(
        text("INSERT INTO github_user_profiles (id, login, name) VALUES (42, 'alice', 'Alice')")
    )
    db.execute(
        text(
            "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
            "VALUES (42, :workspace_id)"
        ),
        [{"workspace_id": workspace}, {"workspace_id": foreign_workspace}],
    )
    db.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace_id, :provider, :external_id, '{}'::jsonb)"
        ),
        [
            {
                "id": installation,
                "workspace_id": workspace,
                "provider": "bitbucket",
                "external_id": 360,
            },
            {
                "id": foreign_installation,
                "workspace_id": foreign_workspace,
                "provider": "bitbucket",
                "external_id": 361,
            },
            {
                "id": github_installation,
                "workspace_id": workspace,
                "provider": "github",
                "external_id": 362,
            },
        ],
    )
    db.execute(
        text(
            "INSERT INTO bitbucket_installation_identities "
            "(provider_installation_id, workspace_id, bitbucket_workspace_uuid) "
            "VALUES (:id, :workspace_id, :remote_uuid)"
        ),
        [
            {"id": installation, "workspace_id": workspace, "remote_uuid": uuid4()},
            {"id": foreign_installation, "workspace_id": foreign_workspace, "remote_uuid": uuid4()},
        ],
    )
    db.execute(
        text(
            "INSERT INTO bitbucket_connections "
            "(provider_installation_id, workspace_id, token_key_id, token_ciphertext) "
            "VALUES (:id, :workspace_id, 'test', :ciphertext)"
        ),
        [
            {"id": installation, "workspace_id": workspace, "ciphertext": b"test-cipher"},
            {
                "id": foreign_installation,
                "workspace_id": foreign_workspace,
                "ciphertext": b"test-cipher",
            },
        ],
    )
    db.execute(
        text("""
            INSERT INTO repositories
                (id, provider_installation_id, workspace_id, external_id,
                 full_name, default_branch, web_url)
            SELECT md5('ac3-' || number::text)::uuid, :installation, :workspace,
                   number + 1, 'team/repo' || lpad(number::text, 3, '0'), 'main',
                   'https://bitbucket.org/team/repo' || lpad(number::text, 3, '0')
            FROM generate_series(0, 101) AS number
        """),
        {"installation": installation, "workspace": workspace},
    )
    db.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, workspace_id, external_id, "
            "full_name, default_branch, web_url) VALUES "
            "(:id, :installation, :workspace, 1, :name, 'main', :url)"
        ),
        [
            {
                "id": uuid4(),
                "installation": foreign_installation,
                "workspace": foreign_workspace,
                "name": "team/repo001",
                "url": "https://bitbucket.org/team/repo001",
            },
            {
                "id": uuid4(),
                "installation": github_installation,
                "workspace": workspace,
                "name": "team/repo050a",
                "url": "https://github.com/team/repo050a",
            },
        ],
    )
    db.execute(
        text("""
            INSERT INTO bitbucket_repository_identities
                (repository_id, provider_installation_id, workspace_id,
                 repository_external_id, repository_uuid)
            SELECT id, provider_installation_id, workspace_id, external_id,
                   md5('remote-' || id::text)::uuid
            FROM repositories
            WHERE provider_installation_id IN (:installation, :foreign_installation)
        """),
        {"installation": installation, "foreign_installation": foreign_installation},
    )
    db.execute(
        text("""
            INSERT INTO bitbucket_repository_webhook_secrets (repository_id)
            SELECT id FROM repositories
            WHERE provider_installation_id IN (:installation, :foreign_installation)
        """),
        {"installation": installation, "foreign_installation": foreign_installation},
    )
    db.commit()
    return PublicConnectionPageSeed(workspace, foreign_workspace, installation)
