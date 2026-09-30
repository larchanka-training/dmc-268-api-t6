"""Review settings of connected repositories (api#20 D10)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID


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


class RepositorySettingsRepository(Protocol):
    """Only repositories of the caller's Workspaces are visible."""

    async def list_repositories(self) -> list[RepositorySettings]: ...

    async def get_repository(self, repository_id: UUID) -> RepositorySettings | None: ...

    async def update_repository(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None: ...


class ListRepositories:
    def __init__(self, repository: RepositorySettingsRepository) -> None:
        self._repository = repository

    async def execute(self) -> list[RepositorySettings]:
        return await self._repository.list_repositories()


class GetRepository:
    def __init__(self, repository: RepositorySettingsRepository) -> None:
        self._repository = repository

    async def execute(self, repository_id: UUID) -> RepositorySettings | None:
        return await self._repository.get_repository(repository_id)


class UpdateRepository:
    def __init__(self, repository: RepositorySettingsRepository) -> None:
        self._repository = repository

    async def execute(
        self, repository_id: UUID, change: RepositorySettingsChange
    ) -> RepositorySettings | None:
        return await self._repository.update_repository(repository_id, change)
