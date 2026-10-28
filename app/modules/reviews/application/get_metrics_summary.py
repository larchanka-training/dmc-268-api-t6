"""Totals, a zero-filled UTC series, and workspace daily budget for visible runs."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Protocol

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.handle_review_run import FAST_RUN_COST_LIMIT
from app.modules.reviews.application.list_pulls import run_verdict
from app.modules.reviews.application.verdict import SEVERITIES

_RANGES = {"24h": timedelta(hours=24), "7d": timedelta(days=7), "30d": timedelta(days=30)}
_CATEGORIES = ("security", "correctness", "performance", "readability")
_STATUSES = ("queued", "running", "publishing", "succeeded", "failed", "cancelled", "skipped")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "skipped"})


@dataclass(frozen=True)
class RunsByStatus:
    queued: int
    running: int
    publishing: int
    succeeded: int
    failed: int
    cancelled: int
    skipped: int


@dataclass(frozen=True)
class PullsByVerdict:
    blocking: int
    attention: int
    clean: int
    no_verdict: int


@dataclass(frozen=True)
class FindingsBySeverity:
    critical: int
    high: int
    medium: int
    low: int
    info: int


@dataclass(frozen=True)
class FindingsByCategory:
    security: int
    correctness: int
    performance: int
    readability: int


@dataclass(frozen=True)
class MetricsTotals:
    runs_by_status: RunsByStatus
    pulls_by_verdict: PullsByVerdict
    findings_by_severity: FindingsBySeverity
    findings_by_category: FindingsByCategory
    succeeded_average_duration_seconds: float
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


@dataclass(frozen=True)
class MetricsDay:
    day: date
    findings_by_category: FindingsByCategory
    run_count: int
    average_duration_seconds: float
    cost_usd: Decimal


@dataclass(frozen=True)
class WorkspaceDailyBudget:
    name: str
    daily_budget_usd: Decimal
    spent_today_usd: Decimal
    paused: bool


@dataclass(frozen=True)
class MetricsSummary:
    range: str
    totals: MetricsTotals
    series: tuple[MetricsDay, ...]
    workspaces: tuple[WorkspaceDailyBudget, ...]


@dataclass(frozen=True)
class TerminalHead:
    """One code change's latest terminal run in the window."""

    status: str
    summary_only: bool
    severities: tuple[str, ...]


@dataclass(frozen=True)
class DayAggregate:
    day: date
    run_count: int
    cost_usd: Decimal
    succeeded_duration_seconds: float
    succeeded_count: int
    findings_by_category: Mapping[str, int]


@dataclass(frozen=True)
class WorkspaceSpend:
    name: str
    daily_budget_usd: Decimal
    spent_today_usd: Decimal


@dataclass(frozen=True)
class MetricsRead:
    status_counts: Mapping[str, int]
    terminal_heads: tuple[TerminalHead, ...]
    findings_by_severity: Mapping[str, int]
    findings_by_category: Mapping[str, int]
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    succeeded_duration_seconds: float
    succeeded_count: int
    days: tuple[DayAggregate, ...]
    workspaces: tuple[WorkspaceSpend, ...]


class MetricsSource(Protocol):
    async def read(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        today_start: datetime,
        repo: str | None,
    ) -> MetricsRead: ...


class GetMetricsSummary:
    """The router supplies ``range``; this use case does not default it."""

    def __init__(
        self,
        source: MetricsSource,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._source = source
        self._now = now

    async def execute(self, scope: AuthScope, *, range: str, repo: str | None) -> MetricsSummary:
        if type(scope) is not AuthScope:
            raise ValueError("GetMetricsSummary requires an AuthScope")
        window = _RANGES.get(range)
        if window is None:
            raise ValueError("range must be 24h, 7d, or 30d")
        end = self._now().astimezone(UTC)
        start = end - window
        read = await self._source.read(
            window_start=start,
            window_end=end,
            today_start=end.replace(hour=0, minute=0, second=0, microsecond=0),
            repo=repo,
        )
        return MetricsSummary(
            range=range,
            totals=_totals(read),
            series=_series(start.date(), end.date(), read.days),
            workspaces=tuple(_workspace(item) for item in read.workspaces),
        )


def _totals(read: MetricsRead) -> MetricsTotals:
    verdicts = {"blocking": 0, "attention": 0, "clean": 0, "no_verdict": 0}
    for head in read.terminal_heads:
        if head.status not in _TERMINAL:
            continue
        bucket = run_verdict(head.status, head.summary_only, head.severities)
        verdicts["no_verdict" if bucket is None else bucket] += 1
    return MetricsTotals(
        runs_by_status=RunsByStatus(
            **{status: read.status_counts.get(status, 0) for status in _STATUSES}
        ),
        pulls_by_verdict=PullsByVerdict(**verdicts),
        findings_by_severity=FindingsBySeverity(
            **{name: read.findings_by_severity.get(name, 0) for name in SEVERITIES}
        ),
        findings_by_category=_categories(read.findings_by_category),
        succeeded_average_duration_seconds=_mean(
            read.succeeded_duration_seconds, read.succeeded_count
        ),
        tokens_in=read.tokens_in,
        tokens_out=read.tokens_out,
        cost_usd=read.cost_usd,
    )


def _series(start: date, end: date, days: tuple[DayAggregate, ...]) -> tuple[MetricsDay, ...]:
    by_day = {item.day: item for item in days}
    series: list[MetricsDay] = []
    day = start
    while day <= end:
        item = by_day.get(day)
        series.append(
            MetricsDay(
                day=day,
                findings_by_category=_categories({} if item is None else item.findings_by_category),
                run_count=0 if item is None else item.run_count,
                average_duration_seconds=(
                    0.0
                    if item is None
                    else _mean(item.succeeded_duration_seconds, item.succeeded_count)
                ),
                cost_usd=Decimal("0") if item is None else item.cost_usd,
            )
        )
        day += timedelta(days=1)
    return tuple(series)


def _workspace(item: WorkspaceSpend) -> WorkspaceDailyBudget:
    return WorkspaceDailyBudget(
        name=item.name,
        daily_budget_usd=item.daily_budget_usd,
        spent_today_usd=item.spent_today_usd,
        paused=(
            item.daily_budget_usd > 0
            and item.daily_budget_usd - item.spent_today_usd < FAST_RUN_COST_LIMIT
        ),
    )


def _categories(counts: Mapping[str, int]) -> FindingsByCategory:
    return FindingsByCategory(**{name: counts.get(name, 0) for name in _CATEGORIES})


def _mean(total: float, count: int) -> float:
    return 0.0 if count == 0 else total / count
