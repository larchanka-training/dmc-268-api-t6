"""Metrics summary aggregates visible runs by the UTC day they were created."""

from __future__ import annotations

import asyncio
import selectors
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_metrics_summary import GetMetricsSummary
from app.modules.reviews.application.list_pulls import ListRepositoryPulls
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from tests.portal_postgres import (
    HEAD,
    INSTALL_A,
    PR_B,
    PR_CLOSED,
    PR_NEW,
    PR_OPEN,
    REPO_A,
    WS_A,
    WS_B,
    Env,
    clean_review_result,
)

pytest_plugins = ("tests.portal_postgres",)
pytestmark = pytest.mark.integration

_NOW = datetime(2026, 10, 28, 12, tzinfo=UTC)
_RUN_OK = UUID("00000000-0000-4000-8000-000000003201")
_RUN_BAD = UUID("00000000-0000-4000-8000-000000003202")
_RUN_HIDDEN = UUID("00000000-0000-4000-8000-000000003203")
_FINDING = UUID("00000000-0000-4000-8000-000000003204")


def test_metrics_summary_counts_visible_runs_and_todays_spend(portal_env: Env) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
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
                first_selects = len(statements)
                async with factory.begin() as session:
                    await _insert_run(
                        session,
                        UUID("00000000-0000-4000-8000-000000003220"),
                        PR_CLOSED,
                        "succeeded",
                        datetime(2026, 10, 27, 9, tzinfo=UTC),
                        datetime(2026, 10, 27, 10, tzinfo=UTC),
                        "e" * 64,
                    )
                statements.clear()
                await GetMetricsSummary(
                    SqlAlchemyMetricsSummary(factory, scope), now=lambda: _NOW
                ).execute(scope, range="7d", repo=None)
            finally:
                event.remove(engine.sync_engine, "before_cursor_execute", count)
        finally:
            await engine.dispose()

        # Runs by day and state, terminal heads, their saved results, the diff fallback of a
        # head without one, findings by day, findings by severity and category, workspaces.
        assert first_selects == 7
        assert len(statements) == first_selects
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


