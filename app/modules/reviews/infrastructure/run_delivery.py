"""Detached metadata-only admission read for immutable Run queue pointers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    active_gitlab_target_metadata,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.run_delivery import (
    PersistedRunIdentity,
    RunDeliveryPointer,
    RunDeliveryUnavailable,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_origin import active_origin_condition


class SqlAlchemyRunDeliveryReader:
    """One owned short session; returned values carry no live ORM or credential state."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def read(self, run_id: UUID) -> PersistedRunIdentity | None:
        gitlab = active_gitlab_target_metadata(require_repository_enabled=False).subquery()
        statement = (
            select(
                Run.id,
                Run.origin_provider_installation_id,
                ProviderInstallation.provider,
                ProviderInstallation.workspace_id,
                ProviderInstallation.external_id,
                Repository.id,
                Repository.external_id,
                CodeChange.external_number,
                Run.head_sha,
                Run.base_sha,
                Run.engine,
                Run.rule_version_id,
                Run.prompt_version_id,
                Run.trigger,
            )
            .select_from(Run)
            .join(CodeChange, Run.code_change_id == CodeChange.id)
            .join(Repository, CodeChange.repository_id == Repository.id)
            .join(
                ProviderInstallation, Run.origin_provider_installation_id == ProviderInstallation.id
            )
            .outerjoin(
                gitlab,
                and_(
                    gitlab.c.origin_provider_installation_id == Run.origin_provider_installation_id,
                    gitlab.c.workspace_id == ProviderInstallation.workspace_id,
                    gitlab.c.repository_id == Repository.id,
                    gitlab.c.project_id == Repository.external_id,
                ),
            )
            .where(
                Run.id == run_id,
                active_origin_condition(),
                or_(
                    ProviderInstallation.provider == "github",
                    and_(
                        ProviderInstallation.provider == "gitlab",
                        gitlab.c.origin_provider_installation_id.is_not(None),
                    ),
                ),
            )
        )
        try:
            async with self._sessions() as session:
                result = await session.execute(statement)
        except (
            OperationalError,
            InterfaceError,
            DisconnectionError,
            SqlAlchemyTimeoutError,
            OSError,
        ):
            raise RunDeliveryUnavailable() from None
        # No driver/session error boundary surrounds detached row/DTO mapping.
        row = result.one_or_none()
        if row is None:
            return None
        if row[1] is None:
            raise ValueError("Invalid persisted run identity")
        return PersistedRunIdentity(
            RunDeliveryPointer(
                run_id=row[0],
                provider=row[2],
                workspace_id=row[3],
                repository_id=row[5],
                repository_external_id=row[6],
                installation_id=row[4],
                pr_number=row[7],
                head_sha=row[8],
                base_sha=row[9],
                engine=row[10].value,
                rule_version_id=row[11],
                prompt_version_id=row[12],
                trigger=row[13],
            ),
            row[1],
        )
