"""Atomically register installation repositories with immutable default rules."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.repositories.application.installation_repositories import (
    RepositoryReference,
    RepositorySnapshot,
)
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

    async def disable_repository(
        self, provider_installation_id: UUID, external_id: int
    ) -> None: ...

    async def disable_installation(self, provider_installation_id: UUID) -> None: ...

    async def record_removal_delivery(self, delivery_id: str) -> bool:
        """Record once in this transaction; false means its effect already committed."""
        ...


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

    async def disable(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryReference, ...],
        delivery_id: str,
        all_repositories: bool = False,
    ) -> None:
        """Soft-disable removed repositories in one short database transaction.

        This lifecycle path deliberately accepts transport references but
        persists only their stable external ids. The delivery marker commits with
        the effect, so a retry cannot revoke access restored after the first commit.
        """
        if not delivery_id:
            raise ValueError("removal requires a durable delivery id")
        async with self._uow_factory() as uow:
            if not await uow.repositories.record_removal_delivery(delivery_id):
                return
            if all_repositories:
                await uow.repositories.disable_installation(provider_installation_id)
            for repository in () if all_repositories else repositories:
                await uow.repositories.disable_repository(
                    provider_installation_id, repository.external_id
                )
            await uow.commit()
