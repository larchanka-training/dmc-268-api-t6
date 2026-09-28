"""Resolve actionable GitHub delivery identities to current PR rows."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import CodeChangeState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.project_github_pull_request import PullRequestEvent
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent
from app.modules.reviews.infrastructure.models import CodeChange


class SqlAlchemyWebhookRunTargets:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def for_pr(self, event: PullRequestEvent) -> UUID | None:
        async with self._session_factory() as session:
            return cast(
                UUID | None,
                await session.scalar(
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
                        CodeChange.external_id == event.external_id,
                    )
                ),
            )

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]:
        async with self._session_factory() as session:
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
            return tuple(rows.all())
