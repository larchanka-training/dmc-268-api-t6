"""``enter_publishing`` writes the ``llm.verify`` run action in PostgreSQL (#130 PR E, R13).

The row sits before ``review.postprocess`` in the publishing transaction, at most once per
run, and only when the store passes a verifier trace: a legacy run writes none.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import ReviewOutput, ReviewSummary
from app.modules.reviews.infrastructure.models import RunAction
from app.modules.reviews.infrastructure.review_output_unit_of_work import (
    SqlAlchemyReviewOutputUnitOfWork,
)
from tests.portal_postgres import NOW, Env, portal_schema
from tests.test_run_stream_postgres import insert_queued_run

STARTED = datetime(2026, 10, 24, 9, 0, 0, tzinfo=UTC)
VERIFY_REQUEST: dict[str, Any] = {
    "candidate_ids": ["c-1", "c-2"],
    "accepted_call_id": "00000000-0000-0000-0000-000000013911",
    "manifest_id": None,
}
VERIFY_RESPONSE: dict[str, Any] = {
    "decisions": [
        {
            "candidate_id": "c-1",
            "status": "supported",
            "evidence_refs": [{"block_id": "file-1", "start_line": 10, "end_line": 10}],
            "requirement_refs": [],
        },
        {
            "candidate_id": "c-2",
            "status": "insufficient_context",
            "evidence_refs": [],
            "requirement_refs": [],
        },
    ]
}


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


def _model_output() -> dict[str, object]:
    return {
        "findings": [],
        "summary": {"problem": "A bug.", "done_well": "Small change.", "effort": "small"},
    }


def _parsed_output() -> ReviewOutput:
    return ReviewOutput(
        findings=[],
        summary=ReviewSummary(problem="A bug.", done_well="Small change.", effort="small"),
    )


def _processed_output() -> ProcessedReviewOutput:
    return ProcessedReviewOutput(inline=(), body_only=(), dropped=(), review_body="A bug.")


async def _publish(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    *,
    trace: bool,
    commit: bool = True,
) -> None:
    async with SqlAlchemyReviewOutputUnitOfWork(factory) as uow:
        stored = await uow.reviews.store_review_output(
            run_id, _model_output(), _parsed_output(), _processed_output()
        )
        assert stored is not None
        await uow.reviews.enter_publishing(
            run_id,
            lease_until=NOW + timedelta(minutes=5),
            postprocess_request={"min_confidence": 0.5, "max_inline": 8},
            postprocess_response={"inline": 0, "body_only": 0, "dropped": []},
            started_at=STARTED,
            duration_ms=11,
            verify_request=VERIFY_REQUEST if trace else None,
            verify_response=VERIFY_RESPONSE if trace else None,
        )
        if commit:
            await uow.commit()
        else:
            await uow.rollback()


async def _actions(
    factory: async_sessionmaker[AsyncSession], run_id: UUID
) -> list[tuple[int, str, Any, Any]]:
    async with factory() as session:
        rows = await session.execute(
            select(RunAction.index, RunAction.tool, RunAction.request, RunAction.response)
            .where(RunAction.run_id == run_id)
            .order_by(RunAction.index)
        )
        return [(index, tool, request, response) for index, tool, request, response in rows]


@pytest.mark.integration
def test_llm_verify_is_written_before_the_postprocess_in_one_transaction(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> tuple[list[tuple[int, str, Any, Any]], list[tuple[int, str]]]:
        try:
            run_id = await insert_queued_run(factory)
            await _publish(factory, run_id, trace=True, commit=False)
            after_rollback = [(i, tool) for i, tool, _, _ in await _actions(factory, run_id)]
            await _publish(factory, run_id, trace=True)
            return await _actions(factory, run_id), after_rollback
        finally:
            await engine.dispose()

    actions, after_rollback = asyncio.run(scenario())
    assert [(index, tool) for index, tool, _, _ in actions] == [
        (0, "llm.review_output"),
        (1, "llm.verify"),
        (2, "review.postprocess"),
    ]
    assert actions[1][2] == VERIFY_REQUEST
    assert actions[1][3] == VERIFY_RESPONSE
    assert after_rollback == []


@pytest.mark.integration
def test_a_second_publishing_entry_adds_no_second_llm_verify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> list[tuple[int, str, Any, Any]]:
        try:
            run_id = await insert_queued_run(factory)
            await _publish(factory, run_id, trace=True)
            await _publish(factory, run_id, trace=True)
            return await _actions(factory, run_id)
        finally:
            await engine.dispose()

    tools = [tool for _, tool, _, _ in asyncio.run(scenario())]
    assert tools.count("llm.verify") == 1
    assert tools[:3] == ["llm.review_output", "llm.verify", "review.postprocess"]


@pytest.mark.integration
def test_a_legacy_store_writes_no_llm_verify(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)

    async def scenario() -> list[tuple[int, str, Any, Any]]:
        try:
            run_id = await insert_queued_run(factory)
            await _publish(factory, run_id, trace=False)
            return await _actions(factory, run_id)
        finally:
            await engine.dispose()

    assert [(index, tool) for index, tool, _, _ in asyncio.run(scenario())] == [
        (0, "llm.review_output"),
        (1, "review.postprocess"),
    ]
