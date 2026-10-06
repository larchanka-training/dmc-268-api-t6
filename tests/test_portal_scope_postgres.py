"""Opt-in PostgreSQL proof for claim, current grant, and repository intersection."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.main import app
from app.modules.auth.application.scope import AuthScope

WORKSPACE_A = UUID("00000000-0000-0000-0000-000000000011")
WORKSPACE_B = UUID("00000000-0000-0000-0000-000000000012")
INSTALLATION_A = UUID("00000000-0000-0000-0000-000000000021")
INSTALLATION_B = UUID("00000000-0000-0000-0000-000000000022")
REPOS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (31, 32, 33))
PULLS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (41, 42, 43))
RUNS = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (51, 52, 53))
RULES = tuple(UUID(f"00000000-0000-0000-0000-{value:012d}") for value in (61, 62, 63))
PROMPT = UUID("00000000-0000-0000-0000-000000000071")


@pytest.fixture
def portal_database() -> Iterator[tuple[str, str]]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_portal_scope_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            config = Config("alembic.ini")
            config.attributes["connection"] = connection
            command.upgrade(config, "head")
            for workspace_id, name in ((WORKSPACE_A, "Alpha"), (WORKSPACE_B, "Beta")):
                connection.execute(
                    text(
                        "INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, :name, 0)"
                    ),
                    {"id": workspace_id, "name": name},
                )
            for installation_id, workspace_id, external_id in (
                (INSTALLATION_A, WORKSPACE_A, 17),
                (INSTALLATION_B, WORKSPACE_B, 18),
            ):
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, 'github', :external, '{}'::jsonb)"
                    ),
                    {"id": installation_id, "workspace": workspace_id, "external": external_id},
                )
            for user_id, login in ((42, "alice"), (43, "bob")):
                connection.execute(
                    text(
                        "INSERT INTO github_user_profiles (id, login, name) "
                        "VALUES (:id, :login, :login)"
                    ),
                    {"id": user_id, "login": login},
                )
            for user_id, workspace_id in ((42, WORKSPACE_A), (42, WORKSPACE_B), (43, WORKSPACE_A)):
                connection.execute(
                    text(
                        "INSERT INTO github_user_workspace_access (github_user_id, workspace_id) "
                        "VALUES (:user, :workspace)"
                    ),
                    {"user": user_id, "workspace": workspace_id},
                )
            for user_id, installation_id, repository_external_id in (
                (42, INSTALLATION_A, 101),
                (42, INSTALLATION_B, 103),
                (43, INSTALLATION_A, 102),
            ):
                connection.execute(
                    text(
                        "INSERT INTO github_user_repository_access "
                        "(github_user_id, provider_installation_id, repository_external_id) "
                        "VALUES (:user, :installation, :repository)"
                    ),
                    {
                        "user": user_id,
                        "installation": installation_id,
                        "repository": repository_external_id,
                    },
                )
            connection.execute(
                text(
                    "INSERT INTO prompt_versions (id, key, version, content, checksum, is_active) "
                    "VALUES (:id, 'review.system', 1, 'system', :checksum, true)"
                ),
                {"id": PROMPT, "checksum": "c" * 64},
            )
            now = datetime.now(UTC)
            records = zip(
                REPOS,
                PULLS,
                RUNS,
                RULES,
                (INSTALLATION_A, INSTALLATION_A, INSTALLATION_B),
                (101, 102, 103),
                strict=True,
            )
            for index, record in enumerate(records, start=1):
                repo_id, pull_id, run_id, rule_id, installation_id, repo_external = record
                connection.execute(
                    text(
                        "INSERT INTO repositories "
                        "(id, provider_installation_id, external_id, full_name, "
                        "default_branch, web_url) "
                        "VALUES (:id, :installation, :external, :name, 'main', :url)"
                    ),
                    {
                        "id": repo_id,
                        "installation": installation_id,
                        "external": repo_external,
                        "name": f"octo/repo-{index}",
                        "url": f"https://github.test/octo/repo-{index}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO rule_versions "
                        "(id, repository_id, version, rules, checksum, is_active) "
                        "VALUES (:id, :repository, 1, '[]'::jsonb, :checksum, true)"
                    ),
                    {"id": rule_id, "repository": repo_id, "checksum": str(index) * 64},
                )
                connection.execute(
                    text(
                        "INSERT INTO code_changes "
                        "(id, repository_id, external_id, external_number, title, source_branch, "
                        "target_branch, base_sha, head_sha, state, web_url) "
                        "VALUES (:id, :repository, :external, :number, 'PR', 'feature', 'main', "
                        ":base, :head, 'open', :url)"
                    ),
                    {
                        "id": pull_id,
                        "repository": repo_id,
                        "external": 900 + index,
                        "number": index,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "url": f"https://github.test/octo/repo-{index}/pull/{index}",
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO runs "
                        "(id, code_change_id, base_sha, head_sha, state, trigger, idempotency_key, "
                        "engine, rule_version_id, prompt_version_id, available_at, created_at) "
                        "VALUES (:id, :pr, :base, :head, 'queued', 'manual', :key, "
                        "'fast', :rule, :prompt, :available, :created)"
                    ),
                    {
                        "id": run_id,
                        "pr": pull_id,
                        "base": "b" * 40,
                        "head": "e" * 40,
                        "key": str(index) * 64,
                        "rule": rule_id,
                        "prompt": PROMPT,
                        "available": now,
                        "created": now + timedelta(seconds={1: 1, 2: 2, 3: 0}[index]),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO run_actions "
                        "(id, run_id, index, tool, request, response, started_at, duration_ms) "
                        "VALUES (:id, :run, 0, 'test', '{}'::jsonb, CAST(:response AS jsonb), "
                        ":started, 1)"
                    ),
                    {
                        "id": uuid4(),
                        "run": run_id,
                        "response": '{"secret":true}',
                        "started": now,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO code_change_diffs "
                        "(id, run_id, code_change_id, head_sha, filename, patch, blob_sha) "
                        "VALUES (:id, :run, :pr, :head, 'src/a.py', '@@ -1 +1 @@', :blob)"
                    ),
                    {
                        "id": uuid4(),
                        "run": run_id,
                        "pr": pull_id,
                        "head": "e" * 40,
                        "blob": str(index) * 40,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO cached_file_blobs "
                        "(id, repository_id, blob_sha, content, expires_at) "
                        "VALUES (:id, :repository, :blob, 'line', :expires)"
                    ),
                    {
                        "id": uuid4(),
                        "repository": repo_id,
                        "blob": str(index) * 40,
                        "expires": now + timedelta(days=7),
                    },
                )
            connection.commit()
            yield database_url, schema
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(DropSchema(schema, cascade=True))
            connection.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_portal_routes_intersect_claim_current_membership_and_repository_grant(
    portal_database: tuple[str, str],
) -> None:
    database_url, schema = portal_database
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    app.state.reviews_api_resources = ReviewsApiResources(engine, factory)
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A,))
    try:
        client = TestClient(app)
        limited = client.get("/api/runs", params={"limit": 1})
        assert limited.status_code == 200
        assert [item["id"] for item in limited.json()["items"]] == [str(RUNS[0])]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A, WORKSPACE_B))
        first_page = client.get("/api/runs", params={"limit": 1}).json()
        second_page = client.get(
            "/api/runs", params={"limit": 1, "cursor": first_page["nextCursor"]}
        ).json()
        assert [item["id"] for item in first_page["items"]] == [str(RUNS[0])]
        assert [item["id"] for item in second_page["items"]] == [str(RUNS[2])]
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_A,))
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 200
        assert client.get(f"/api/runs/{RUNS[2]}").status_code == 404
        denied = RUNS[1]
        for path in (
            f"/api/runs/{denied}",
            f"/api/runs/{denied}/comments",
            f"/api/runs/{denied}/actions",
            f"/api/runs/{denied}/actions/0/response",
            f"/api/runs/{denied}/diff",
            f"/api/runs/{denied}/files?path=src/a.py",
        ):
            assert client.get(path).status_code == 404
        assert client.post(f"/api/runs/{denied}/cancel").status_code == 404

        async def denied_state() -> str:
            async with factory() as session:
                return str(
                    await session.scalar(
                        text("SELECT state FROM runs WHERE id = :id"), {"id": denied}
                    )
                )

        assert asyncio.run(denied_state()) == "queued"
        assert client.get("/api/auth/me").json()["workspaces"] == [
            {"id": str(WORKSPACE_A), "name": "Alpha", "installationId": 17}
        ]

        async def revoke_repo_grant() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "DELETE FROM github_user_repository_access "
                        "WHERE github_user_id = 42 AND provider_installation_id = :installation "
                        "AND repository_external_id = 101"
                    ),
                    {"installation": INSTALLATION_A},
                )

        asyncio.run(revoke_repo_grant())
        assert client.get("/api/runs").json()["items"] == []
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(43, (WORKSPACE_A,))
        assert [item["id"] for item in client.get("/api/runs").json()["items"]] == [str(RUNS[1])]
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, (WORKSPACE_B,))
        assert [item["id"] for item in client.get("/api/runs").json()["items"]] == [str(RUNS[2])]

        async def revoke_workspace_grant() -> None:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "DELETE FROM github_user_workspace_access "
                        "WHERE github_user_id = 42 AND workspace_id = :workspace"
                    ),
                    {"workspace": WORKSPACE_B},
                )

        asyncio.run(revoke_workspace_grant())
        assert client.get("/api/runs").json()["items"] == []
        assert client.get("/api/auth/me").json()["workspaces"] == []
        app.dependency_overrides[get_auth_scope] = lambda: AuthScope(42, ())
        assert client.get("/api/runs").json() == {"items": [], "nextCursor": None}
        assert client.get(f"/api/runs/{RUNS[0]}").status_code == 404
        assert client.get("/api/repos").status_code == 200
        assert client.get("/api/repos").json() == []
        assert client.get("/api/auth/me").json()["workspaces"] == []
    finally:
        app.dependency_overrides.clear()
        del app.state.reviews_api_resources
        asyncio.run(engine.dispose())
