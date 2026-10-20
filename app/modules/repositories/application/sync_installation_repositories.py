"""Atomically register installation repositories with immutable default rules."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.enums import PurgeReason
from app.modules.repositories.application.code_index import CodeIndexPurger
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

_LOGGER = logging.getLogger(__name__)


class InstallationIdentityChanged(RuntimeError):
    """Restart the short UOW so newly assigned account locks precede row locks."""


@dataclass(frozen=True)
class RepositoryOnboardingInput:
    """A repository snapshot paired with its already-resolved language shares."""

    snapshot: RepositorySnapshot
    languages: Mapping[str, int]


class InstallationRepositoryStore(RepositoryRuleVersionStore, Protocol):
    """Flush-only store used by installation synchronization."""

    async def lock_active_installation(self, provider_installation_id: UUID) -> bool: ...

    async def upsert_repository(
        self, provider_installation_id: UUID, snapshot: RepositorySnapshot
    ) -> UUID | None:
        """Return None when fresh OAuth must confirm a preserved archive first."""
        ...

    async def disable_repository(
        self, provider_installation_id: UUID, external_id: int
    ) -> UUID | None:
        """The id of the repository that lost App access; None when none matched."""
        ...

    async def disable_installation(self, provider_installation_id: UUID) -> list[UUID]:
        """The ids of every repository of the installation."""
        ...

    async def record_removal_delivery(self, delivery_id: str) -> bool:
        """Record once in this transaction; false means its effect already committed."""
        ...

    async def request_rules_refresh(self, repository_id: UUID) -> None:
        """Ask ``webhook-worker`` to read ``.review/rules.md`` of the repository (#150)."""
        ...


class InstallationRepositoriesUnitOfWork(UnitOfWork, Protocol):
    """The sync use case owns the one repository-plus-rules transaction."""

    @property
    def repositories(self) -> InstallationRepositoryStore: ...

    @property
    def index_purger(self) -> CodeIndexPurger: ...


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

    async def is_active(self, provider_installation_id: UUID) -> bool:
        # The short preflight UOW closes before the projector enters any provider.
        retried = False
        while True:
            try:
                async with self._uow_factory() as uow:
                    return await uow.repositories.lock_active_installation(provider_installation_id)
            except InstallationIdentityChanged:
                _LOGGER.info(
                    "Retrying installation preflight %s: account identity changed before row lock",
                    provider_installation_id,
                )
                if retried:
                    raise
                retried = True

    async def execute(
        self,
        *,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]:
        # Account identity is immutable once assigned, so one NULL-to-numeric
        # transition can require one retry. Exiting the failed UOW releases every
        # row lock before the replacement transaction acquires its account fence.
        retried = False
        while True:
            try:
                return await self._execute_transaction(provider_installation_id, repositories)
            except InstallationIdentityChanged:
                _LOGGER.info(
                    "Retrying installation sync %s: account identity changed before row lock",
                    provider_installation_id,
                )
                if retried:
                    raise
                retried = True

    async def _execute_transaction(
        self,
        provider_installation_id: UUID,
        repositories: tuple[RepositoryOnboardingInput, ...],
    ) -> tuple[OnboardingResult, ...]:
        async with self._uow_factory() as uow:
            if not await uow.repositories.lock_active_installation(provider_installation_id):
                return ()
            onboarding = OnboardRepository(uow.repositories, self._rule_sets)
            results: list[OnboardingResult] = []
            for repository in repositories:
                repository_id = await uow.repositories.upsert_repository(
                    provider_installation_id, repository.snapshot
                )
                if repository_id is not None:
                    results.append(await onboarding.execute(repository_id, repository.languages))
                    # In the same commit, so a connected repository always gets its read.
                    await uow.repositories.request_rules_refresh(repository_id)
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
        The index of every repository that lost access is purged in the same commit
        (#143, ADR 0001 §6.2).
        """
        if not delivery_id:
            raise ValueError("removal requires a durable delivery id")
        async with self._uow_factory() as uow:
            if not await uow.repositories.record_removal_delivery(delivery_id):
                return
            removed: list[UUID] = []
            if all_repositories:
                removed = await uow.repositories.disable_installation(provider_installation_id)
            for repository in () if all_repositories else repositories:
                repository_id = await uow.repositories.disable_repository(
                    provider_installation_id, repository.external_id
                )
                if repository_id is not None:
                    removed.append(repository_id)
            # After the repositories rows and in id order, so every path locks
            # `repositories` before the index state rows, and those in one order.
            for repository_id in sorted(set(removed)):
                await uow.index_purger.purge(repository_id, PurgeReason.REMOVED)
            await uow.commit()
