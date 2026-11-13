"""Opt-in PostgreSQL proof of agent token persistence and live grant scope."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.agent_tokens import AgentTokens, RepositoryNotAvailable
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.agent_tokens import SqlAlchemyAgentTokenUnitOfWork
from app.modules.auth.infrastructure.models import AuthAgentToken
from app.modules.repositories.infrastructure.models import Repository
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsStore,
)
from tests.portal_postgres import INSTALL_A, REPO_A, REPO_B, RUN_B, RUN_DONE, WS_A, Env, api

pytest_plugins = ("tests.agent_postgres",)


@pytest.mark.integration
def test_agent_token_persists_only_hash_and_limits_repository_queries(agent_schema: Env) -> None:
    engine = agent_schema.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def exercise() -> None:
        tokens = AgentTokens(lambda: SqlAlchemyAgentTokenUnitOfWork(factory))
        issued = await tokens.issue(42, "octo/a")
        with pytest.raises(RepositoryNotAvailable):
            await tokens.issue(42, "octo/unknown")
        async with factory() as session:
            stored = await session.scalar(select(AuthAgentToken))
            assert stored is not None
            assert stored.repository_id == REPO_A
            assert issued.secret not in str(stored.__dict__)
            assert len(stored.token_hash) == 64
        scope = await tokens.resolve(issued.secret)
        assert scope is not None
        listed = await tokens.list_active(AuthScope(42, ()))
        assert [(item.id, item.repository) for item in listed] == [(issued.id, "octo/a")]
        assert await tokens.list_active(AuthScope(43, ())) == []
        async with factory() as session:
            repos = await SqlAlchemyRepositorySettingsStore(session, scope).list_repositories()
            assert [item.id for item in repos] == [REPO_A]
        async with factory.begin() as session:
            await session.execute(
                update(Repository)
                .where(Repository.id == REPO_A)
                .values(installation_available=False)
            )
        async with factory() as session:
            repos = await SqlAlchemyRepositorySettingsStore(session, scope).list_repositories()
            assert repos == []
        assert await tokens.revoke(AuthScope(42, ()), issued.id)
        assert await tokens.list_active(AuthScope(42, ())) == []
        assert await tokens.resolve(issued.secret) is None

    try:
        asyncio.run(exercise())
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_agent_token_cannot_access_another_repository_in_the_same_installation(
    agent_schema: Env,
) -> None:
    engine = agent_schema.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def seed_and_issue() -> AuthScope:
        async with factory.begin() as session:
            await session.execute(
                update(Repository)
                .where(Repository.id == REPO_B)
                .values(provider_installation_id=INSTALL_A)
            )
            await session.execute(
                text(
                    "INSERT INTO github_user_repository_access "
                    "(github_user_id, provider_installation_id, repository_external_id) "
                    "VALUES (42, :installation, 102)"
                ),
                {"installation": INSTALL_A},
            )
            await session.execute(
                text(
                    "UPDATE runs SET origin_provider_installation_id = :installation "
                    "WHERE id = :run"
                ),
                {"installation": INSTALL_A, "run": RUN_B},
            )
        tokens = AgentTokens(lambda: SqlAlchemyAgentTokenUnitOfWork(factory))
        issued = await tokens.issue(42, "octo/a")
        scope = await tokens.resolve(issued.secret)
        assert scope is not None
        return scope

    try:
        scope = asyncio.run(seed_and_issue())
        assert scope.workspace_ids == (WS_A,)
        with api(agent_schema, AuthScope(42, (WS_A,))) as (client, _):
            assert {item["id"] for item in client.get("/api/repos").json()} == {
                str(REPO_A),
                str(REPO_B),
            }
            assert client.get(f"/api/repos/{REPO_B}/pulls").status_code == 200
            assert client.get(f"/api/runs/{RUN_B}").status_code == 200

        with api(agent_schema, scope) as (client, _):
            assert [item["id"] for item in client.get("/api/repos").json()] == [str(REPO_A)]
            assert client.get(f"/api/repos/{REPO_A}/pulls").status_code == 200
            assert client.get(f"/api/runs/{RUN_DONE}").status_code == 200
            assert client.get(f"/api/repos/{REPO_B}").status_code == 404
            assert client.patch(f"/api/repos/{REPO_B}", json={"enabled": False}).status_code == 404
            assert client.get(f"/api/repos/{REPO_B}/pulls").status_code == 404
            assert client.get(f"/api/runs/{RUN_B}").status_code == 404
            assert str(RUN_B) not in {
                item["id"] for item in client.get("/api/runs").json()["items"]
            }
    finally:
        asyncio.run(engine.dispose())
