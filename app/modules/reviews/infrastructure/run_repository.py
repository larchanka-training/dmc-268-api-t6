"""SQLAlchemy read projection for review runs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.infrastructure.models import CodeChange, Finding, Run, RunAction


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
        return [self._to_run_list_item(*row) for row in rows]

    async def get_run(self, run_id: UUID) -> RunListItem | None:
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
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        return self._to_run_list_item(*row) if row is not None else None

    async def get_published_comments(self, run_id: UUID) -> list[PublishedComment] | None:
        statement = (
            select(Run.id, Finding)
            .outerjoin(
                Finding,
                and_(Finding.run_id == Run.id, Finding.published.is_(True)),
            )
            .where(Run.id == run_id)
            .order_by(Finding.created_at.asc(), Finding.id.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_published_comment(finding) for _, finding in rows if finding is not None]

    @staticmethod
    def _to_run_list_item(
        run: Run,
        code_change: CodeChange,
        repository_name: str,
        model: str | None,
        action_count: int,
    ) -> RunListItem:
        return RunListItem(
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

    @staticmethod
    def _to_published_comment(finding: Finding) -> PublishedComment:
        return PublishedComment(
            id=finding.id,
            path=finding.file_path,
            old_line=finding.line_start if finding.side.value == "LEFT" else None,
            new_line=finding.line_start if finding.side.value == "RIGHT" else None,
            severity=finding.severity.value,
            category=finding.category.value,
            confidence=finding.confidence,
            title=finding.title,
            body=finding.body,
            suggestion=finding.suggestion,
            rule_name=finding.rule_name,
        )
