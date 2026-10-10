"""Late paid conventions cannot insert or overwrite terminal context history."""

from __future__ import annotations

import asyncio
import json
from functools import partial
from typing import Any
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
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.run_trace import (
    TracedReviewPromptRepository,
    TransactionalRunTrace,
)
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
from app.modules.reviews.infrastructure.run_lifecycle_store import SqlAlchemyRunTraceUnitOfWork
from tests.portal_postgres import INSTALL_A, PROMPT, REPO_A, RUN_ATTEMPTED, Env
from tests.test_app_reinstall_postgres import FreshGitHub, apply
from tests.test_llm_gateway import PRIMARY, completion, valid
from tests.test_run_diff_origin_postgres import history
from tests.test_run_llm_origin_postgres import llm_env as llm_env
from tests.test_run_llm_origin_postgres import run_context
from tests.test_run_origin_guard_postgres import prepare
from tests.test_run_publication_origin_postgres import retire


async def context_row(factory: async_sessionmaker[AsyncSession]) -> Any:
    async with factory() as session:
        return await session.scalar(
            text("SELECT to_jsonb(c) FROM context_payloads c WHERE run_id=:id"),
            {"id": RUN_ATTEMPTED},
        )


@pytest.mark.integration
@pytest.mark.parametrize("boundary", ["active", "deleted", "rebound"])
@pytest.mark.parametrize("existing", [False, True])
def test_paid_conventions_context_build_preserves_terminal_payload(
    llm_env: Env, boundary: str, existing: bool
) -> None:
    async def exercise() -> None:
        engine = llm_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        requests: list[httpx.Request] = []
        before_context: Any = None
        before_history: dict[str, object] = {}

        class Source:
            async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
                return RepositorySnapshot(None, "a" * 40)

            async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
                return (RepositoryFile("a.py", 12),)

            async def fetch_files(
                self, repository_id: UUID, paths: tuple[str, ...]
            ) -> tuple[RepositoryFile, ...]:
                return (RepositoryFile("a.py", 12, "x = 1"),)

        class Meta:
            async def get_pull_request_meta(self, run_id: UUID) -> PullRequestMeta:
                return PullRequestMeta(
                    "Title", None, "author", "feature", "main", (), 1, 1, 0, False, False
                )

        class Output:
            async def execute(self, run_id: UUID, raw: object) -> bool:
                return True

        async def http(request: httpx.Request) -> httpx.Response:
            nonlocal before_context, before_history
            requests.append(request)
            if len(requests) > 1:
                return valid()
            if boundary != "active":
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
            before_context = await context_row(factory)
            before_history = await history(factory)
            return completion(
                json.dumps(
                    {
                        "files": [{"path": "a.py", "relevance": "changed file"}],
                        "key_patterns": ["One", "Two", "Three"],
                        "recommendations": [
                            f"Check {i} (from: standard/correctness)" for i in range(5)
                        ],
                    }
                )
            )

        try:
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
            async with httpx.AsyncClient(transport=httpx.MockTransport(http)) as client:
                gateway = build_gateway(
                    LlmSettings(primary=PRIMARY, fallback=None), client, factory
                )
                generated = GenerateRepoConventions(
                    Source(),
                    GatewayConventionsModel(gateway, run_context()),
                    partial(SqlAlchemyRepositoryConventionsUnitOfWork, factory),
                )

                class Processor:
                    async def prepare(self, run_id: UUID) -> GeneratedConventions:
                        return await generated.execute(
                            repository_id=REPO_A,
                            conventions_prompt=ActiveConventionsPrompt(PROMPT, "Conventions"),
                            run_id=run_id,
                            changed_files=("a.py",),
                        )

                trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
                pipeline = ExecuteReviewRun(
                    Processor(),
                    TracedReviewPromptRepository(
                        SqlAlchemyReviewPromptRepository(factory), trace, trace, engine="fast"
                    ),
                    GatewayReviewModel(gateway, run_context(), Meta()),
                    Output(),
                )
                if boundary == "active":
                    assert await pipeline.execute(RUN_ATTEMPTED)
                else:
                    with pytest.raises(RunCancelled):
                        await pipeline.execute(RUN_ATTEMPTED)
            after_context = await context_row(factory)
            if boundary == "active":
                assert after_context["summary"] == {
                    "files": [],
                    "omitted_files": [],
                    "budget": {"limit": 60000, "used": 0},
                }
            else:
                assert after_context == before_context
                after_history = await history(factory)
                for key in ("run", "code_change_diffs", "findings", "comments"):
                    assert after_history[key] == before_history[key]
            assert len(requests) == (2 if boundary == "active" else 1)
            async with factory() as session:
                assert await session.scalar(
                    text("SELECT count(*) FROM usage_events WHERE run_id=:id"),
                    {"id": RUN_ATTEMPTED},
                ) == (2 if boundary == "active" else 1)
                assert await session.scalar(
                    text("SELECT count(*) FROM run_actions WHERE run_id=:id AND tool='llm.call'"),
                    {"id": RUN_ATTEMPTED},
                ) == (2 if boundary == "active" else 1)
        finally:
            await engine.dispose()

    asyncio.run(exercise())
