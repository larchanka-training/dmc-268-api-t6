"""SQLAlchemy read projection for review runs."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.get_run_actions import RunAction as RunActionProjection
from app.modules.reviews.application.get_run_actions import RunActionResponse
from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.list_runs import RunCursor, RunListItem
from app.modules.reviews.application.process_run import RunDiffInput
from app.modules.reviews.infrastructure.models import (
    CodeChange,
    CodeChangeDiff,
    Finding,
    Run,
    RunAction,
)


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

    async def get_run_actions(self, run_id: UUID) -> list[RunActionProjection] | None:
        statement = (
            select(Run.id, RunAction)
            .outerjoin(RunAction, RunAction.run_id == Run.id)
            .where(Run.id == run_id)
            .order_by(RunAction.index.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [self._to_run_action(action) for _, action in rows if action is not None]

    async def get_run_action_response(self, run_id: UUID, index: int) -> RunActionResponse | None:
        statement = (
            select(RunAction.response)
            .join(Run, RunAction.run_id == Run.id)
            .where(Run.id == run_id, RunAction.index == index)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunActionResponse(response=row[0])

    async def get_run_diff(self, run_id: UUID) -> list[DiffSnapshot] | None:
        statement = (
            select(Run.id, CodeChangeDiff.filename, CodeChangeDiff.patch)
            .outerjoin(
                CodeChangeDiff,
                and_(
                    CodeChangeDiff.code_change_id == Run.code_change_id,
                    CodeChangeDiff.head_sha == Run.head_sha,
                ),
            )
            .where(Run.id == run_id)
            .order_by(CodeChangeDiff.filename.asc())
        )
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
        if not rows:
            return None
        return [
            DiffSnapshot(filename=filename, patch=patch)
            for _, filename, patch in rows
            if filename is not None
        ]

    async def replace_diff_snapshots(
        self, code_change_id: UUID, head_sha: str, snapshots: list[DiffSnapshot]
    ) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                delete(CodeChangeDiff).where(
                    CodeChangeDiff.code_change_id == code_change_id,
                    CodeChangeDiff.head_sha == head_sha,
                )
            )
            session.add_all(
                [
                    CodeChangeDiff(
                        code_change_id=code_change_id,
                        head_sha=head_sha,
                        filename=snapshot.filename,
                        patch=snapshot.patch,
                    )
                    for snapshot in snapshots
                ]
            )

    async def get_run_diff_input(self, run_id: UUID) -> RunDiffInput | None:
        statement = select(Run.code_change_id, Run.head_sha).where(Run.id == run_id)
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return RunDiffInput(code_change_id=row[0], head_sha=row[1])

    async def get_run_file_key(self, run_id: UUID, path: str) -> BlobCacheKey | None:
        statement = (
            select(Run.code_change_id, Run.head_sha)
            .join(
                CodeChangeDiff,
                and_(
                    CodeChangeDiff.code_change_id == Run.code_change_id,
                    CodeChangeDiff.head_sha == Run.head_sha,
                    CodeChangeDiff.filename == path,
                ),
            )
            .where(Run.id == run_id)
        )
        async with self._session_factory() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        return BlobCacheKey(code_change_id=row[0], head_sha=row[1], path=path)

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

    @staticmethod
    def _to_run_action(action: RunAction) -> RunActionProjection:
        return RunActionProjection(
            index=action.index,
            tool=action.tool,
            request=action.request,
            response=action.response,
            response_ref=action.response_ref,
            started_at=action.started_at,
            duration_ms=action.duration_ms,
        )
