"""The production repository stores a backend result without ``llm.review_output`` (#130 F5).

DD6 on PostgreSQL: a technical-only change is finalized through the common publishing
transaction; only a model answer is recorded as ``llm.review_output``.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.store_review_output import NoModelAnswer, StoreReviewOutput
from app.modules.reviews.infrastructure.models import Run, RunAction
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from tests.portal_postgres import RUN_ATTEMPTED, Env
from tests.test_clean_without_text_result import answer
from tests.test_clean_without_text_result_postgres import prepare_result
from tests.test_clean_without_text_result_postgres import result_env as result_env
from tests.test_severity_threshold_postgres import RecordingQueue

_TOO_LARGE = DiffSnapshot("large.txt", None, "d" * 40, omission_reason="too_large")


async def _tools(factory: async_sessionmaker[AsyncSession]) -> dict[str, int]:
    async with factory() as session:
        rows = await session.execute(
            select(RunAction.tool, func.count())
            .where(RunAction.run_id == RUN_ATTEMPTED)
            .group_by(RunAction.tool)
        )
        return {tool: count for tool, count in rows.tuples()}


@pytest.mark.integration
def test_postgres_backend_result_enters_publishing_without_a_model_answer(
    result_env: Env,
) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        queue = RecordingQueue()
        store = StoreReviewOutput(
            lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
            queue,
            worker_id="worker",
            run_url=lambda _: None,
        )
        try:
            await prepare_result(factory, [_TOO_LARGE])

            assert await store.execute(RUN_ATTEMPTED, NoModelAnswer()) is True

            assert await _tools(factory) == {"context.build": 1, "review.postprocess": 1}
            async with factory() as session:
                run = await session.get(Run, RUN_ATTEMPTED)
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "review.postprocess"
                    )
                )
            assert run is not None and action is not None
            assert run.state is RunState.PUBLISHING
            assert run.review_body is not None
            assert "**Verdict:** нет доступных для анализа изменений" in run.review_body
            assert isinstance(action.response, dict)
            assert action.response["verdict"] is None
            assert action.response["coverage"]["status"] == "none"
            [pointer] = queue.pointers
            assert pointer.review_event == "COMMENT"

            # The T8 guard: the run is no longer running, nothing is added.
            assert await store.execute(RUN_ATTEMPTED, NoModelAnswer()) is False
            assert await _tools(factory) == {"context.build": 1, "review.postprocess": 1}
            assert len(queue.pointers) == 1
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_postgres_model_answer_is_still_recorded_as_llm_review_output(result_env: Env) -> None:
    async def scenario() -> None:
        engine = result_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            await prepare_result(factory, [_TOO_LARGE])

            assert await StoreReviewOutput(
                lambda: SqlAlchemyReviewOutputUnitOfWork(factory),
                RecordingQueue(),
                worker_id="worker",
                run_url=lambda _: None,
            ).execute(RUN_ATTEMPTED, answer())

            assert await _tools(factory) == {
                "context.build": 1,
                "llm.review_output": 1,
                "review.postprocess": 1,
            }
            async with factory() as session:
                action = await session.scalar(
                    select(RunAction).where(
                        RunAction.run_id == RUN_ATTEMPTED, RunAction.tool == "llm.review_output"
                    )
                )
            assert action is not None and action.response == answer()
        finally:
            await engine.dispose()

    asyncio.run(scenario())
