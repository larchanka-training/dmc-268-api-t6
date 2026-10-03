"""LLM gateway persistence on PostgreSQL: ``usage_events`` sums and ``llm.call`` (#33).

Opt-in: set ``TEST_DATABASE_URL`` to a disposable PostgreSQL database.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from functools import partial
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.analytics.infrastructure.usage_ledger import SqlAlchemyUsageLedger
from app.modules.reviews.application.llm import (
    LlmCallKind,
    LlmCallRecord,
    LlmUsage,
    RunCallContext,
)
from app.modules.reviews.application.run_trace import TransactionalRunTrace
from app.modules.reviews.infrastructure.llm_call_trace import RunTraceLlmCalls
from app.modules.reviews.infrastructure.run_lifecycle_store import (
    SqlAlchemyRunTraceUnitOfWork,
)
from tests.portal_postgres import NOW, RUN_B, RUN_DONE, WS_A, WS_B, Env, portal_schema


@pytest.fixture
def env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as schema:
        yield schema


def _usage(cost: str) -> LlmUsage:
    return LlmUsage("eurouter", "gpt-4.1-mini-2025-04-14", "review", 100, 10, 0, Decimal(cost))


def _record(call_no: int) -> LlmCallRecord:
    return LlmCallRecord(
        kind=LlmCallKind.PRIMARY if call_no == 1 else LlmCallKind.RETRY,
        model="gpt-4.1-mini",
        call_no=call_no,
        attempt=1,
        timeout_s=90.0,
        prompt_version_id=None,
        rule_version_id=None,
        input_tokens_estimate=100,
        started_at=NOW,
        duration_ms=5,
        response={"model": "gpt-4.1-mini-2025-04-14", "choices": []},
    )


def _rows(factory: async_sessionmaker[Any], sql: str, **values: Any) -> list[Any]:
    async def read() -> list[Any]:
        async with factory() as session:
            return list((await session.execute(text(sql), values)).all())

    return asyncio.run(read())


@pytest.mark.integration
def test_run_cost_sums_every_attempt_of_one_run_only(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    ledger = SqlAlchemyUsageLedger(factory)
    deadline = NOW + timedelta(minutes=8)
    try:

        async def scenario() -> tuple[Decimal, Decimal, Decimal]:
            for attempt, cost in ((1, "0.010000"), (2, "0.020500")):
                await ledger.record(
                    RunCallContext(RUN_DONE, WS_A, attempt, "fast", deadline), _usage(cost)
                )
            await ledger.record(RunCallContext(RUN_B, WS_B, 1, "fast", deadline), _usage("0.4"))
            unknown = RUN_DONE.int + 1000
            return (
                await ledger.run_cost_usd(RUN_DONE),
                await ledger.run_cost_usd(RUN_B),
                await ledger.run_cost_usd(type(RUN_DONE)(int=unknown)),
            )

        done, other, none = asyncio.run(scenario())
    finally:
        asyncio.run(engine.dispose())

    assert (done, other, none) == (Decimal("0.030500"), Decimal("0.400000"), Decimal("0"))


@pytest.mark.integration
def test_llm_calls_continue_the_run_action_indexes_next_to_other_steps(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    run_trace = TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory))
    calls = RunTraceLlmCalls(run_trace)
    try:

        async def scenario() -> None:
            # another run's actions must not shift the indexes of this run
            for _ in range(3):
                await run_trace.record(RUN_B, "context.build", {}, {}, NOW, 1)
            await run_trace.record(RUN_DONE, "context.build", {}, {"files": []}, NOW, 1)
            for call_no in (1, 2, 3):
                await calls.record_call(RUN_DONE, _record(call_no))
            await run_trace.record(RUN_DONE, "llm.review_output", {}, {"findings": []}, NOW, 0)

        asyncio.run(scenario())
        rows = _rows(
            factory,
            "SELECT index, tool, request ->> 'call_no' FROM run_actions "
            "WHERE run_id = :id ORDER BY index",
            id=RUN_DONE,
        )
    finally:
        asyncio.run(engine.dispose())

    assert rows == [
        (0, "context.build", None),
        (1, "llm.call", "1"),
        (2, "llm.call", "2"),
        (3, "llm.call", "3"),
        (4, "llm.review_output", None),
    ]


@pytest.mark.integration
def test_concurrent_llm_call_writes_get_distinct_indexes(env: Env) -> None:
    engine = env.engine()
    factory = async_sessionmaker(engine, expire_on_commit=False)
    calls = RunTraceLlmCalls(TransactionalRunTrace(partial(SqlAlchemyRunTraceUnitOfWork, factory)))
    try:

        async def scenario() -> None:
            await asyncio.gather(
                *(calls.record_call(RUN_DONE, _record(call_no)) for call_no in range(1, 9))
            )

        asyncio.run(scenario())
        rows = _rows(
            factory,
            "SELECT index FROM run_actions WHERE run_id = :id ORDER BY index",
            id=RUN_DONE,
        )
    finally:
        asyncio.run(engine.dispose())

    assert [row[0] for row in rows] == list(range(8))
