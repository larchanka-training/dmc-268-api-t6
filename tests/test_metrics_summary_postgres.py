"""Metrics summary aggregates visible runs by the UTC day they were created."""

from __future__ import annotations

import asyncio
import os
import selectors
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_metrics_summary import GetMetricsSummary
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary
from tests.portal_postgres import PR_B, PR_NEW, PR_OPEN, WS_A, WS_B, Env, portal_schema

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
_RUN_OK = UUID("00000000-0000-4000-8000-000000003201")
_RUN_BAD = UUID("00000000-0000-4000-8000-000000003202")
_RUN_HIDDEN = UUID("00000000-0000-4000-8000-000000003203")
_FINDING = UUID("00000000-0000-4000-8000-000000003204")


@pytest.fixture
def metrics_env() -> Iterator[Env]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    with portal_schema(database_url, None) as env:
        yield env


def test_metrics_summary_counts_visible_runs_and_todays_spend(metrics_env: Env) -> None:
    async def exercise() -> None:
        engine = metrics_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE workspaces SET name = 'alpha', daily_budget_usd = 1 WHERE id = :id"
                    ),
                    {"id": WS_A},
                )
                await session.execute(
                    text("UPDATE workspaces SET name = 'beta' WHERE id = :id"),
                    {"id": WS_B},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET created_at = '2020-01-01T00:00:00Z', "
                        "finished_at = '2020-01-01T00:00:00Z'"
                    )
                )
                await _insert_run(
                    session,
                    _RUN_OK,
                    PR_OPEN,
                    "succeeded",
                    datetime(2026, 10, 26, 10, tzinfo=UTC),
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    "a" * 64,
                )
                await _insert_run(
                    session,
                    _RUN_BAD,
                    PR_NEW,
                    "failed",
                    datetime(2026, 10, 28, 8, tzinfo=UTC),
                    datetime(2026, 10, 28, 8, 30, tzinfo=UTC),
                    "b" * 64,
                )
                await _insert_run(
                    session,
                    _RUN_HIDDEN,
                    PR_B,
                    "succeeded",
                    datetime(2026, 10, 27, 10, tzinfo=UTC),
                    datetime(2026, 10, 27, 11, tzinfo=UTC),
                    "c" * 64,
                )
                await session.execute(
                    text(
                        "INSERT INTO findings (id, run_id, file_path, line_start, severity, "
                        "confidence, category, title, body, published, inline_comment) VALUES "
                        "(:id, :run, 'auth.py', 4, 'high', 0.90, 'security', 'Secret', "
                        "'A secret is logged', true, false)"
                    ),
                    {"id": _FINDING, "run": _RUN_OK},
                )
                await session.execute(
                    text(
                        "INSERT INTO usage_events (id, run_id, workspace_id, provider, model, "
                        "operation, tokens_in, tokens_out, cost_usd, created_at) VALUES "
                        "(:a, :ok, :ws_a, 'openai', 'gpt', 'review', 10, 4, 0.200, "
                        "'2026-10-26T10:00:00Z'), "
                        "(:b, :ok, :ws_a, 'openai', 'gpt', 'review', 5, 1, 0.050, "
                        "'2026-10-28T01:00:00Z'), "
                        "(:c, :bad, :ws_a, 'openai', 'gpt', 'review', 1, 2, 0.125, "
                        "'2026-10-28T08:00:00Z'), "
                        "(:d, :hidden, :ws_b, 'openai', 'gpt', 'review', 1000, 1, 9, "
                        "'2026-10-28T02:00:00Z')"
                    ),
                    {
                        "a": UUID("00000000-0000-4000-8000-000000003211"),
                        "b": UUID("00000000-0000-4000-8000-000000003212"),
                        "c": UUID("00000000-0000-4000-8000-000000003213"),
                        "d": UUID("00000000-0000-4000-8000-000000003214"),
                        "ok": _RUN_OK,
                        "bad": _RUN_BAD,
                        "hidden": _RUN_HIDDEN,
                        "ws_a": WS_A,
                        "ws_b": WS_B,
                    },
                )
            statements: list[str] = []

            def count(
                conn: object,
                cursor: object,
                statement: str,
                parameters: object,
                context: object,
                executemany: bool,
            ) -> None:
                del conn, cursor, parameters, context, executemany
                if statement.lstrip().lower().startswith("select"):
                    statements.append(statement)

            event.listen(engine.sync_engine, "before_cursor_execute", count)
            try:
                summary = await GetMetricsSummary(
                    SqlAlchemyMetricsSummary(factory, scope), now=lambda: _NOW
                ).execute(scope, range="7d", repo=None)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count)
        finally:
            await engine.dispose()

        assert len(statements) == 7
        assert summary.totals.runs_by_status.succeeded == 1
        assert summary.totals.runs_by_status.failed == 1
        assert summary.totals.runs_by_status.queued == 0
        assert summary.totals.pulls_by_verdict.blocking == 1
        assert summary.totals.pulls_by_verdict.no_verdict == 1
        assert summary.totals.findings_by_severity.high == 1
        assert summary.totals.findings_by_severity.critical == 0
        assert summary.totals.findings_by_category.security == 1
        assert summary.totals.findings_by_category.correctness == 0
        assert summary.totals.tokens_in == 16
        assert summary.totals.tokens_out == 7
        assert summary.totals.cost_usd == Decimal("0.375")
        assert summary.totals.succeeded_average_duration_seconds == 3600.0
        by_day = {item.day.isoformat(): item for item in summary.series}
        assert by_day["2026-10-26"].run_count == 1
        assert by_day["2026-10-26"].cost_usd == Decimal("0.250")
        assert by_day["2026-10-26"].average_duration_seconds == 3600.0
        assert by_day["2026-10-26"].findings_by_category.security == 1
        assert by_day["2026-10-27"].run_count == 0
        assert by_day["2026-10-27"].cost_usd == Decimal("0")
        assert by_day["2026-10-27"].findings_by_category.security == 0
        assert by_day["2026-10-28"].run_count == 1
        assert by_day["2026-10-28"].cost_usd == Decimal("0.125")
        assert by_day["2026-10-28"].average_duration_seconds == 0.0
        assert [item.name for item in summary.workspaces] == ["alpha"]
        assert summary.workspaces[0].spent_today_usd == Decimal("0.175")
        assert summary.workspaces[0].daily_budget_usd == Decimal("1")
        assert summary.workspaces[0].paused is False

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


async def _insert_run(
    session: object,
    run_id: UUID,
    code_change_id: UUID,
    state: str,
    created_at: datetime,
    finished_at: datetime,
    idempotency_key: str,
) -> None:
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO runs (id, code_change_id, base_sha, base_ref, head_sha, state, trigger, "
            "idempotency_key, engine, rule_version_id, prompt_version_id, available_at, attempt, "
            "created_at, finished_at, origin_provider_installation_id) "
            "SELECT :id, :pr, base_sha, base_ref, head_sha, :state, 'manual', :key, engine, "
            "rule_version_id, prompt_version_id, :created, 1, :created, :finished, "
            "origin_provider_installation_id FROM runs LIMIT 1"
        ),
        {
            "id": run_id,
            "pr": code_change_id,
            "state": state,
            "key": idempotency_key,
            "created": created_at,
            "finished": finished_at,
        },
    )
