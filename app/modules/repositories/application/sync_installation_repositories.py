"""Atomically register installation repositories with immutable default rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import (
    DefaultRuleSet,
    OnboardingResult,
    OnboardRepository,
    RepositoryRuleVersionStore,
)


@dataclass(frozen=True)
class RepositoryOnboardingInput:
    """A repository snapshot paired with its already-resolved language shares."""

    snapshot: RepositorySnapshot
    languages: Mapping[str, int]


class InstallationRepositoryStore(RepositoryRuleVersionStore, Protocol):
    """Flush-only store used by installation synchronization."""

    async def upsert_repository(
        self, provider_installation_id: UUID, snapshot: RepositorySnapshot
    ) -> UUID: ...


class InstallationRepositoriesUnitOfWork(UnitOfWork, Protocol):
    """The sync use case owns the one repository-plus-rules transaction."""

    @property
    def repositories(self) -> InstallationRepositoryStore: ...


InstallationRepositoriesUnitOfWorkFactory = Callable[[], InstallationRepositoriesUnitOfWork]


class SyncInstallationRepositories:
    """Upsert repositories and create their initial active rule versions atomically.

    Provider tree retrieval deliberately happens in a caller before this use case.
    This transaction therefore contains only database work and can stay short.
    """

    def __init__(
        self,
        *,
        uow_factory: InstallationRepositoriesUnitOfWorkFactory,
        rule_sets: Mapping[str, DefaultRuleSet],
    ) -> None:
        self._uow_factory = uow_factory
        self._rule_sets = rule_sets

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]:
        async with self._uow_factory() as uow:
            onboarding = OnboardRepository(uow.repositories, self._rule_sets)
            results: list[OnboardingResult] = []
            for repository in repositories:
                repository_id = await uow.repositories.upsert_repository(
                    provider_installation_id, repository.snapshot
                )
                results.append(await onboarding.execute(repository_id, repository.languages))
            await uow.commit()
        return tuple(results)
