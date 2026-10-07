"""Link installations from an authenticated GitHub user token to Workspaces."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork

_MAX_BIGINT = 2**63 - 1


@dataclass(frozen=True)
class InstallationSnapshotReservation:
    generation: int
    started_at: datetime


@dataclass(frozen=True)
class GitHubInstallation:
    id: int
    account_login: str
    repository_ids: tuple[int, ...] = ()


@dataclass(frozen=True)
class AuthenticatedGitHubInstallations:
    user_id: int
    installations: tuple[GitHubInstallation, ...]


class GitHubUserInstallationsProvider(Protocol):
    """Derive both identity and accessible installations from one user token."""

    async def identify_user(self, access_token: str) -> int: ...

    async def list_for_user(self, access_token: str) -> AuthenticatedGitHubInstallations: ...


class GitHubInstallationLinkStore(Protocol):
    async def reserve_generation(self, user_id: int) -> InstallationSnapshotReservation: ...

    async def begin_apply(self, user_id: int, generation: int) -> bool: ...

    async def mark_applied(self, user_id: int, generation: int) -> None: ...

    async def current_workspace_ids(self, user_id: int) -> tuple[UUID, ...]: ...

    async def link(self, user_id: int, installation: GitHubInstallation) -> UUID: ...

    async def reconcile_repositories(
        self,
        user_id: int,
        installation_id: int,
        repository_ids: tuple[int, ...],
        snapshot_started_at: datetime,
    ) -> None: ...

    async def wake_receipts(self, installation_id: int) -> None: ...

    async def revoke_unlisted(
        self, user_id: int, workspace_ids: tuple[UUID, ...], installation_ids: tuple[int, ...]
    ) -> None: ...


class GitHubInstallationLinkUnitOfWork(UnitOfWork, Protocol):
    @property
    def links(self) -> GitHubInstallationLinkStore: ...


class LinkGitHubInstallations:
    """Reconcile one authenticated user's installations without webhook-created access."""

    def __init__(
        self,
        *,
        github: GitHubUserInstallationsProvider,
        uow_factory: Callable[[], GitHubInstallationLinkUnitOfWork],
    ) -> None:
        self._github = github
        self._uow_factory = uow_factory

    async def execute(
        self, access_token: str, *, expected_user_id: int | None = None
    ) -> tuple[UUID, ...]:
        user_id = await self._github.identify_user(access_token)
        if expected_user_id is not None and user_id != expected_user_id:
            raise ValueError("GitHub user identity changed before installation sync")
        if not 0 < user_id <= _MAX_BIGINT:
            raise ValueError("GitHub user ID must fit PostgreSQL BIGINT")
        async with self._uow_factory() as uow:
            reservation = await uow.links.reserve_generation(user_id)
            await uow.commit()

        user = await self._github.list_for_user(access_token)
        if user.user_id != user_id:
            raise ValueError("GitHub user identity changed during installation sync")

        unique_installations: list[GitHubInstallation] = []
        seen_installations: set[int] = set()
        for installation in user.installations:
            if not 0 < installation.id <= _MAX_BIGINT:
                raise ValueError("GitHub installation ID must fit PostgreSQL BIGINT")
            if any(
                not 0 < repository_id <= _MAX_BIGINT
                for repository_id in installation.repository_ids
            ):
                raise ValueError("GitHub repository ID must fit PostgreSQL BIGINT")
            if installation.id in seen_installations:
                continue
            seen_installations.add(installation.id)
            unique_installations.append(
                GitHubInstallation(
                    installation.id,
                    installation.account_login,
                    tuple(sorted(set(installation.repository_ids))),
                )
            )
        unique_installations.sort(key=lambda item: item.id)

        workspace_ids: list[UUID] = []
        async with self._uow_factory() as uow:
            if not await uow.links.begin_apply(user_id, reservation.generation):
                return await uow.links.current_workspace_ids(user_id)
            for installation in unique_installations:
                workspace_id = await uow.links.link(user_id, installation)
                await uow.links.reconcile_repositories(
                    user_id, installation.id, installation.repository_ids, reservation.started_at
                )
                await uow.links.wake_receipts(installation.id)
                workspace_ids.append(workspace_id)
            await uow.links.revoke_unlisted(
                user_id,
                tuple(workspace_ids),
                tuple(installation.id for installation in unique_installations),
            )
            await uow.links.mark_applied(user_id, reservation.generation)
            await uow.commit()
        return tuple(workspace_ids)
