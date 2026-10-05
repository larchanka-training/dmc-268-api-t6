"""Project durable installation events into repository onboarding work."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositoryReference,
    RepositorySnapshot,
    RepositoryTreeBlob,
    classify_tree_languages,
)
from app.modules.repositories.application.onboard_repository import OnboardingResult
from app.modules.repositories.application.sync_installation_repositories import (
    RepositoryOnboardingInput,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepositoryDetails:
    """The repository fields installation events usually omit."""

    default_branch: str
    web_url: str


class InstallationRepositoryDetailsProvider(Protocol):
    """Read a repository's default branch and web URL using the installation's token."""

    async def fetch_repository_details(
        self, *, installation_external_id: int, full_name: str
    ) -> RepositoryDetails: ...


class InstallationRepositoryTreeProvider(Protocol):
    """Fetch one repository's recursive tree at its declared default branch."""

    async def fetch_default_branch_tree(
        self,
        *,
        installation_external_id: int,
        repository: RepositorySnapshot,
    ) -> tuple[RepositoryTreeBlob, ...]: ...


class InstallationRepositoryLabelProvider(Protocol):
    """Create the review trigger label using the installation's GitHub token."""

    async def create_ai_review_label(
        self, *, installation_external_id: int, repository: RepositorySnapshot
    ) -> None: ...


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
        repositories: tuple[RepositoryReference, ...],
    ) -> None: ...


class InstallationEventProjector:
    """Prepare GitHub repository data before entering the repository transaction.

    A durable-delivery runner calls this projector after parsing a stored
    installation event.  Real events name a repository without its default branch
    and web URL; those are read through ``details_provider`` before the tree is
    fetched, and not at all when the event already carries both.  Provider
    failures deliberately propagate, leaving the delivery eligible for retry and
    ensuring ``sync`` has not opened its unit of work.  Removed/deleted events
    enter the explicit transactional soft-disable path without making a VCS
    request.
    """

    def __init__(
        self,
        *,
        tree_provider: InstallationRepositoryTreeProvider,
        label_provider: InstallationRepositoryLabelProvider,
        details_provider: InstallationRepositoryDetailsProvider,
        sync: InstallationRepositoriesSync,
    ) -> None:
        self._tree_provider = tree_provider
        self._label_provider = label_provider
        self._details_provider = details_provider
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
        for reference in event.added_repositories:
            repository = await self._resolve(event.installation_external_id, reference)
            tree = await self._tree_provider.fetch_default_branch_tree(
                installation_external_id=event.installation_external_id,
                repository=repository,
            )
            try:
                await self._label_provider.create_ai_review_label(
                    installation_external_id=event.installation_external_id,
                    repository=repository,
                )
            except Exception:
                _LOGGER.exception("Failed to create ai-review label for %s", repository.full_name)
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

    async def _resolve(
        self, installation_external_id: int, reference: RepositoryReference
    ) -> RepositorySnapshot:
        """Complete a reference; a missing branch or URL is read from GitHub for both."""
        if reference.default_branch is not None and reference.web_url is not None:
            return RepositorySnapshot(
                external_id=reference.external_id,
                full_name=reference.full_name,
                default_branch=reference.default_branch,
                web_url=reference.web_url,
            )
        details = await self._details_provider.fetch_repository_details(
            installation_external_id=installation_external_id, full_name=reference.full_name
        )
        return RepositorySnapshot(
            external_id=reference.external_id,
            full_name=reference.full_name,
            default_branch=details.default_branch,
            web_url=details.web_url,
        )
