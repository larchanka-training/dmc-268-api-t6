"""Historical run provider is pinned to its original installation."""

from __future__ import annotations

import asyncio
import os
from uuid import UUID

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bootstrap.bitbucket_reviews import BitbucketReviewWorkerResources
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.reviews.infrastructure.bitbucket_ci import (
    SqlAlchemyBitbucketStatusSource,
    SqlAlchemyRunProvider,
)
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


@pytest.mark.integration
def test_bitbucket_status_bootstrap_skips_github_and_gitlab_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")

    gitlab_installation = UUID("00000000-0000-4000-8000-0000000000d2")
    gitlab_run = UUID("00000000-0000-4000-8000-0000000000d3")
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
                        "VALUES (:id, :workspace, 'gitlab', 777, '{}'::jsonb)"
                    ),
                    {"id": gitlab_installation, "workspace": WS_A},
                )
                connection.execute(
                    text(
                        "INSERT INTO runs (id, code_change_id, origin_provider_installation_id, "
                        "base_sha, base_ref, head_sha, state, trigger, idempotency_key, engine, "
                        "rule_version_id, prompt_version_id, available_at) "
                        "SELECT :new_id, code_change_id, :installation, base_sha, base_ref, "
                        "head_sha, 'succeeded', 'manual', :key, engine, rule_version_id, "
                        "prompt_version_id, now() FROM runs WHERE id = :original"
                    ),
                    {
                        "new_id": gitlab_run,
                        "installation": gitlab_installation,
                        "key": "9" * 64,
                        "original": RUN_DONE,
                    },
                )
        finally:
            engine.dispose()

        async def scenario() -> None:
            async_engine = env.engine()
            try:
                sessions = async_sessionmaker(async_engine, expire_on_commit=False)
                run_provider = SqlAlchemyRunProvider(sessions)
                assert await run_provider(RUN_DONE) == "github"
                assert await run_provider(gitlab_run) == "gitlab"

                async def forbidden_current(
                    _source: SqlAlchemyBitbucketStatusSource, _run_id: UUID
                ) -> None:
                    raise AssertionError("foreign run entered Bitbucket status source")

                monkeypatch.setattr(SqlAlchemyBitbucketStatusSource, "current", forbidden_current)
                resources = BitbucketReviewWorkerResources(
                    session_factory=sessions,
                    cipher=BitbucketCredentialCipher(
                        active_key_id="test", keys={"test": b"k" * 32}
                    ),
                )
                await resources.statuses.upsert(RUN_DONE, "https://review.test/runs/github")
                await resources.statuses.upsert(gitlab_run, "https://review.test/runs/gitlab")
            finally:
                await async_engine.dispose()

        asyncio.run(scenario())
