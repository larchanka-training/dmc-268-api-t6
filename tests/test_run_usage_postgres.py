"""Run list and detail rows sum every usage event in the same SELECT."""

from __future__ import annotations

import asyncio
import os
import selectors
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from tests.portal_postgres import NOW, RUN_ATTEMPTED, RUN_DONE, WS_A, Env, portal_schema

pytestmark = pytest.mark.integration

_EVENT_EARLIER = UUID("00000000-0000-4000-8000-000000003101")
_EVENT_LATER = UUID("00000000-0000-4000-8000-000000003102")


@pytest.fixture
def usage_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def test_run_usage_sums_both_attempts_without_an_extra_statement_per_run(usage_env: Env) -> None:
    async def exercise() -> None:
        engine = usage_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        repository = SqlAlchemyRunRepository(factory, scope=AuthScope(42, (WS_A,)))
        try:
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE runs SET attempt = 2 WHERE id = :id"),
                    {"id": RUN_DONE},
                )
                await session.execute(
                    text(
                        "INSERT INTO usage_events (id, run_id, workspace_id, provider, model, "
                        "operation, tokens_in, tokens_out, cost_usd, created_at) VALUES "
                        "(:earlier, :run, :workspace, 'openai', 'gpt', 'review', 1, 2, "
                        ":cost_earlier, :at_earlier), "
                        "(:later, :run, :workspace, 'openai', 'gpt', 'review', 2, 3, "
                        ":cost_later, :at_later)"
                    ),
                    {
                        "earlier": _EVENT_EARLIER,
                        "later": _EVENT_LATER,
                        "run": RUN_DONE,
                        "workspace": WS_A,
                        "cost_earlier": Decimal("0.125"),
                        "cost_later": Decimal("0.250"),
                        "at_earlier": NOW - timedelta(minutes=2),
                        "at_later": NOW - timedelta(minutes=1),
                    },
                )

            summed = await repository.get_run(RUN_DONE)
            untouched = await repository.get_run(RUN_ATTEMPTED)
            assert summed is not None and untouched is not None
            assert summed.usage.tokens_in == 3
            assert summed.usage.tokens_out == 5
            assert summed.usage.cost_usd == Decimal("0.375")
            assert untouched.usage.tokens_in == 0
            assert untouched.usage.tokens_out == 0
            assert untouched.usage.cost_usd == Decimal("0")

            statements: list[str] = []

            def record(
                connection: object,
                cursor: object,
                statement: str,
                parameters: object,
                context: object,
                many: bool,
            ) -> None:
                statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", record)
            try:
                page_of_one = await repository.list_runs(
                    status=None, repository="octo/a", cursor=None, limit=1
                )
                one_run_statements = list(statements)
                statements.clear()
                page_of_three = await repository.list_runs(
                    status=None, repository="octo/a", cursor=None, limit=3
                )
                three_run_statements = list(statements)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", record)

            assert len(page_of_one) == 1
            assert len(page_of_three) == 3
            listed = next(item for item in page_of_three if item.id == RUN_DONE)
            assert listed.usage.tokens_in == 3
            assert listed.usage.tokens_out == 5
            assert listed.usage.cost_usd == Decimal("0.375")
            quiet = next(item for item in page_of_three if item.id == RUN_ATTEMPTED)
            assert quiet.usage.tokens_in == 0
            assert quiet.usage.tokens_out == 0
            assert quiet.usage.cost_usd == Decimal("0")
            assert len(one_run_statements) == len(three_run_statements)
            assert _selects(one_run_statements) == 1
            assert _selects(three_run_statements) == 1
        finally:
            await engine.dispose()

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def _selects(statements: list[str]) -> int:
    return sum(1 for statement in statements if statement.lstrip().upper().startswith("SELECT"))
