"""Metrics summary buckets verdicts, pause, and a zero-filled UTC series."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

import pytest

from app.modules.auth.application.scope import AuthScope
from app.modules.reviews.application.get_metrics_summary import (
    DayAggregate,
    GetMetricsSummary,
    MetricsRead,
    TerminalHead,
    WorkspaceSpend,
)
from app.modules.reviews.infrastructure.metrics_summary import SqlAlchemyMetricsSummary

_NOW = datetime(2026, 10, 28, 15, tzinfo=UTC)
_SCOPE = AuthScope(42, (UUID("00000000-0000-4000-8000-00000000000a"),))
_DAYS = (
    "2026-10-21",
    "2026-10-22",
    "2026-10-23",
    "2026-10-24",
    "2026-10-25",
    "2026-10-26",
    "2026-10-27",
    "2026-10-28",
)


def test_seven_day_summary_zero_fills_buckets_verdicts_and_pauses() -> None:
    source = _Source(
        _read(
            status_counts={"succeeded": 2},
            terminal_heads=(
                TerminalHead("succeeded", True, ()),
                TerminalHead("succeeded", False, ("critical",)),
            ),
            findings_by_severity={"critical": 1},
            findings_by_category={"security": 1},
            tokens_in=3,
            tokens_out=5,
            cost_usd=Decimal("0.375"),
            succeeded_duration_seconds=10.0,
            succeeded_count=2,
            days=(
                DayAggregate(
                    date(2026, 10, 26),
                    2,
                    Decimal("1.5"),
                    10.0,
                    2,
                    {"security": 3},
                ),
            ),
            workspaces=(
                WorkspaceSpend("unset", Decimal("0"), Decimal("10")),
                WorkspaceSpend("tight", Decimal("1"), Decimal("0.60")),
                WorkspaceSpend("room", Decimal("1"), Decimal("0.40")),
            ),
        )
    )
    summary = asyncio.run(
        GetMetricsSummary(source, now=lambda: _NOW).execute(_SCOPE, range="7d", repo=None)
    )

    assert source.calls == [
        (
            datetime(2026, 10, 21, 15, tzinfo=UTC),
            _NOW,
            datetime(2026, 10, 28, tzinfo=UTC),
            None,
        )
    ]
    assert summary.range == "7d"
    assert [day.day.isoformat() for day in summary.series] == list(_DAYS)
    filled = summary.series[5]
    assert filled.day == date(2026, 10, 26)
    assert filled.run_count == 2
    assert filled.cost_usd == Decimal("1.5")
    assert filled.average_duration_seconds == 5.0
    assert filled.findings_by_category.security == 3
    assert filled.findings_by_category.correctness == 0
    quiet = summary.series[6]
    assert quiet.day == date(2026, 10, 27)
    assert quiet.run_count == 0
    assert quiet.cost_usd == Decimal("0")
    assert quiet.average_duration_seconds == 0.0
    assert quiet.findings_by_category.security == 0
    status = summary.totals.runs_by_status
    assert status.succeeded == 2
    assert status.queued == 0
    assert status.running == 0
    assert status.publishing == 0
    assert status.failed == 0
    assert status.cancelled == 0
    assert status.skipped == 0
    verdicts = summary.totals.pulls_by_verdict
    assert verdicts.no_verdict == 1
    assert verdicts.blocking == 1
    assert verdicts.attention == 0
    assert verdicts.clean == 0
    assert summary.totals.findings_by_severity.critical == 1
    assert summary.totals.findings_by_severity.high == 0
    assert summary.totals.findings_by_category.security == 1
    assert summary.totals.findings_by_category.readability == 0
    assert summary.totals.tokens_in == 3
    assert summary.totals.tokens_out == 5
    assert summary.totals.cost_usd == Decimal("0.375")
    assert summary.totals.succeeded_average_duration_seconds == 5.0
    paused = {item.name: item.paused for item in summary.workspaces}
    assert paused == {"unset": False, "tight": True, "room": False}


def test_invisible_repo_is_empty_and_omitted_repo_keeps_the_caller_workspace() -> None:
    caller = WorkspaceSpend("mine", Decimal("1"), Decimal("0.40"))
    source = _Source(
        _read(),
        by_repo={
            "secret/other": _read(workspaces=(caller,)),
            None: _read(status_counts={"succeeded": 1}, workspaces=(caller,)),
        },
    )
    use_case = GetMetricsSummary(source, now=lambda: _NOW)

    hidden = asyncio.run(use_case.execute(_SCOPE, range="7d", repo="secret/other"))
    visible = asyncio.run(use_case.execute(_SCOPE, range="7d", repo=None))

    assert [day.day.isoformat() for day in hidden.series] == list(_DAYS)
    assert hidden.totals.runs_by_status.succeeded == 0
    assert hidden.totals.tokens_in == 0
    assert hidden.totals.cost_usd == Decimal("0")
    assert hidden.series[0].run_count == 0
    assert [item.name for item in hidden.workspaces] == ["mine"]
    assert "other-user" not in [item.name for item in hidden.workspaces]
    assert visible.totals.runs_by_status.succeeded == 1
    assert [item.name for item in visible.workspaces] == ["mine"]
    assert source.calls[0][3] == "secret/other"
    assert source.calls[1][3] is None


def test_range_outside_the_three_windows_is_rejected() -> None:
    use_case = GetMetricsSummary(_Source(_read()), now=lambda: _NOW)

    with pytest.raises(ValueError, match="24h"):
        asyncio.run(use_case.execute(_SCOPE, range="1h", repo=None))


def test_execute_rejects_a_scope_that_is_not_an_auth_scope() -> None:
    use_case = GetMetricsSummary(_Source(_read()), now=lambda: _NOW)

    with pytest.raises(ValueError, match="AuthScope"):
        asyncio.run(use_case.execute(cast(Any, None), range="7d", repo=None))


def test_sql_metrics_summary_requires_a_scope() -> None:
    with pytest.raises(ValueError, match="allow_unscoped=True"):
        SqlAlchemyMetricsSummary(cast(Any, None))
    SqlAlchemyMetricsSummary(cast(Any, None), allow_unscoped=True)


def _read(**overrides: object) -> MetricsRead:
    read = MetricsRead(
        status_counts={},
        terminal_heads=(),
        findings_by_severity={},
        findings_by_category={},
        tokens_in=0,
        tokens_out=0,
        cost_usd=Decimal("0"),
        succeeded_duration_seconds=0.0,
        succeeded_count=0,
        days=(),
        workspaces=(),
    )
    return MetricsRead(**{**read.__dict__, **overrides})


@dataclass
class _Source:
    read_model: MetricsRead
    by_repo: dict[str | None, MetricsRead] | None = None
    calls: list[tuple[datetime, datetime, datetime, str | None]] = field(default_factory=list)

    async def read(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        today_start: datetime,
        repo: str | None,
    ) -> MetricsRead:
        self.calls.append((window_start, window_end, today_start, repo))
        if self.by_repo is not None:
            return self.by_repo[repo]
        return self.read_model
