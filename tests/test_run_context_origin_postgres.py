"""Late paid conventions cannot insert or overwrite terminal context history."""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.llm_gateway import build_gateway
from app.modules.reviews.application.conventions import (
    ActiveConventionsPrompt,
    GeneratedConventions,
    GenerateRepoConventions,
    RepositoryFile,
    RepositorySnapshot,
)
from app.modules.reviews.application.execute_review import ExecuteReviewRun
from app.modules.reviews.application.handle_review_run import (
    AttemptCheckpoint,
    AttemptProvider,
    CheckpointedProvider,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.conventions_unit_of_work import (
    SqlAlchemyRepositoryConventionsUnitOfWork,
)
from app.modules.reviews.infrastructure.llm.models import (
    GatewayConventionsModel,
    GatewayReviewModel,
)
from app.modules.reviews.infrastructure.llm.settings import LlmSettings
from app.modules.reviews.infrastructure.review_prompt_repository import (
    SqlAlchemyReviewPromptRepository,
)
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunLifecycleUnitOfWork,
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import INSTALL_A, PROMPT, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_llm_gateway import PRIMARY, completion, valid
from tests.test_review_context_stage import _stage
from tests.test_run_diff_origin_postgres import history
from tests.test_run_llm_origin_postgres import run_context
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire


@pytest.fixture
def llm_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


async def context_row(factory: async_sessionmaker[AsyncSession]) -> Any:
    async with factory() as session:
        return await session.scalar(
            text("SELECT to_jsonb(c) FROM context_payloads c WHERE run_id=:id"),
            {"id": RUN_ATTEMPTED},
        )


class _Source:
    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return RepositorySnapshot(None, "a" * 40)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return (RepositoryFile("a.py", 12),)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return (RepositoryFile("a.py", 12, "x = 1"),)


class _Meta:
    async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
        return PullRequestMeta(
            "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
        )


class _Output:
    async def execute(self, run_id: UUID, raw: object) -> bool:
        return True


def _conventions_answer() -> httpx.Response:
    return completion(
        json.dumps(
            {
                "files": [{"path": "a.py", "relevance": "changed file"}],
                "key_patterns": ["One", "Two", "Three"],
                "recommendations": [f"Check {i} (from: standard/correctness)" for i in range(5)],
            }
        )
    )


async def _retire(factory: async_sessionmaker[AsyncSession], boundary: str) -> None:
    await retire(factory)
    if boundary == "rebound":
        assert await apply(factory, FreshGitHub())
    else:
        async with factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE runs SET state='cancelled',"
                    "error_code='installation_unavailable',"
                    "finished_at=clock_timestamp() WHERE id=:id"
                ),
                {"id": RUN_ATTEMPTED},
            )


async def _paid_calls(factory: async_sessionmaker[AsyncSession]) -> tuple[int, int]:
    async with factory() as session:
        usage = await session.scalar(
            text("SELECT count(*) FROM usage_events WHERE run_id=:id"), {"id": RUN_ATTEMPTED}
        )
        calls = await session.scalar(
            text("SELECT count(*) FROM run_actions WHERE run_id=:id AND tool='llm.call'"),
            {"id": RUN_ATTEMPTED},
        )
    return usage, calls


