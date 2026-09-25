"""SQLAlchemy transaction composition for repository conventions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import RepoConventionDraft, RepoConventions
from app.modules.reviews.application.conventions import (
    CachedConventions,
    ConventionsFile,
)
from app.modules.reviews.infrastructure.models import RunAction


class SqlAlchemyRepositoryConventionsStore:
    """Session-bound adapter; it only flushes and never owns a commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self, repository_id: UUID, agents_md_sha: str | None, prompt_version_id: UUID
    ) -> CachedConventions | None:
        row = await self._session.execute(
            select(RepoConventions, RepoConventionDraft)
            .outerjoin(
                RepoConventionDraft,
                RepoConventionDraft.repo_conventions_id == RepoConventions.id,
            )
            .where(
                RepoConventions.repository_id == repository_id,
                RepoConventions.agents_md_sha == agents_md_sha,
                RepoConventions.prompt_version_id == prompt_version_id,
            )
        )
        item = row.one_or_none()
        if item is None:
            return None
        conventions, draft = item
        if draft is None:
            raise RuntimeError("cached conventions are missing their draft trace")
        return CachedConventions(
            repository_id=conventions.repository_id,
            agents_md_sha=conventions.agents_md_sha,
            prompt_version_id=conventions.prompt_version_id,
            key_patterns=tuple(conventions.key_patterns),
            recommendations=tuple(conventions.recommendations),
            languages=dict(conventions.languages),
            draft_files=tuple(ConventionsFile.model_validate(file) for file in draft.files),
        )

    async def save_and_record_trace(
        self, run_id: UUID, conventions: CachedConventions
    ) -> CachedConventions:
        existing = await self.get(
            conventions.repository_id, conventions.agents_md_sha, conventions.prompt_version_id
        )
        saved = existing
        if saved is None:
            row = RepoConventions(
                repository_id=conventions.repository_id,
                agents_md_sha=conventions.agents_md_sha,
                prompt_version_id=conventions.prompt_version_id,
                agents_md=None,
                key_patterns=list(conventions.key_patterns),
                recommendations=list(conventions.recommendations),
                languages=conventions.languages,
            )
            self._session.add(row)
            await self._session.flush()
            self._session.add(
                RepoConventionDraft(
                    repo_conventions_id=row.id,
                    files=[file.model_dump() for file in conventions.draft_files],
                )
            )
            saved = conventions
        index = await self._session.scalar(
            select(func.coalesce(func.max(RunAction.index), -1)).where(RunAction.run_id == run_id)
        )
        assert index is not None
        self._session.add(
            RunAction(
                run_id=run_id,
                index=index + 1,
                tool="llm.repo_conventions",
                request={},
                response={"files": [file.model_dump() for file in saved.draft_files]},
                response_ref=None,
                started_at=datetime.now(UTC),
                duration_ms=0,
            )
        )
        await self._session.flush()
        return saved


class SqlAlchemyRepositoryConventionsUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose cache and trace persistence in one explicit transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def conventions(self) -> SqlAlchemyRepositoryConventionsStore:
        return SqlAlchemyRepositoryConventionsStore(self.session)
