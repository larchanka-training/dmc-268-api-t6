"""Keep the saved visibility of a GitHub repository current (#454).

The public badge trusts this flag alone, and no GitHub call happens to refresh it: one short
transaction per delivery.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from app.common.application.unit_of_work import UnitOfWork


class RepositoryVisibilityStore(Protocol):
    async def forget(self, installation_external_id: int) -> bool:
        """Back to unknown for every repository of the installation; False when it has none."""
        ...

    async def overwrite(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> bool:
        """False when the repository is not connected."""
        ...

    async def learn(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> None:
        """Fill an unknown value or close; only ``overwrite`` opens."""
        ...


class RepositoryVisibilityUnitOfWork(UnitOfWork, Protocol):
    @property
    def visibility(self) -> RepositoryVisibilityStore: ...


class RecordRepositoryVisibility:
    def __init__(self, uow_factory: Callable[[], RepositoryVisibilityUnitOfWork]) -> None:
        self._uow_factory = uow_factory

    async def forget(self, installation_external_id: int) -> bool:
        """``installation.suspend``: no event reaches the service until it ends, so nothing would
        say a repository went private. The next pull request delivery after it fills the flag."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.forget(installation_external_id)
            await uow.commit()
        return known

    async def overwrite(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> bool:
        """``repository.privatized`` or ``publicized``: the change itself, so it wins."""
        async with self._uow_factory() as uow:
            known = await uow.visibility.overwrite(
                installation_external_id, repository_external_id, private
            )
            await uow.commit()
        return known

    async def learn(
        self, installation_external_id: int, repository_external_id: int, private: bool
    ) -> None:
        """The flag in the payload of another event, which may arrive after the change.

        So a late delivery cannot reopen a repository made private. It fills the rows connected
        before the flag existed.
        """
        async with self._uow_factory() as uow:
            await uow.visibility.learn(installation_external_id, repository_external_id, private)
            await uow.commit()