async def _prepare_running(factory: async_sessionmaker[AsyncSession], *, existing: bool) -> None:
    await prepare(factory, "active", "running")
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE provider_installations SET account_external_id=700 WHERE id=:id"),
            {"id": INSTALL_A},
        )
        if existing:
            await session.execute(
                text(
                    "INSERT INTO context_payloads(id,run_id,schema_version,summary) "
                    "VALUES (:id,:id,1,'{\"preserve\": true}')"
                ),
                {"id": RUN_ATTEMPTED},
            )


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["active", "deleted", "rebound"])
@pytest.mark.parametrize("existing", [False, True])
def test_stage_context_payload_write_fences_a_retired_origin_before_conventions(
    llm_env: Env, boundary: str, existing: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        requests: list[httpx.Request] = []

        async def http(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return valid() if len(requests) > 1 else _conventions_answer()

        try:
            await _prepare_running(factory, existing=existing)
            if boundary != "active":
                await _retire(factory, boundary)
            before_context = await context_row(factory)
            before_history = await history(factory)
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(
                    LlmSettings(primary=PRIMARY, fallback=None), client, factory
                )
                generated = GenerateRepoConventions(
                    _Source(),
                    GatewayConventionsModel(gateway, run_context()),
                    partial(SqlAlchemyRepositoryConventionsUnitOfWork, factory),
                )
                trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
                # The stage reads in-memory fakes, so its context_payloads write is the only
                # origin check between the retirement and the paid conventions call.
                stage = _stage(trace, payloads=trace)

                class Processor:
                    async def prepare(self, run_id: UUID) -> GeneratedConventions:
                        await stage.run(run_id)
                        return await generated.execute(
                            repository_id=REPO_A,
                            conventions_prompt=ActiveConventionsPrompt(PROMPT, "Conventions"),
                            run_id=run_id,
                            changed_files=("a.py",),
                        )

                pipeline = ExecuteReviewRun(
                    Processor(),
                    SqlAlchemyReviewPromptRepository(factory),
                    GatewayReviewModel(gateway, run_context(), _Meta()),
                    _Output(),
                )
                if boundary == "active":
                    assert await pipeline.execute(RUN_ATTEMPTED)
                else:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await pipeline.execute(RUN_ATTEMPTED)
            after_context = await context_row(factory)
            if boundary == "active":
                async with factory() as session:
                    build = await session.scalar(
                        text(
                            "SELECT response FROM run_actions "
                            "WHERE run_id=:id AND tool='context.build'"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
                # The stage's build response, as schema 2, also over an earlier v1 row.
                assert build["status"] == "review"
                assert (after_context["schema_version"], after_context["summary"]) == (2, build)
            else:
                assert after_context == before_context
                after_history = await history(factory)
                for key in ("run", "code_change_diffs", "findings", "comments"):
                    assert after_history[key] == before_history[key]
            assert len(requests) == (2 if boundary == "active" else 0)
            assert await _paid_calls(factory) == ((2, 2) if boundary == "active" else (0, 0))
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["active", "deleted", "rebound"])
@pytest.mark.parametrize("existing", [False, True])
def test_origin_retired_during_paid_conventions_is_refused_before_the_review_call(
    llm_env: Env, boundary: str, existing: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        requests: list[httpx.Request] = []
        before_context: Any = None
        before_history: dict[str, object] = {}

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal before_context, before_history
            requests.append(request)
            if len(requests) > 1:
                return valid()
            if boundary != "active":
                await _retire(factory, boundary)
            # The terminal state while the paid conventions call is in flight: the stage's
            # context_payloads row is already written.
            before_context = await context_row(factory)
            before_history = await history(factory)
            return _conventions_answer()

        try:
            await _prepare_running(factory, existing=existing)
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(
                    LlmSettings(primary=PRIMARY, fallback=None), client, factory
                )
                generated = GenerateRepoConventions(
                    _Source(),
                    GatewayConventionsModel(gateway, run_context()),
                    partial(SqlAlchemyRepositoryConventionsUnitOfWork, factory),
                )
                trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
                stage = _stage(trace, payloads=trace)

                class Processor:
                    async def prepare(self, run_id: UUID) -> GeneratedConventions:
                        await stage.run(run_id)
                        return await generated.execute(
                            repository_id=REPO_A,
                            conventions_prompt=ActiveConventionsPrompt(PROMPT, "Conventions"),
                            run_id=run_id,
                            changed_files=("a.py",),
                        )

                # The production fences after conventions: the attempt checkpoint before the
                # review call (worker.py wraps every attempt provider the same way) and the
                # gateway's own checkpoint before its HTTP call; either one refuses the review.
                checkpoint = AttemptCheckpoint(
                    partial(SqlAlchemyRunLifecycleUnitOfWork, factory),
                    RUN_ATTEMPTED,
                    datetime.now(UTC) + timedelta(minutes=5),
                    lambda: datetime.now(UTC),
                )
                review = CheckpointedProvider(
                    cast(AttemptProvider, GatewayReviewModel(gateway, run_context(), _Meta())),
                    checkpoint,
                )
                pipeline = ExecuteReviewRun(
                    Processor(), SqlAlchemyReviewPromptRepository(factory), review, _Output()
                )
                if boundary == "active":
                    assert await pipeline.execute(RUN_ATTEMPTED)
                else:
                    with pytest.raises(RunCancelled, match="installation_unavailable"):
                        await pipeline.execute(RUN_ATTEMPTED)
            after_context = await context_row(factory)
            # The stage wrote its build before conventions, over an earlier v1 row too.
            assert before_context["schema_version"] == 2
            assert before_context["summary"]["status"] == "review"
            # Nothing after the paid call, a late retirement included, rewrites the row.
            assert after_context == before_context
            if boundary != "active":
                after_history = await history(factory)
                for key in ("run", "code_change_diffs", "findings", "comments"):
                    assert after_history[key] == before_history[key]
            # The paid conventions call stays the only one after the retirement.
            assert len(requests) == (2 if boundary == "active" else 1)
            assert await _paid_calls(factory) == ((2, 2) if boundary == "active" else (1, 1))
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
@pytest.mark.parametrize("retired", [False, True])
@pytest.mark.parametrize("existing", [False, True])
def test_running_context_write_fences_retired_origin_without_changing_history(
    llm_env: Env, retired: bool, existing: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare(factory, "active", "running")
            if existing:
                async with factory.begin() as session:
                    await session.execute(
                        text(
                            "INSERT INTO context_payloads(id,run_id,schema_version,summary) "
                            "VALUES (:id,:id,1,'{\"preserve\": true}')"
                        ),
                        {"id": RUN_ATTEMPTED},
                    )
            if retired:
                await retire(factory)
            before_context = await context_row(factory)
            before_history = await history(factory)
            trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
            if retired:
                with pytest.raises(RunCancelled, match="installation_unavailable"):
                    await trace.save_context_payload(RUN_ATTEMPTED, {"late": True})
                assert await context_row(factory) == before_context
            else:
                await trace.save_context_payload(RUN_ATTEMPTED, {"late": True})
                assert (await context_row(factory))["summary"] == {"late": True}
            after_history = await history(factory)
            for key in ("run", "code_change_diffs", "findings", "comments"):
                assert after_history[key] == before_history[key]
        finally:
            await engine.dispose()

    asyncio.run(exercise())
