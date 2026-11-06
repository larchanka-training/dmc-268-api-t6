"""SQLAlchemy writes of ``repositories.private`` (#454).

Every write carries the time it knows the flag as of, and only a newer observation replaces the
stored one. So a retried delivery, which keeps the time it was received, loses to a later change.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import ColumnElement, case, literal, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.types import Boolean

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


def _of_github_installation(installation_external_id: int) -> ColumnElement[bool]:
    installation = (
        select(ProviderInstallation.id)
        .where(
            ProviderInstallation.provider == "github",
            ProviderInstallation.external_id == installation_external_id,
        )
        .scalar_subquery()
    )
    return Repository.provider_installation_id == installation


def _github_repository(
    installation_external_id: int, repository_external_id: int
) -> ColumnElement[bool]:
    return _of_github_installation(installation_external_id) & (
        Repository.external_id == repository_external_id
    )


def _newer(observed_at: datetime, private: bool | None) -> ColumnElement[bool]:
    """A newer observation wins; on a tie only the one that closes."""
    stamp = Repository.private_observed_at
    older = stamp.is_(None) | (stamp < observed_at)
    return older if private is False else older | (stamp == observed_at)


def _ordered(observed_at: datetime, private: bool | None) -> dict[str, ColumnElement[object]]:
    """Set values that keep the row as it is when this observation is not newer."""
    newer = _newer(observed_at, private)
    return {
        "private": case((newer, literal(private, Boolean)), else_=Repository.private),
        "private_observed_at": case((newer, observed_at), else_=Repository.private_observed_at),
    }


class SqlAlchemyRepositoryVisibilityStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        forgotten = await self._session.scalars(
            update(Repository)
            .where(_of_github_installation(installation_external_id))
            .values(_ordered(observed_at, None))
            .returning(Repository.id)
        )
        return bool(forgotten.all())

    async def forget_repository(self, repository_external_id: int, observed_at: datetime) -> bool:
        forgotten = await self._session.scalars(
            update(Repository)
            .where(
                Repository.external_id == repository_external_id,
                Repository.provider_installation_id.in_(
                    select(ProviderInstallation.id).where(ProviderInstallation.provider == "github")
                ),
            )
            .values(_ordered(observed_at, None))
            .returning(Repository.id)
        )
        return bool(forgotten.all())

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
    ) -> bool:
        repository_id = await self._session.scalar(
            update(Repository)
            .where(_github_repository(installation_external_id, repository_external_id))
            .values(
                full_name=full_name,
                web_url=Repository.web_url if web_url is None else web_url,
            )
            .returning(Repository.id)
        )
        return repository_id is not None

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool | None,
        observed_at: datetime,
    ) -> bool:
        repository_id = await self._session.scalar(
            update(Repository)
            .where(_github_repository(installation_external_id, repository_external_id))
            .values(_ordered(observed_at, private))
            .returning(Repository.id)
        )
        return repository_id is not None

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        await self._session.execute(
            update(Repository)
            .where(
                _github_repository(installation_external_id, repository_external_id),
                Repository.private.is_not(True),
                _newer(observed_at, private),
                # Most deliveries change nothing: no write for them.
                Repository.private.is_distinct_from(private),
            )
            .values(private=private, private_observed_at=observed_at)
        )


class SqlAlchemyRepositoryVisibilityUnitOfWork(SqlAlchemyUnitOfWork):
    @property
    def visibility(self) -> SqlAlchemyRepositoryVisibilityStore:
        return SqlAlchemyRepositoryVisibilityStore(self.session)
