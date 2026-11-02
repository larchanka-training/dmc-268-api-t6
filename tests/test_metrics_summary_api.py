from datetime import date
from decimal import Decimal

from app.bootstrap.reviews_api import get_metrics_summary
from app.main import app
from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_metrics_summary import (
    FindingsByCategory,
    FindingsBySeverity,
    MetricsDay,
    MetricsSummary,
    MetricsTotals,
    PullsByVerdict,
    RunsByStatus,
    WorkspaceDailyBudget,
)
from tests.portal_test_client import authenticated_test_client as TestClient

_EMPTY_SEVERITY = FindingsBySeverity(critical=0, high=0, medium=0, low=0, info=0)
_EMPTY_CATEGORY = FindingsByCategory(security=0, correctness=0, performance=0, readability=0)


class _Summary:
    def __init__(self, summary: MetricsSummary) -> None:
        self.summary = summary
        self.calls: list[tuple[AuthScope, str, str | None]] = []

    async def execute(self, scope: AuthScope, *, range: str, repo: str | None) -> MetricsSummary:
        self.calls.append((scope, range, repo))
        return self.summary


def test_metrics_summary_returns_the_camel_case_body() -> None:
    summary = MetricsSummary(
        range="7d",
        totals=MetricsTotals(
            runs_by_status=RunsByStatus(
                queued=0,
                running=0,
                publishing=0,
                succeeded=1,
                failed=0,
                cancelled=0,
                skipped=0,
            ),
            pulls_by_verdict=PullsByVerdict(blocking=0, attention=0, clean=0, no_verdict=1),
            findings_by_severity=_EMPTY_SEVERITY,
            findings_by_category=_EMPTY_CATEGORY,
            succeeded_average_duration_seconds=0.0,
            tokens_in=3,
            tokens_out=5,
            cost_usd=Decimal("0.375"),
        ),
        series=(
            MetricsDay(
                day=date(2026, 10, 26),
                findings_by_category=_EMPTY_CATEGORY,
                run_count=1,
                average_duration_seconds=5.0,
                cost_usd=Decimal("1.5"),
            ),
        ),
        workspaces=(
            WorkspaceDailyBudget(
                name="alpha",
                daily_budget_usd=Decimal("1"),
                spent_today_usd=Decimal("0.5"),
                paused=True,
            ),
        ),
    )
    use_case = _Summary(summary)
    app.dependency_overrides[get_metrics_summary] = lambda: use_case
    try:
        response = TestClient(app).get("/api/metrics/summary?range=7d&repo=octo/a")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "range": "7d",
        "totals": {
            "runsByStatus": {
                "queued": 0,
                "running": 0,
                "publishing": 0,
                "succeeded": 1,
                "failed": 0,
                "cancelled": 0,
                "skipped": 0,
            },
            "pullsByVerdict": {"blocking": 0, "attention": 0, "clean": 0, "noVerdict": 1},
            "findingsBySeverity": {
                "critical": 0,
                "high": 0,
                "medium": 0,
                "low": 0,
                "info": 0,
            },
            "findingsByCategory": {
                "security": 0,
                "correctness": 0,
                "performance": 0,
                "readability": 0,
            },
            "succeededAverageDurationSeconds": 0.0,
            "tokensIn": 3,
            "tokensOut": 5,
            "costUsd": 0.375,
        },
        "series": [
            {
                "day": "2026-10-26",
                "findingsByCategory": {
                    "security": 0,
                    "correctness": 0,
                    "performance": 0,
                    "readability": 0,
                },
                "runCount": 1,
                "averageDurationSeconds": 5.0,
                "costUsd": 1.5,
            }
        ],
        "workspaces": [
            {
                "name": "alpha",
                "dailyBudgetUsd": 1.0,
                "spentTodayUsd": 0.5,
                "paused": True,
            }
        ],
    }
    assert len(use_case.calls) == 1
    scope, window, repo = use_case.calls[0]
    assert isinstance(scope, AuthScope)
    assert window == "7d"
    assert repo == "octo/a"


def test_metrics_summary_rejects_a_range_outside_the_enum() -> None:
    use_case = _Summary(
        MetricsSummary(
            range="24h",
            totals=MetricsTotals(
                runs_by_status=RunsByStatus(0, 0, 0, 0, 0, 0, 0),
                pulls_by_verdict=PullsByVerdict(0, 0, 0, 0),
                findings_by_severity=_EMPTY_SEVERITY,
                findings_by_category=_EMPTY_CATEGORY,
                succeeded_average_duration_seconds=0.0,
                tokens_in=0,
                tokens_out=0,
                cost_usd=Decimal("0"),
            ),
            series=(),
            workspaces=(),
        )
    )
    app.dependency_overrides[get_metrics_summary] = lambda: use_case
    try:
        response = TestClient(app).get("/api/metrics/summary?range=1h")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert use_case.calls == []
