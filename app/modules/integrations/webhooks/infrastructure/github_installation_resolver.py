"""Database lookup for workspace-authorized GitHub installations."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.repositories.infrastructure.models import ProviderInstallation


class SqlAlchemyGitHubInstallationResolver:
    """Resolve an existing GitHub installation without creating any records."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def find_github_installation_id(self, external_id: int) -> UUID | None:
        """Return an internal id only for a workspace-linked GitHub installation."""
        statement = select(ProviderInstallation.id).where(
            ProviderInstallation.provider == "github",
            ProviderInstallation.external_id == external_id,
        )
        async with self._session_factory() as session:
            return cast(UUID | None, await session.scalar(statement))
