"""Resolve actionable GitHub delivery identities to current PR rows."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState
from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.project_github_pull_request import PullRequestEvent
from app.modules.reviews.application.trigger_from_delivery import (
    CiTriggerEvent,
    ProjectedPullRequestTarget,
)
from app.modules.reviews.infrastructure.models import CodeChange


class SqlAlchemyWebhookRunTargets:
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

    async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget | None:
        async with self._session_scope() as session:
            row = (
                await session.execute(
                    select(CodeChange.id, CodeChange.head_sha)
                    .join(Repository, CodeChange.repository_id == Repository.id)
                    .join(
                        ProviderInstallation,
                        Repository.provider_installation_id == ProviderInstallation.id,
                    )
                    .where(
                        ProviderInstallation.provider == "github",
                        ProviderInstallation.external_id == event.installation_external_id,
                        Repository.external_id == event.repository_external_id,
                        CodeChange.external_id == event.external_id,
                        CodeChange.state == CodeChangeState.OPEN,
                    )
                )
            ).one_or_none()
            return ProjectedPullRequestTarget(*row) if row is not None else None

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
        async with self._session_scope() as session:
            rows = await session.scalars(
                select(CodeChange.id)
                .join(Repository, CodeChange.repository_id == Repository.id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .where(
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.external_id == event.installation_external_id,
                    Repository.external_id == event.repository_external_id,
                    CodeChange.head_sha == event.head_sha,
                    CodeChange.state == CodeChangeState.OPEN,
                )
                .order_by(CodeChange.id)
            )
            ids = tuple(rows.all())
            if ids and event.event_name in {"check_suite", "status"}:
                await session.execute(
                    update(CodeChange)
                    .where(CodeChange.id.in_(ids), CodeChange.head_sha == event.head_sha)
                    .values(ci_status={"event": event.event_name})
                )
                await session.flush()
                if self._session is None:
                    await session.commit()
            return ids


class SqlAlchemyRunTriggerUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def targets(self) -> SqlAlchemyWebhookRunTargets:
        return SqlAlchemyWebhookRunTargets(self.session)
