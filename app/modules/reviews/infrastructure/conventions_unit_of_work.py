"""SQLAlchemy transaction composition for repository conventions."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import RepoConventions
from app.modules.reviews.application.conventions import (
    CachedConventions,
    ConventionsFile,
)
from app.modules.reviews.infrastructure.run_action_records import insert_run_action


class SqlAlchemyRepositoryConventionsStore:
    """Session-bound adapter; it only flushes and never owns a commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get(
        self,
        repository_id: UUID,
        agents_md_sha: str | None,
        prompt_version_id: UUID,
        policy_key: str | None = None,
    ) -> CachedConventions | None:
        result = await self._session.execute(
            select(RepoConventions).where(
                RepoConventions.repository_id == repository_id,
                # ``==`` with None is rendered as IS NULL, which is the wanted match.
                RepoConventions.agents_md_sha == agents_md_sha,
                RepoConventions.prompt_version_id == prompt_version_id,
                RepoConventions.policy_key == policy_key,
            )
        )
        item = result.scalar_one_or_none()
        if item is None:
            return None
        conventions = item
        return CachedConventions(
            repository_id=conventions.repository_id,
            agents_md_sha=conventions.agents_md_sha,
            prompt_version_id=conventions.prompt_version_id,
            key_patterns=tuple(conventions.key_patterns),
            recommendations=tuple(conventions.recommendations),
            languages=dict(conventions.languages),
            policy_key=conventions.policy_key,
        )

    async def save_and_record_trace(
        self,
        run_id: UUID,
        conventions: CachedConventions,
        trace_files: tuple[ConventionsFile, ...],
    ) -> CachedConventions:
        key = (
            conventions.repository_id,
            conventions.agents_md_sha,
            conventions.prompt_version_id,
            conventions.policy_key,
        )
        saved = await self.get(*key)
        if saved is None:
            # Another Run of this repository may have written the key since the read: its row
            # wins, as if the read had found it. The insert waits for that Run's commit.
            inserted = await self._session.scalar(
                insert(RepoConventions)
                .values(
                    repository_id=conventions.repository_id,
                    agents_md_sha=conventions.agents_md_sha,
                    prompt_version_id=conventions.prompt_version_id,
                    policy_key=conventions.policy_key,
                    agents_md=None,
                    key_patterns=list(conventions.key_patterns),
                    recommendations=list(conventions.recommendations),
                    languages=conventions.languages,
                )
                .on_conflict_do_nothing(constraint="uq_repo_conventions_snapshot")
                .returning(RepoConventions.id)
            )
            winner = None if inserted is not None else await self.get(*key)
            saved = winner or conventions
        await insert_run_action(
            self._session,
            run_id=run_id,
            tool="llm.repo_conventions",
            request={},
            response={"files": [file.model_dump() for file in trace_files]},
            started_at=datetime.now(UTC),
            duration_ms=0,
        )
        return saved


class SqlAlchemyRepositoryConventionsUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose cache and trace persistence in one explicit transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def conventions(self) -> SqlAlchemyRepositoryConventionsStore:
        return SqlAlchemyRepositoryConventionsStore(self.session)
