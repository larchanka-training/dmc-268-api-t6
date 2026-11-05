"""Opt-in PostgreSQL proof of agent token persistence and live grant scope."""

from __future__ import annotations

import asyncio
import os

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.agent_tokens import AgentTokens, RepositoryNotAvailable
from app.modules.auth.infrastructure.agent_tokens import SqlAlchemyAgentTokenUnitOfWork
from app.modules.auth.infrastructure.models import AuthAgentToken
from app.modules.repositories.infrastructure.models import Repository
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsStore,
)
from tests.portal_postgres import REPO_A, portal_schema


@pytest.mark.integration
def test_agent_token_persists_only_hash_and_limits_repository_queries() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        engine = env.engine()
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
            assert await tokens.revoke(42, issued.id)
            assert await tokens.resolve(issued.secret) is None

        try:
            asyncio.run(exercise())
        finally:
            asyncio.run(engine.dispose())
