"""Historical run provider is pinned to its original installation."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import INSTALL_A, REPO_A, RUN_DONE, WS_A, portal_schema


@pytest.mark.integration
def test_historical_run_provider_survives_cross_provider_repository_rebind() -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")

    new_installation = UUID("00000000-0000-4000-8000-0000000000d1")
    with portal_schema(database_url, None) as env:
        engine = create_engine(
            env.database_url, connect_args={"options": f"-csearch_path={env.schema}"}
        )
        try:
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "INSERT INTO provider_installations "
                        "(id, workspace_id, provider, external_id, metadata) "
                        "VALUES (:id, :workspace, 'bitbucket', 999, '{}'::jsonb)"
                    ),
                    {"id": new_installation, "workspace": WS_A},
                )
                connection.execute(
                    text(
                        "UPDATE repositories SET provider_installation_id = :installation "
                        "WHERE id = :repository"
                    ),
                    {"installation": new_installation, "repository": REPO_A},
                )
                assert (
                    connection.scalar(
                        text("SELECT origin_provider_installation_id FROM runs WHERE id = :id"),
                        {"id": RUN_DONE},
                    )
                    == INSTALL_A
                )
        finally:
            engine.dispose()

        async def read_projection() -> None:
            async_engine = env.engine()
            try:
                repository = SqlAlchemyRunRepository(
                    async_sessionmaker(async_engine, expire_on_commit=False),
                    allow_unscoped=True,
                )
                historical = await repository.get_run(RUN_DONE)
                assert historical is not None
                assert historical.provider == "github"
                listed = await repository.list_runs(
                    status=None, repository=None, cursor=None, limit=20
                )
                assert next(run for run in listed if run.id == RUN_DONE).provider == "github"
            finally:
                await async_engine.dispose()

        asyncio.run(read_projection())
