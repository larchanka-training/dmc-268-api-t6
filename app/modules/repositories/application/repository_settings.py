"""Review settings of connected repositories (api#20 D10)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork


@dataclass(frozen=True)
class RepositorySettings:
    id: UUID
    full_name: str
    url: str
    default_branch: str
    enabled: bool
    default_engine: str
    wait_for_ci: str
    max_comments: int
    review_event: str


@dataclass(frozen=True)
class RepositorySettingsChange:
    """Settings to change; ``None`` keeps the stored value."""

    enabled: bool | None = None
    default_engine: str | None = None
    wait_for_ci: str | None = None
    max_comments: int | None = None
    review_event: str | None = None


class RepositorySettingsStore(Protocol):
    """Only repositories of the caller's Workspaces are visible; flushes, never commits."""

    async def list_repositories(self) -> list[RepositorySettings]: ...

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None: ...

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None: ...


class RepositorySettingsUnitOfWork(UnitOfWork, Protocol):
    @property
    def repositories(self) -> RepositorySettingsStore: ...


type RepositorySettingsUowFactory = Callable[[], RepositorySettingsUnitOfWork]


class ListRepositories:
    def __init__(self, uow_factory: RepositorySettingsUowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self) -> list[RepositorySettings]:
        async with self._uow_factory() as uow:
            return await uow.repositories.list_repositories()


class GetRepository:
    def __init__(self, uow_factory: RepositorySettingsUowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(self, repository_id: UUID) -> RepositorySettings | None:
        async with self._uow_factory() as uow:
            return await uow.repositories.get_repository(repository_id)


class UpdateRepository:
    def __init__(self, uow_factory: RepositorySettingsUowFactory) -> None:
        self._uow_factory = uow_factory

    async def execute(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        async with self._uow_factory() as uow:
            item = await uow.repositories.update_repository(repository_id, change)
            if item is not None:
                await uow.commit()
            return item
