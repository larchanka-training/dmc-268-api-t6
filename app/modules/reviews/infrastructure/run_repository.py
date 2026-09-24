"""SQLAlchemy read projection for review runs."""

from __future__ import annotations

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.infrastructure.models import CodeChange, Run, RunAction


class SqlAlchemyRunRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_runs(
        self,
        *,
        status: str | None,
        repository: str | None,
        cursor: RunCursor | None,
        limit: int,
    ) -> list[RunListItem]:
        latest_model = (
            select(UsageEvent.model)
            .where(UsageEvent.run_id == Run.id)
            .order_by(UsageEvent.created_at.desc(), UsageEvent.id.desc())
            .limit(1)
            .scalar_subquery()
        )
        action_count = (
            select(func.count())
            .select_from(RunAction)
            .where(RunAction.run_id == Run.id)
            .scalar_subquery()
        )
        statement = (
            select(Run, CodeChange, Repository.full_name, latest_model, action_count)
            .join(CodeChange, Run.code_change_id == CodeChange.id)
            .join(Repository, CodeChange.repository_id == Repository.id)
            .order_by(Run.created_at.desc(), Run.id.desc())
            .limit(limit)
        )
        if status is not None:
            statement = statement.where(Run.state == status)
        if repository is not None:
            statement = statement.where(Repository.full_name == repository)
        if cursor is not None:
            statement = statement.where(
                or_(
                    Run.created_at < cursor.created_at,
                    and_(Run.created_at == cursor.created_at, Run.id < cursor.id),
                )
            )

        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        return [
            RunListItem(
                id=run.id,
                status=run.state.value,
                engine=run.engine.value,
                attempt=run.attempt,
                cancel_requested=run.cancel_requested,
                started_at=run.started_at,
                finished_at=run.finished_at,
                error_code=run.error_code,
                model=model,
                action_count=action_count,
                repo=repository_name,
                number=code_change.external_number,
                title=code_change.title,
                url=code_change.web_url,
                head_sha=run.head_sha,
                created_at=run.created_at,
            )
            for run, code_change, repository_name, model, action_count in rows
        ]
