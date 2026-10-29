"""Aggregate metrics for repositories the caller can see. Reads only; no commit."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Date, String, and_, case, cast, func, select, true
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
            status_counts = await self._status_counts(session, window_start, window_end, repo)
            heads = await self._terminal_heads(session, window_start, window_end, repo)
            severities, categories = await self._finding_counts(
                session, window_start, window_end, repo
            )
            tokens_in, tokens_out, cost, duration, succeeded = await self._usage_and_duration(
                session, window_start, window_end, repo
            )
            days = await self._days(session, window_start, window_end, repo)
            workspaces = await self._workspaces(session, today_start)
        return MetricsRead(
            status_counts=status_counts,
            terminal_heads=heads,
            findings_by_severity=severities,
            findings_by_category=categories,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            cost_usd=cost,
            succeeded_duration_seconds=duration,
            succeeded_count=succeeded,
            days=days,
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

    async def _status_counts(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> dict[str, int]:
        rows = (
            await session.execute(
                self._visible((Run.state, func.count()), start, end, repo).group_by(Run.state)
            )
        ).all()
        return {_state_name(state): int(count) for state, count in rows}

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

    async def _usage_and_duration(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> tuple[int, int, Decimal, float, int]:
        tokens_in, tokens_out, cost = _event_sums()
        duration, succeeded = _succeeded_duration()
        row = (
            await session.execute(
                self._visible(
                    (
                        func.sum(tokens_in),
                        func.sum(tokens_out),
                        func.sum(cost),
                        duration,
                        succeeded,
                    ),
                    start,
                    end,
                    repo,
                )
            )
        ).one()
        return (
            int(row[0] or 0),
            int(row[1] or 0),
            Decimal(row[2] or 0),
            float(row[3] or 0),
            int(row[4] or 0),
        )

    async def _days(
        self, session: AsyncSession, start: datetime, end: datetime, repo: str | None
    ) -> tuple[DayAggregate, ...]:
        day = _utc_day()
        tokens_in, tokens_out, cost = _event_sums()
        del tokens_in, tokens_out
        duration, succeeded = _succeeded_duration()
        rows = (
            await session.execute(
                self._visible(
                    (day, func.count(), func.coalesce(func.sum(cost), 0), duration, succeeded),
                    start,
                    end,
                    repo,
                ).group_by(day)
            )
        ).all()
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
        return tuple(
            DayAggregate(
                _as_date(item_day),
                int(run_count),
                Decimal(item_cost),
                float(item_duration or 0),
                int(item_succeeded or 0),
                by_day.get(_as_date(item_day), {}),
            )
            for item_day, run_count, item_cost, item_duration, item_succeeded in rows
        )

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


def _succeeded_duration() -> tuple[ColumnElement[Any], ColumnElement[Any]]:
    duration = func.coalesce(
        func.sum(
            case(
                (
                    Run.state == RunState.SUCCEEDED,
                    func.extract("epoch", Run.finished_at - Run.created_at),
                ),
                else_=0,
            )
        ),
        0,
    )
    return duration, func.count().filter(Run.state == RunState.SUCCEEDED)


def _event_sums() -> tuple[ColumnElement[Any], ColumnElement[Any], ColumnElement[Any]]:
    def total(column: Any) -> ColumnElement[Any]:
        return (
            select(func.coalesce(func.sum(column), 0))
            .where(UsageEvent.run_id == Run.id)
            .correlate(Run)
            .scalar_subquery()
        )

    return total(UsageEvent.tokens_in), total(UsageEvent.tokens_out), total(UsageEvent.cost_usd)


def _state_name(state: Any) -> str:
    return state.value if isinstance(state, RunState) else str(state)
