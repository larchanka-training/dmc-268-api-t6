"""Forward the worker's provider to the conventions application ports."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.conventions import (
    ConventionsModel,
    RepositoryConventionsSource,
    RepositoryFile,
    RepositorySnapshot,
)


class ReviewConventionsProvider(Protocol):
    """Provider capabilities the ordinary review worker requires for conventions."""

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot: ...

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]: ...

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]: ...

    async def draft_conventions(
        self,
        *,
        agents_md: str | None,
        files: tuple[RepositoryFile, ...],
        languages: dict[str, int],
    ) -> Mapping[str, object]: ...


class ProviderRepositoryConventionsSource(RepositoryConventionsSource):
    """Concrete worker adapter for revision-pinned repository provider calls."""

    def __init__(self, provider: ReviewConventionsProvider) -> None:
        self._provider = provider

    async def fetch_agents_md(self, repository_id: UUID) -> RepositorySnapshot:
        return await self._provider.fetch_agents_md(repository_id)

    async def fetch_tree(self, repository_id: UUID) -> tuple[RepositoryFile, ...]:
        return await self._provider.fetch_tree(repository_id)

    async def fetch_files(
        self, repository_id: UUID, paths: tuple[str, ...]
    ) -> tuple[RepositoryFile, ...]:
        return await self._provider.fetch_files(repository_id, paths)


class ProviderConventionsModel(ConventionsModel):
    """Concrete worker adapter for the provider's strict conventions draft call."""

    def __init__(self, provider: ReviewConventionsProvider) -> None:
        self._provider = provider

    async def draft_conventions(
        self,
        *,
        agents_md: str | None,
        files: tuple[RepositoryFile, ...],
        languages: dict[str, int],
    ) -> Mapping[str, object]:
        return await self._provider.draft_conventions(
            agents_md=agents_md,
            files=files,
            languages=languages,
        )
