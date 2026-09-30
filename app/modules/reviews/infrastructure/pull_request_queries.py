"""Read model of a repository's pull requests with their latest run (api#20 D11)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.list_pulls import LatestRunRow, PullRequestRow
from app.modules.reviews.application.list_runs import RunCursor
from app.modules.reviews.infrastructure.models import CodeChange, CodeChangeDiff, Finding, Run
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate

_STATES = {
    "open": (CodeChangeState.OPEN,),
    "closed": (CodeChangeState.CLOSED, CodeChangeState.MERGED),
    "all": tuple(CodeChangeState),
}


class SqlAlchemyPullRequestQueries:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], scope: AuthScope | None = None
    ) -> None:
        self._session_factory = session_factory
        self._scope = scope

    async def list_pulls(
        self, repository_id: UUID, *, state: str, cursor: RunCursor | None, limit: int
    ) -> list[PullRequestRow] | None:
        visible = true() if self._scope is None else repository_access_predicate(self._scope)
        updated_at = func.coalesce(CodeChange.provider_updated_at, CodeChange.updated_at)
        statement = (
            select(CodeChange, updated_at)
            .where(CodeChange.repository_id == repository_id, CodeChange.state.in_(_STATES[state]))
            .order_by(updated_at.desc(), CodeChange.id.desc())
            .limit(limit)
        )
        if cursor is not None:
            statement = statement.where(
                or_(
                    updated_at < cursor.created_at,
                    and_(updated_at == cursor.created_at, CodeChange.id < cursor.id),
                )
            )
        async with self._session_factory() as session:
            if (
                await session.scalar(
                    select(Repository.id).where(Repository.id == repository_id, visible)
                )
                is None
            ):
                return None
            rows = (await session.execute(statement)).all()
            latest = await self._latest_runs(session, [pr.id for pr, _ in rows])
        return [
            PullRequestRow(
                id=pr.id,
                number=pr.external_number,
                title=pr.title,
                url=pr.web_url,
                author=pr.author_login,
                head_sha=pr.head_sha,
                updated_at=updated,
                latest_run=latest.get(pr.id),
            )
            for pr, updated in rows
        ]

    @staticmethod
    async def _latest_runs(session: AsyncSession, pull_ids: list[UUID]) -> dict[UUID, LatestRunRow]:
        if not pull_ids:
            return {}
        runs = (
            await session.execute(
                select(Run.code_change_id, Run.id, Run.state)
                .where(Run.code_change_id.in_(pull_ids))
                .order_by(Run.code_change_id, Run.created_at.desc(), Run.id.desc())
                .distinct(Run.code_change_id)
            )
        ).all()
        run_ids = [run_id for _, run_id, _ in runs]
        severities: dict[UUID, list[str]] = {}
        for run_id, severity in await session.execute(
            select(Finding.run_id, Finding.severity).where(
                Finding.run_id.in_(run_ids),
                Finding.published.is_(True),
                Finding.drop_reason.is_(None),
            )
        ):
            severities.setdefault(run_id, []).append(severity.value)
        summary_only = set(
            await session.scalars(
                select(CodeChangeDiff.run_id)
                .where(CodeChangeDiff.run_id.in_(run_ids), CodeChangeDiff.summary_only.is_(True))
                .distinct()
            )
        )
        return {
            pull_id: LatestRunRow(
                id=run_id,
                status=state.value,
                summary_only=run_id in summary_only,
                published_severities=tuple(severities.get(run_id, ())),
            )
            for pull_id, run_id, state in runs
        }
