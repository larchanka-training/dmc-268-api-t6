"""Project durable installation events into repository onboarding work."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositorySnapshot,
    RepositoryTreeBlob,
    classify_tree_languages,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)


class InstallationRepositoryTreeProvider(Protocol):
    """Fetch one repository's recursive tree at its declared default branch."""

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]: ...


class InstallationRepositoriesSync(Protocol):
    """Transactional repository and initial-rule synchronization boundary."""

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]: ...

    async def disable(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositorySnapshot, ...],
    ) -> None: ...


class InstallationEventProjector:
    """Prepare GitHub tree data before entering the repository transaction.

    A durable-delivery runner calls this projector after parsing a stored
    installation event.  Provider failures deliberately propagate, leaving the
    delivery eligible for retry and ensuring ``sync`` has not opened its unit of
    work.  Removed/deleted events enter the explicit transactional soft-disable
    path without making a VCS request.
    """

    def __init__(
        self,
        *,
        tree_provider: InstallationRepositoryTreeProvider,
        sync: InstallationRepositoriesSync,
    ) -> None:
        self._tree_provider = tree_provider
        self._sync = sync

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        event: InstallationRepositoriesEvent,
    ) -> tuple[OnboardingResult, ...]:
        if event.removed_repositories:
            await self._sync.disable(
                provider_installation_id=provider_installation_id,
                repositories=event.removed_repositories,
            )
            return ()

        inputs: list[RepositoryOnboardingInput] = []
        for repository in event.added_repositories:
            tree = await self._tree_provider.fetch_default_branch_tree(
                installation_external_id=event.installation_external_id,
                repository=repository,
            )
            inputs.append(
                RepositoryOnboardingInput(
                    snapshot=repository,
                    languages=classify_tree_languages(tree),
                )
            )

        if not inputs:
            return ()
        return await self._sync.execute(
            provider_installation_id=provider_installation_id,
            repositories=tuple(inputs),
        )
