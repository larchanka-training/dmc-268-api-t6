"""SQLAlchemy writes of ``repositories.private`` from GitHub events (#454)."""

from __future__ import annotations

from sqlalchemy import ColumnElement, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


def _github_repository(
    installation_external_id: int, repository_external_id: int
) -> ColumnElement[bool]:
    installation = (
        select(ProviderInstallation.id)
        .where(
            ProviderInstallation.provider == "github",
            ProviderInstallation.external_id == installation_external_id,
        )
        .scalar_subquery()
    )
    return (Repository.provider_installation_id == installation) & (
        Repository.external_id == repository_external_id
    )


class SqlAlchemyRepositoryVisibilityStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def overwrite(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> bool:
        repository_id = await self._session.scalar(
            update(Repository)
            .where(_github_repository(installation_external_id, repository_external_id))
            .values(private=private)
            .returning(Repository.id)
        )
        return repository_id is not None

    async def learn(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> None:
        await self._session.execute(
            update(Repository)
            .where(
                _github_repository(installation_external_id, repository_external_id),
                Repository.private.is_not(True),
                # Most deliveries change nothing: no write for them.
                Repository.private.is_distinct_from(private),
            )
            .values(private=private)
        )


class SqlAlchemyRepositoryVisibilityUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def visibility(self) -> SqlAlchemyRepositoryVisibilityStore:
        return SqlAlchemyRepositoryVisibilityStore(self.session)
