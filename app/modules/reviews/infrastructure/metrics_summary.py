"""Aggregate metrics for repositories the caller can see. Reads only; no commit."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, String, and_, cast, func, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.sql.elements import ColumnElement

from app.common.infrastructure.db.enums import RunState
from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.get_metrics_summary import (
    DayAggregate,
    MetricsRead,
    TerminalHead,
    WorkspaceSpend,
)
from app.modules.reviews.infrastructure.models import CodeChange, Finding, Run
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.modules.reviews.infrastructure.saved_review_results import load_review_results
from app.modules.workspaces.infrastructure.models import GitHubUserWorkspaceAccess, Workspace
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate

_TERMINAL = (
    RunState.SUCCEEDED,
    RunState.FAILED,
    RunState.CANCELLED,
    RunState.SKIPPED,
)


class SqlAlchemyMetricsSummary:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> None:
        if scope is None and not allow_unscoped:
            raise ValueError(
                "SqlAlchemyMetricsSummary requires an AuthScope unless allow_unscoped=True"
            )
        self._session_factory = session_factory
        self._scope = scope

    async def read(
        self,
        *,
        window_start: datetime,
        window_end: datetime,
        today_start: datetime,
        repo: str | None,
    ) -> MetricsRead:
        async with self._session_factory() as session:
            runs = await self._runs(session, window_start, window_end, repo)
            heads = await self._terminal_heads(session, window_start, window_end, repo)
            severities, categories = await self._finding_counts(
                session, window_start, window_end, repo
            )
            day_categories = await self._day_categories(session, window_start, window_end, repo)
            workspaces = await self._workspaces(session, today_start)
        return MetricsRead(
            status_counts=dict(runs.status_counts),
            terminal_heads=heads,
            findings_by_severity=severities,
            findings_by_category=categories,
            tokens_in=runs.tokens_in,
            tokens_out=runs.tokens_out,
            cost_usd=runs.cost_usd,
            succeeded_duration_seconds=float(runs.duration),
            succeeded_count=runs.succeeded,
            days=tuple(
                DayAggregate(
                    item_day,
                    day.run_count,
                    day.cost_usd,
                    float(day.duration),
                    day.succeeded,
                    day_categories.get(item_day, {}),
                )
                for item_day, day in runs.days.items()
            ),
            workspaces=workspaces,
        )

    def _visible(
        self, columns: tuple[Any, ...], start: datetime, end: datetime, repo: str | None
    ) -> Any:
        statement = (
            select(*columns)
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .where(Run.created_at >= start, Run.created_at <= end, self._access())
        )
        if repo is not None:
            statement = statement.where(Repository.full_name == repo)
        return statement

    def _access(self) -> ColumnElement[bool]:
        if self._scope is None:
            return true()
        return repository_access_predicate(self._scope)

    async def _runs(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> _RunTotals:
        """Statuses, spend and durations of the window in one pass, by UTC day and state."""
        day = _utc_day()
        # One index lookup per run for all three sums, not three correlated subqueries.
        usage = (
            select(
                func.sum(UsageEvent.tokens_in).label("tokens_in"),
                func.sum(UsageEvent.tokens_out).label("tokens_out"),
                func.sum(UsageEvent.cost_usd).label("cost_usd"),
            )
            .where(UsageEvent.run_id == Run.id)
            .lateral("usage")
        )
        rows = (
            await session.execute(
                self._visible(
                    (
                        day,
                        Run.state,
                        func.count(),
                        func.sum(usage.c.tokens_in),
                        func.sum(usage.c.tokens_out),
                        func.sum(usage.c.cost_usd),
                        func.sum(func.extract("epoch", Run.finished_at - Run.created_at)),
                    ),
                    start,
                    end,
                    repo,
                )
                .outerjoin(usage, true())
                .group_by(day, Run.state)
            )
        ).all()
        totals = _RunTotals()
        for item_day, state, count, tokens_in, tokens_out, cost, duration in rows:
            totals.add(
                _as_date(item_day),
                _state_name(state),
                int(count),
                int(tokens_in or 0),
                int(tokens_out or 0),
                Decimal(cost or 0),
                # numeric, summed exactly; only succeeded runs count toward the average.
                Decimal(duration or 0),
            )
        return totals

    async def _terminal_heads(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> tuple[TerminalHead, ...]:
        latest = (
            self._visible(
                (
                    Run.id,
                    Run.state,
                    Run.head_sha,
                    SqlAlchemyRunRepository._summary_only_projection(),
                ),
                start,
                end,
                repo,
            )
            .where(Run.state.in_(_TERMINAL))
            .distinct(Run.code_change_id)
            .order_by(Run.code_change_id, Run.finished_at.desc().nulls_last(), Run.id.desc())
        ).subquery("latest_terminal")
        rows = (
            await session.execute(
                select(
                    latest.c.id,
                    latest.c.state,
                    latest.c.head_sha,
                    latest.c.summary_only,
                    cast(Finding.severity, String),
                )
                .select_from(latest)
                .outerjoin(
                    Finding,
                    and_(
                        Finding.run_id == latest.c.id,
                        Finding.published.is_(True),
                        Finding.drop_reason.is_(None),
                    ),
                )
            )
        ).all()
        grouped: dict[UUID, list[Any]] = {}
        for run_id, state, head_sha, summary_only, severity in rows:
            head = grouped.get(run_id)
            if head is None:
                head = [_state_name(state), str(head_sha), bool(summary_only), []]
                grouped[run_id] = head
            if severity is not None:
                head[3].append(severity)
        results = await load_review_results(
            session,
            {
                run_id: item[1]
                for run_id, item in grouped.items()
                if item[0] == RunState.SUCCEEDED.value
            },
            {run_id: item[3] for run_id, item in grouped.items()},
        )
        return tuple(
            TerminalHead(item[0], item[2], tuple(item[3]), results.get(run_id))
            for run_id, item in grouped.items()
        )

    async def _finding_counts(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> tuple[dict[str, int], dict[str, int]]:
        rows = (
            await session.execute(
                self._visible(
                    (cast(Finding.severity, String), cast(Finding.category, String), func.count()),
                    start,
                    end,
                    repo,
                )
                .join(
                    Finding,
                    and_(
                        Finding.run_id == Run.id,
                        Finding.published.is_(True),
                        Finding.drop_reason.is_(None),
                    ),
                )
                .group_by(Finding.severity, Finding.category)
            )
        ).all()
        severities: dict[str, int] = defaultdict(int)
        categories: dict[str, int] = defaultdict(int)
        for severity, category, count in rows:
            severities[severity] += int(count)
            categories[category] += int(count)
        return dict(severities), dict(categories)

    async def _day_categories(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> dict[date, dict[str, int]]:
        category_day = _utc_day()
        categories = (
            await session.execute(
                self._visible(
                    (category_day, cast(Finding.category, String), func.count()),
                    start,
                    end,
                    repo,
                )
                .join(
                    Finding,
                    and_(
                        Finding.run_id == Run.id,
                        Finding.published.is_(True),
                        Finding.drop_reason.is_(None),
                    ),
                )
                .group_by(category_day, Finding.category)
            )
        ).all()
        by_day: dict[date, dict[str, int]] = defaultdict(dict)
        for item_day, category, count in categories:
            by_day[_as_date(item_day)][category] = int(count)
        return by_day

    async def _workspaces(
        self, session: AsyncSession, today_start: datetime
    ) -> tuple[WorkspaceSpend, ...]:
        if self._scope is None:
            return ()
        spent = func.coalesce(func.sum(UsageEvent.cost_usd), 0)
        rows = (
            await session.execute(
                select(Workspace.name, Workspace.daily_budget_usd, spent)
                .join(
                    GitHubUserWorkspaceAccess,
                    and_(
                        GitHubUserWorkspaceAccess.workspace_id == Workspace.id,
                        GitHubUserWorkspaceAccess.github_user_id == self._scope.user_id,
                    ),
                )
                .outerjoin(
                    UsageEvent,
                    and_(
                        UsageEvent.workspace_id == Workspace.id,
                        UsageEvent.created_at >= today_start,
                    ),
                )
                .where(Workspace.id.in_(self._scope.workspace_ids))
                .group_by(Workspace.id, Workspace.name, Workspace.daily_budget_usd)
                .order_by(Workspace.name)
            )
        ).all()
        return tuple(
            WorkspaceSpend(name, Decimal(budget), Decimal(spent_today))
            for name, budget, spent_today in rows
        )


def _utc_day() -> ColumnElement[Any]:
    return cast(func.timezone("UTC", Run.created_at), Date).label("day")


def _as_date(value: date | datetime) -> date:
    return value.date() if isinstance(value, datetime) else value


@dataclass
class _DayRuns:
    run_count: int = 0
    cost_usd: Decimal = Decimal(0)
    duration: Decimal = Decimal(0)
    succeeded: int = 0


@dataclass
class _RunTotals:
    """Sums of the (day, state) groups; durations stay numeric until the very end."""

    status_counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: Decimal = Decimal(0)
    duration: Decimal = Decimal(0)
    succeeded: int = 0
    days: dict[date, _DayRuns] = field(default_factory=lambda: defaultdict(_DayRuns))

    def add(
        self,
        day: date,
        state: str,
        count: int,
        tokens_in: int,
        tokens_out: int,
        cost: Decimal,
        duration: Decimal,
    ) -> None:
        self.status_counts[state] += count
        self.tokens_in += tokens_in
        self.tokens_out += tokens_out
        self.cost_usd += cost
        item = self.days[day]
        item.run_count += count
        item.cost_usd += cost
        if state == RunState.SUCCEEDED.value:
            self.duration += duration
            self.succeeded += count
            item.duration += duration
            item.succeeded += count


def _state_name(state: Any) -> str:
    return state.value if isinstance(state, RunState) else str(state)
