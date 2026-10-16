"""The repository an indexing job indexes, read in a short session of its own (#142)."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.application.ports import IndexedRepository
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


class SqlAlchemyIndexRepositoryReader:
    """`IndexRepositoryReader`: the repository row and its provider installation.

    The lifecycle of a removed installation or a disabled repository is #143's: their
    index is purged there, and a job of a purged index is refused at the claim.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get(self, repository_id: UUID) -> IndexedRepository | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        Repository.full_name,
                        ProviderInstallation.provider,
                        ProviderInstallation.external_id,
                    )
                    .join(
                        ProviderInstallation,
                        ProviderInstallation.id == Repository.provider_installation_id,
                    )
                    .where(Repository.id == repository_id)
                )
            ).one_or_none()
        if row is None:
            return None
        return IndexedRepository(
            repository_id=repository_id,
            provider=row.provider,
            installation_external_id=row.external_id,
            full_name=row.full_name,
        )
