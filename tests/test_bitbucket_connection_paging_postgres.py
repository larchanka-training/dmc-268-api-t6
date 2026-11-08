"""Public Bitbucket pages read only active bindings in the selected workspace."""

from __future__ import annotations

import asyncio
import os
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import get_bitbucket_management
from app.bootstrap.portal_auth import get_auth_scope
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketCredentialCodec,
    BitbucketRepositoryProvider,
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from tests.bitbucket_connection_postgres_support import _config

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_public_connection_pages_are_bounded_and_workspace_scoped(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
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
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()

    database_url = make_url(os.environ["TEST_DATABASE_URL"]).set(
        drivername="postgresql+psycopg_async"
    )
    engine = create_async_engine(database_url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    manager = ManageBitbucketConnection(
        uow_factory=lambda: SqlAlchemyBitbucketManagementUnitOfWork(factory),
        cipher=cast(BitbucketCredentialCodec, object()),
        provider=cast(BitbucketRepositoryProvider, object()),
        rule_sets={},
        webhook_base_url="https://portal.test",
    )
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (workspace, foreign_workspace))
    app.dependency_overrides[get_bitbucket_management] = lambda: manager
    try:
        client = TestClient(app)
        url = f"/api/workspaces/{workspace}/bitbucket/connections"
        first = client.get(url)
        assert first.status_code == 200
        assert [row["repositorySlug"] for row in first.json()["connections"]] == [
            f"repo{index:03d}" for index in range(100)
        ]
        cursor = first.json()["nextCursor"]
        assert isinstance(cursor, str) and cursor

        second = client.get(url, params={"cursor": cursor})
        assert second.status_code == 200
        assert [row["repositorySlug"] for row in second.json()["connections"]] == [
            "repo100",
            "repo101",
        ]
        assert second.json()["nextCursor"] is None

        foreign = client.get(
            f"/api/workspaces/{foreign_workspace}/bitbucket/connections",
            params={"cursor": cursor},
        )
        assert (foreign.status_code, foreign.json()) == (400, {"detail": "invalid_cursor"})
        not_member = client.get(f"/api/workspaces/{UUID(int=999)}/bitbucket/connections")
        assert not_member.status_code == 404

        db.execute(
            text(
                "UPDATE repositories SET installation_available=false "
                "WHERE provider_installation_id=:installation "
                "AND full_name IN ('team/repo100', 'team/repo101')"
            ),
            {"installation": installation},
        )
        db.commit()
        empty = client.get(url, params={"cursor": cursor})
        assert empty.status_code == 200
        assert empty.json() == {"connections": [], "nextCursor": None}
    finally:
        app.dependency_overrides.clear()
        asyncio.run(engine.dispose())
