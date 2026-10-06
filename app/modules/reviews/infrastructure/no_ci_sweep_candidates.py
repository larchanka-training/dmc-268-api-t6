"""PostgreSQL port for the #11 no-CI sweep candidate selection."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timedelta

from sqlalchemy import exists, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState, RunState, WaitForCi
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.sweep_no_ci import DueNoCiCandidate
from app.modules.reviews.infrastructure.models import CodeChange, Run

_NO_CI_WINDOW = timedelta(minutes=2)


class SqlAlchemyDueNoCiCandidates:
    def __init__(
        self,
        session_or_factory: AsyncSession | async_sessionmaker[AsyncSession],
    ) -> None:
        self._session: AsyncSession | None
        self._session_factory: async_sessionmaker[AsyncSession] | None
        if isinstance(session_or_factory, AsyncSession):
            self._session = session_or_factory
            self._session_factory = None
        else:
            self._session = None
            self._session_factory = session_or_factory

    @asynccontextmanager
    async def _session_scope(self) -> AsyncIterator[AsyncSession]:
        if self._session is not None:
            yield self._session
        else:
            assert self._session_factory is not None
            async with self._session_factory() as session:
                yield session

    async def list_due(self, now: datetime, limit: int) -> tuple[DueNoCiCandidate, ...]:
        active_run = exists(
            select(Run.id).where(
                Run.code_change_id == CodeChange.id,
                Run.state.in_([RunState.QUEUED, RunState.RUNNING, RunState.PUBLISHING]),
            )
        )
        webhook_run = exists(
            select(Run.id).where(
                Run.code_change_id == CodeChange.id,
                Run.trigger == "webhook",
                Run.head_sha == CodeChange.head_sha,
            )
        )
        async with self._session_scope() as session:
            rows = await session.execute(
                select(CodeChange.id, CodeChange.head_sha)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .where(
                    CodeChange.state == CodeChangeState.OPEN,
                    CodeChange.ai_review_labeled.is_(True),
                    CodeChange.ai_review_labeled_at.is_not(None),
                    CodeChange.head_first_seen_at.is_not(None),
                    CodeChange.ci_status == {},
                    Repository.enabled.is_(True),
                    Repository.wait_for_ci == WaitForCi.AUTO,
                    ~active_run,
                    ~webhook_run,
                    func.greatest(CodeChange.ai_review_labeled_at, CodeChange.head_first_seen_at)
                    + _NO_CI_WINDOW
                    <= now,
                )
                .order_by(CodeChange.id)
                .limit(limit)
            )
            return tuple(DueNoCiCandidate(*row) for row in rows)

    async def exclude(self, candidate: DueNoCiCandidate) -> None:
        async with self._session_scope() as session:
            await session.execute(
                update(CodeChange)
                .where(
                    CodeChange.id == candidate.code_change_id,
                    CodeChange.head_sha == candidate.head_sha,
                    CodeChange.ci_status == {},
                )
                .values(ci_status={"sweep": "excluded"})
            )
            await session.flush()


class SqlAlchemySweepNoCiUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def candidates(self) -> SqlAlchemyDueNoCiCandidates:
        return SqlAlchemyDueNoCiCandidates(self.session)
