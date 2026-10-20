"""Review settings of connected repositories (api#20 D10)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.repositories.application.code_index import CodeIndexEnabler, CodeIndexPurger


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
    branch_filter: tuple[str, ...]
    severity_threshold: str
    installation_available: bool
    # Like ``enabled``: the stored preference AND ``installation_available``; ``rag_index``
    # is derived from this effective value (#145).
    rag_enabled: bool
    rag_index: RagIndex


@dataclass(frozen=True)
class RepositorySettingsChange:
    """Settings to change; ``None`` keeps the stored value, an empty ``branch_filter`` clears it."""

    enabled: bool | None = None
    default_engine: str | None = None
    wait_for_ci: str | None = None
    max_comments: int | None = None
    review_event: str | None = None
    branch_filter: tuple[str, ...] | None = None
    severity_threshold: str | None = None
    rag_enabled: bool | None = None


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

    @property
    def index_purger(self) -> CodeIndexPurger: ...

    @property
    def index_enabler(self) -> CodeIndexEnabler: ...


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
            if item is None:
                return None
            # Only a body that sets the toggle touches the index: editing other settings
            # never brings back an index the TTL purged. Off purges; on requests a full
            # build when the re-read shows RAG in effect (`rag_enabled` is masked by the
            # installation). The purger and the enabler are scoped like the store.
            if change.rag_enabled is False:
                await uow.index_purger.purge(item.id, PurgeReason.DISABLED)
                item = await uow.repositories.get_repository(item.id)
            elif change.rag_enabled is True and item.rag_enabled:
                await uow.index_enabler.enable(item.id)
                item = await uow.repositories.get_repository(item.id)
            await uow.commit()
            return item
