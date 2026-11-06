"""Keep the saved visibility of a GitHub repository current (#454).

The public badge trusts this flag alone. Every write says when the flag was known, and only a
newer observation replaces the stored one; one short transaction, no GitHub call inside it.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork

# A flag not confirmed for this long reads as not public, recorded in contracts/openapi.yaml.
VISIBILITY_MAX_AGE = timedelta(hours=72)


class RepositoryVisibilityStore(Protocol):
    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        """Back to unknown for every repository of the installation; False when it has none."""
        ...

    async def forget_repository(self, repository_external_id: int, observed_at: datetime) -> bool:
        """Back to unknown wherever a GitHub installation holds it; False when none does."""
        ...

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
    ) -> bool:
        """False when the repository is not connected."""
        ...

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool | None,
        observed_at: datetime,
    ) -> bool:
        """False when the repository is not connected."""
        ...

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        """Fill an unknown value or close; ``overwrite`` alone opens."""
        ...


class RepositoryVisibilityUnitOfWork(UnitOfWork, Protocol):
    @property
    def visibility(self) -> RepositoryVisibilityStore: ...


class RecordRepositoryVisibility:
    def __init__(self, uow_factory: Callable[[], RepositoryVisibilityUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def forget(self, installation_external_id: int, observed_at: datetime) -> bool:
        """``installation.suspend``: no event reaches the service until it ends, so nothing would
        say a repository went private. A delivery older than the suspension cannot fill it back."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.forget(installation_external_id, observed_at)
            await uow.commit()
        return known

    async def forget_repository(self, repository_external_id: int, observed_at: datetime) -> bool:
        """``repository.transferred``: a row is never moved to the new owner's installation."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.forget_repository(repository_external_id, observed_at)
            await uow.commit()
        return known

    async def rename(
        self,
        installation_external_id: int,
        repository_external_id: int,
        full_name: str,
        web_url: str | None,
    ) -> bool:
        """``repository.renamed``: the badge finds a repository by its current name."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.rename(
                installation_external_id, repository_external_id, full_name, web_url
            )
            await uow.commit()
        return known

    async def overwrite(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> bool:
        """``repository.privatized`` or ``publicized``: the change itself, so it may open."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.overwrite(
                installation_external_id, repository_external_id, private, observed_at
            )
            await uow.commit()
        return known

    async def learn(
        self,
        installation_external_id: int,
        repository_external_id: int,
        private: bool,
        observed_at: datetime,
    ) -> None:
        """The flag in the payload of another event: it fills the rows connected before the flag
        existed and may close, but never opens."""
        async with self._uow_factory() as uow:
            await uow.visibility.learn(
                installation_external_id, repository_external_id, private, observed_at
            )
            await uow.commit()
