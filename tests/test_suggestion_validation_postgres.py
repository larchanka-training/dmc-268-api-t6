"""PostgreSQL integration check for suggestion validation & connection pool return (#444)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine

from app.modules.reviews.application.get_run_diff import DiffSnapshot, StoreDiffSnapshot
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.review_output import (
    PublishedFinding,
    PublishReviewOutput,
)
from app.modules.reviews.application.store_review_output import StoreReviewOutput
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from app.modules.reviews.infrastructure.run_processing_unit_of_work import (
    SqlAlchemyRunProcessingUnitOfWork,
)
from tests.portal_postgres import HEAD, PR_NEW, REPO_A, RUN_ATTEMPTED, Env, portal_schema
from tests.verifier_pipeline_fakes import FakeQueue, InProcessSuggestionParser

_FIXTURE = Path(__file__).parent / "fixtures" / "swapi" / "films_858e1c1.ts"
_PATH = "src/tools/films.ts"
_BLOB_SHA = "429179313c40dff554f894a40de9652ba34c8a77"
_PATCH = (
    "@@ -95,1 +95,2 @@\n"
    " const hasEpisodeId = episodeId !== undefined;\n"
    "+if ((hasFilmId && hasEpisodeId) || (!hasFilmId && !hasEpisodeId)) {"
)


def _make_pool_parser(engine: AsyncEngine) -> InProcessSuggestionParser:
    def _assert_pool_zero(_reqs: object) -> None:
        assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]

    return InProcessSuggestionParser(on_validate=_assert_pool_zero)


@pytest.fixture
def pg_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


class _PoolCheckingProvider:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self.calls: list[tuple[PublishedFinding, ...]] = []

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        # AGENTS.md: no DB transaction or checked-out connection during provider call.
        assert self._engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
        self.calls.append(findings)


@pytest.mark.integration
def test_store_and_publish_return_pool_connection_after_reading_blob_cache(pg_env: Env) -> None:
    engine = create_async_engine(
        pg_env.database_url,
        connect_args={"options": f"-csearch_path={pg_env.schema}"},
        pool_size=1,
        max_overflow=0,
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    films_text = _FIXTURE.read_text(encoding="utf-8")

    answer = {
        "findings": [
            {
                "path": _PATH,
                "start_line": None,
                "line": 96,
                "severity": "high",
                "category": "correctness",
                "title": "McpError not imported",
                "body": "Move the import to module top level.",
                "suggestion": (
                    'import { McpError, ErrorCode } from "@modelcontextprotocol/sdk/server/mcp.js";'
                ),
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
        "summary": {
            "problem": "Missing import.",
            "done_well": "Clean validation.",
            "effort": "small",
        },
    }

    async def exercise() -> None:
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
                    {"run": RUN_ATTEMPTED},
                )
            async with SqlAlchemyRunProcessingUnitOfWork(factory) as uow:
                await StoreDiffSnapshot(uow.repository).execute(
                    run_id=RUN_ATTEMPTED,
                    code_change_id=PR_NEW,
                    head_sha=HEAD,
                    files=[
                        DiffSnapshot(
                            _PATH,
                            _PATCH,
                            blob_sha=_BLOB_SHA,
                            status="modified",
                            additions=1,
                            deletions=0,
                            changes=1,
                        )
                    ],
                    excluded=lambda _f: False,
                )
                await uow.blob_cache.put(
                    BlobCacheKey(REPO_A, _BLOB_SHA), films_text, ttl=timedelta(hours=1)
                )
                await uow.commit()

            assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            parser = _make_pool_parser(engine)
            stored = await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                FakeQueue(),
                worker_id="worker",
                run_url=lambda _: None,
                parser=parser,
            ).execute(RUN_ATTEMPTED, answer)
            assert stored is True
            assert len(parser.seen_requests) > 0
            assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]

            async with factory() as session:
                rows = (
                    await session.execute(
                        text(
                            "SELECT suggestion, body, inline_comment "
                            "FROM findings WHERE run_id=:run"
                        ),
                        {"run": RUN_ATTEMPTED},
                    )
                ).all()
            assert len(rows) == 1
            assert rows[0][0] is None
            assert "Move the import to module top level." in rows[0][1]
            assert rows[0][2] is True

            # PublishReviewOutput also reads blob cache inside closed UoW before provider call.
            provider = _PoolCheckingProvider(engine)
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET state='running', worker_id='worker' WHERE id=:run"),
                    {"run": RUN_ATTEMPTED},
                )
                await session.execute(
                    text("DELETE FROM findings WHERE run_id=:run"), {"run": RUN_ATTEMPTED}
                )
                await session.execute(
                    text("DELETE FROM run_actions WHERE run_id=:run AND tool='llm.review_output'"),
                    {"run": RUN_ATTEMPTED},
                )
            published = await PublishReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                provider,
                parser=_make_pool_parser(engine),
            ).execute(RUN_ATTEMPTED, answer)
            assert published is True
            assert engine.sync_engine.pool.checkedout() == 0  # type: ignore[attr-defined]
            assert provider.calls[0][0].suggestion is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())