def test_a_day_sums_runs_of_every_state_with_and_without_usage(portal_env: Env) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        succeeded_with_usage = uuid4()
        cancelled_with_usage = uuid4()
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET created_at = '2020-01-01T00:00:00Z', "
                        "finished_at = '2020-01-01T00:00:00Z'"
                    )
                )
                for run_id, pr, state, created, finished, key in (
                    (succeeded_with_usage, PR_OPEN, "succeeded", (27, 9, 0), (27, 9, 30), "f"),
                    (uuid4(), PR_NEW, "succeeded", (27, 10, 0), (27, 11, 0), "g"),
                    (cancelled_with_usage, PR_CLOSED, "cancelled", (27, 11, 0), (27, 11, 5), "h"),
                    (uuid4(), PR_OPEN, "failed", (28, 8, 0), (28, 8, 10), "i"),
                ):
                    await _insert_run(
                        session,
                        run_id,
                        pr,
                        state,
                        datetime(2026, 10, created[0], created[1], created[2], tzinfo=UTC),
                        datetime(2026, 10, finished[0], finished[1], finished[2], tzinfo=UTC),
                        key * 64,
                    )
                await session.execute(
                    text(
                        "INSERT INTO usage_events (id, run_id, workspace_id, provider, model, "
                        "operation, tokens_in, tokens_out, cost_usd, created_at) VALUES "
                        "(gen_random_uuid(), :ok, :ws, 'openai', 'gpt', 'review', 3, 1, 0.100, "
                        "'2026-10-27T09:10:00Z'), "
                        "(gen_random_uuid(), :ok, :ws, 'openai', 'gpt', 'review', 2, 2, 0.025, "
                        "'2026-10-27T09:20:00Z'), "
                        "(gen_random_uuid(), :cancelled, :ws, 'openai', 'gpt', 'review', 7, 0, "
                        "0.500, '2026-10-27T11:01:00Z')"
                    ),
                    {"ok": succeeded_with_usage, "cancelled": cancelled_with_usage, "ws": WS_A},
                )
            summary = await GetMetricsSummary(
                SqlAlchemyMetricsSummary(factory, scope), now=lambda: _NOW
            ).execute(scope, range="7d", repo=None)
        finally:
            await engine.dispose()

        statuses = summary.totals.runs_by_status
        assert (statuses.succeeded, statuses.cancelled, statuses.failed) == (2, 1, 1)
        assert (statuses.queued, statuses.running, statuses.skipped) == (0, 0, 0)
        assert (summary.totals.tokens_in, summary.totals.tokens_out) == (12, 3)
        assert summary.totals.cost_usd == Decimal("0.625")
        # 30 and 60 minutes; cancelled and failed runs do not count.
        assert summary.totals.succeeded_average_duration_seconds == 2700.0
        by_day = {item.day.isoformat(): item for item in summary.series}
        assert by_day["2026-10-26"].run_count == 0
        assert (by_day["2026-10-27"].run_count, by_day["2026-10-27"].cost_usd) == (
            3,
            Decimal("0.625"),
        )
        assert by_day["2026-10-27"].average_duration_seconds == 2700.0
        assert (by_day["2026-10-28"].run_count, by_day["2026-10-28"].cost_usd) == (1, Decimal(0))
        assert by_day["2026-10-28"].average_duration_seconds == 0.0

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def test_metrics_summary_matches_pull_verdicts_and_published_findings(
    portal_env: Env,
) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        old = UUID("00000000-0000-4000-8000-000000003231")
        latest = UUID("00000000-0000-4000-8000-000000003232")
        failed = UUID("00000000-0000-4000-8000-000000003233")
        clean = UUID("00000000-0000-4000-8000-000000003234")
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE runs SET created_at = '2020-01-01T00:00:00Z', "
                        "finished_at = '2020-01-01T00:00:00Z'"
                    )
                )
                await _insert_run(
                    session,
                    old,
                    PR_OPEN,
                    "succeeded",
                    datetime(2026, 10, 26, 9, tzinfo=UTC),
                    datetime(2026, 10, 26, 10, tzinfo=UTC),
                    "f" * 64,
                )
                await _insert_run(
                    session,
                    latest,
                    PR_OPEN,
                    "succeeded",
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    datetime(2026, 10, 26, 12, tzinfo=UTC),
                    "g" * 64,
                )
                await _insert_run(
                    session,
                    failed,
                    PR_CLOSED,
                    "failed",
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    datetime(2026, 10, 26, 12, tzinfo=UTC),
                    "h" * 64,
                )
                await _insert_run(
                    session,
                    clean,
                    PR_NEW,
                    "succeeded",
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    datetime(2026, 10, 26, 12, tzinfo=UTC),
                    "i" * 64,
                )
                await _finding(session, old, "critical", "security", published=True)
                await _finding(session, latest, "medium", "correctness", published=True)
                await _finding(session, latest, "critical", "security", published=False)
                await _finding(
                    session, latest, "critical", "security", published=True, drop_reason="duplicate"
                )
                await session.execute(
                    text(
                        "INSERT INTO run_actions (id, run_id, index, tool, request, response, "
                        "started_at, duration_ms) VALUES (:id, :run, 0, 'review.postprocess', "
                        "'{}'::jsonb, CAST(:body AS jsonb), :at, 1)"
                    ),
                    {
                        "id": uuid4(),
                        "run": clean,
                        "body": clean_review_result(HEAD),
                        "at": datetime(2026, 10, 26, 12, tzinfo=UTC),
                    },
                )
            summary = await GetMetricsSummary(
                SqlAlchemyMetricsSummary(factory, scope), now=lambda: _NOW
            ).execute(scope, range="7d", repo=None)
            page = await ListRepositoryPulls(SqlAlchemyPullRequestQueries(factory, scope)).execute(
                REPO_A, state="all", limit=20
            )
        finally:
            await engine.dispose()

        assert page is not None
        verdicts = {
            item.number: None if item.latest_run is None else item.latest_run.verdict
            for item in page.items
        }
        assert verdicts == {1: "attention", 2: None, 3: "clean"}
        buckets = summary.totals.pulls_by_verdict
        assert buckets.attention == 1
        assert buckets.clean == 1
        assert buckets.blocking == 0
        assert buckets.no_verdict == 1
        assert summary.totals.findings_by_severity.critical == 1
        assert summary.totals.findings_by_severity.medium == 1
        assert summary.totals.findings_by_category.security == 1
        assert summary.totals.findings_by_category.correctness == 1
        day = next(item for item in summary.series if item.day.isoformat() == "2026-10-26")
        assert day.findings_by_category.security == 1
        assert day.findings_by_category.correctness == 1

    asyncio.run(
        exercise(),
        loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector()),
    )


def test_metrics_summary_filters_repo_and_pauses_on_the_worker_rule(portal_env: Env) -> None:
    async def exercise() -> None:
        engine = portal_env.engine()
        factory = async_sessionmaker(engine, expire_on_commit=False)
        scope = AuthScope(42, (WS_A,))
        extra_repo = UUID("00000000-0000-4000-8000-000000003241")
        extra_pr = UUID("00000000-0000-4000-8000-000000003242")
        own = UUID("00000000-0000-4000-8000-000000003243")
        extra_run = UUID("00000000-0000-4000-8000-000000003244")
        try:
            async with factory.begin() as session:
                await session.execute(
                    text(
                        "UPDATE workspaces SET name = 'alpha', daily_budget_usd = 1 WHERE id = :id"
                    ),
                    {"id": WS_A},
                )
                await session.execute(
                    text(
                        "UPDATE runs SET created_at = '2020-01-01T00:00:00Z', "
                        "finished_at = '2020-01-01T00:00:00Z'"
                    )
                )
                await session.execute(
                    text(
                        "INSERT INTO repositories (id, provider_installation_id, external_id, "
                        "full_name, default_branch, web_url) VALUES (:id, :installation, 201, "
                        "'octo/extra', 'main', 'https://github.test/octo/extra')"
                    ),
                    {"id": extra_repo, "installation": INSTALL_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO github_user_repository_access (github_user_id, "
                        "provider_installation_id, repository_external_id) "
                        "VALUES (42, :installation, 201)"
                    ),
                    {"installation": INSTALL_A},
                )
                await session.execute(
                    text(
                        "INSERT INTO code_changes (id, repository_id, external_id, "
                        "external_number, title, author_login, source_branch, target_branch, "
                        "base_sha, head_sha, state, web_url, provider_updated_at) "
                        "VALUES (:id, :repo, 950, 9, 'PR', 'octocat', 'feature', 'main', "
                        ":base, :head, 'open', 'https://github.test/pull/9', :updated)"
                    ),
                    {
                        "id": extra_pr,
                        "repo": extra_repo,
                        "base": "b" * 40,
                        "head": HEAD,
                        "updated": datetime(2026, 10, 26, tzinfo=UTC),
                    },
                )
                await _insert_run(
                    session,
                    own,
                    PR_OPEN,
                    "succeeded",
                    datetime(2026, 10, 26, 10, tzinfo=UTC),
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    "j" * 64,
                )
                await _insert_run(
                    session,
                    extra_run,
                    extra_pr,
                    "succeeded",
                    datetime(2026, 10, 26, 10, tzinfo=UTC),
                    datetime(2026, 10, 26, 11, tzinfo=UTC),
                    "k" * 64,
                )
                await _finding(session, own, "medium", "correctness", published=True)
                await _finding(session, extra_run, "critical", "security", published=True)
                await session.execute(
                    text(
                        "INSERT INTO usage_events (id, run_id, workspace_id, provider, model, "
                        "operation, tokens_in, tokens_out, cost_usd, created_at) VALUES "
                        "(:own_id, :own, :ws, 'openai', 'gpt', 'review', 10, 1, 0.100, "
                        "'2026-10-26T10:00:00Z'), "
                        "(:extra_id, :extra, :ws, 'openai', 'gpt', 'review', 1000, 1, 0.510, "
                        "'2026-10-28T01:00:00Z')"
                    ),
                    {
                        "own_id": uuid4(),
                        "extra_id": uuid4(),
                        "own": own,
                        "extra": extra_run,
                        "ws": WS_A,
                    },
                )
            use_case = GetMetricsSummary(SqlAlchemyMetricsSummary(factory, scope), now=lambda: _NOW)
            visible = await use_case.execute(scope, range="7d", repo="octo/a")
            everything = await use_case.execute(scope, range="7d", repo=None)
            foreign = await use_case.execute(scope, range="7d", repo="octo/b")
            missing = await use_case.execute(scope, range="7d", repo="no/such")
            async with factory.begin() as session:
                await session.execute(
                    text("UPDATE workspaces SET daily_budget_usd = 0 WHERE id = :id"),
                    {"id": WS_A},
                )
            unlimited = await use_case.execute(scope, range="7d", repo=None)
        finally:
            await engine.dispose()

        assert visible.totals.tokens_in == 10
        assert everything.totals.tokens_in == 1010
        assert visible.totals.pulls_by_verdict.attention == 1
        assert visible.totals.pulls_by_verdict.blocking == 0
        assert visible.totals.findings_by_severity.medium == 1
        assert visible.totals.findings_by_severity.critical == 0
        assert visible.totals.findings_by_category.correctness == 1
        assert visible.totals.findings_by_category.security == 0
        assert everything.totals.pulls_by_verdict.attention == 1
        assert everything.totals.pulls_by_verdict.blocking == 1
        assert everything.totals.findings_by_severity.critical == 1
        assert everything.totals.findings_by_category.security == 1
        assert [item.name for item in visible.workspaces] == ["alpha"]
        assert visible.workspaces[0].paused is True
        assert visible.workspaces[0].spent_today_usd == Decimal("0.510")
        for empty in (foreign, missing):
            assert empty.totals.tokens_in == 0
            assert empty.totals.runs_by_status.succeeded == 0
            assert empty.totals.pulls_by_verdict.attention == 0
            assert empty.totals.pulls_by_verdict.blocking == 0
            assert empty.totals.findings_by_severity.medium == 0
            assert empty.totals.findings_by_severity.critical == 0
            assert empty.totals.findings_by_category.correctness == 0
            assert empty.totals.findings_by_category.security == 0
            assert all(day.run_count == 0 for day in empty.series)
            assert [item.name for item in empty.workspaces] == ["alpha"]
        assert unlimited.workspaces[0].paused is False
        assert unlimited.workspaces[0].daily_budget_usd == Decimal(0)

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


async def _finding(
    session: object,
    run_id: UUID,
    severity: str,
    category: str,
    *,
    published: bool,
    drop_reason: str | None = None,
) -> None:
    await session.execute(  # type: ignore[attr-defined]
        text(
            "INSERT INTO findings (id, run_id, file_path, line_start, severity, confidence, "
            "category, title, body, published, inline_comment, drop_reason) VALUES "
            "(:id, :run, 'auth.py', 4, :severity, 0.90, :category, 'Title', 'Body', "
            ":published, false, :drop_reason)"
        ),
        {
            "id": uuid4(),
            "run": run_id,
            "severity": severity,
            "category": category,
            "published": published,
            "drop_reason": drop_reason,
        },
    )
