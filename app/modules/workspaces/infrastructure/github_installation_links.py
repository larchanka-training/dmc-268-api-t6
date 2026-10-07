"""Transactional Workspace links for authenticated GitHub installations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.application.link_github_installations import (
    GitHubInstallation,
    InstallationSnapshotReservation,
)
from app.modules.workspaces.infrastructure.models import (
    GitHubInstallationAccessRevocation,
    GitHubUserInstallationSync,
    GitHubUserRepositoryAccess,
    GitHubUserWorkspaceAccess,
    Workspace,
)


class SqlAlchemyGitHubInstallationLinkStore:
    """Flush-only writes; the application use case commits the whole reconciliation."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def reserve_generation(self, user_id: int) -> InstallationSnapshotReservation:
        generation = cast(
            int | None,
            await self._session.scalar(
                insert(GitHubUserInstallationSync)
                .values(
                    github_user_id=user_id,
                    reserved_generation=1,
                    applied_generation=0,
                )
                .on_conflict_do_update(
                    index_elements=[GitHubUserInstallationSync.github_user_id],
                    set_={
                        "reserved_generation": GitHubUserInstallationSync.reserved_generation + 1
                    },
                )
                .returning(GitHubUserInstallationSync.reserved_generation)
            ),
        )
        if generation is None:
            raise RuntimeError("GitHub installation sync generation was not reserved")
        started_at = cast(datetime, await self._session.scalar(select(func.clock_timestamp())))
        return InstallationSnapshotReservation(generation, started_at)

    async def begin_apply(self, user_id: int, generation: int) -> bool:
        applied_generation = await self._session.scalar(
            select(GitHubUserInstallationSync.applied_generation)
            .where(GitHubUserInstallationSync.github_user_id == user_id)
            .with_for_update()
        )
        if applied_generation is None:
            raise RuntimeError("GitHub installation sync generation is missing")
        return generation > applied_generation

    async def mark_applied(self, user_id: int, generation: int) -> None:
        await self._session.execute(
            update(GitHubUserInstallationSync)
            .where(
                GitHubUserInstallationSync.github_user_id == user_id,
                GitHubUserInstallationSync.applied_generation < generation,
            )
            .values(applied_generation=generation)
        )

    async def current_workspace_ids(self, user_id: int) -> tuple[UUID, ...]:
        result = await self._session.scalars(
            select(GitHubUserWorkspaceAccess.workspace_id)
            .where(GitHubUserWorkspaceAccess.github_user_id == user_id)
            .order_by(GitHubUserWorkspaceAccess.workspace_id)
        )
        return tuple(result.all())

    async def link(self, user_id: int, installation: GitHubInstallation) -> UUID:
        existing = await self._session.scalar(
            select(ProviderInstallation.workspace_id).where(
                ProviderInstallation.provider == "github",
                ProviderInstallation.external_id == installation.id,
            )
        )
        workspace_id = existing
        if workspace_id is None:
            workspace = Workspace(
                id=uuid4(),
                name=f"GitHub: {installation.account_login}"[:255],
                daily_budget_usd=Decimal("0"),
            )
            self._session.add(workspace)
            await self._session.flush()
            created_installation_id = cast(
                UUID | None,
                await self._session.scalar(
                    insert(ProviderInstallation)
                    .values(
                        id=uuid4(),
                        workspace_id=workspace.id,
                        provider="github",
                        external_id=installation.id,
                        provider_metadata={"account_login": installation.account_login},
                    )
                    .on_conflict_do_nothing(
                        index_elements=[
                            ProviderInstallation.provider,
                            ProviderInstallation.external_id,
                        ]
                    )
                    .returning(ProviderInstallation.id)
                ),
            )
            if created_installation_id is None:
                await self._session.delete(workspace)
                await self._session.flush()
                workspace_id = cast(
                    UUID | None,
                    await self._session.scalar(
                        select(ProviderInstallation.workspace_id).where(
                            ProviderInstallation.provider == "github",
                            ProviderInstallation.external_id == installation.id,
                        )
                    ),
                )
                if workspace_id is None:
                    raise RuntimeError("concurrent GitHub installation link disappeared")
            else:
                workspace_id = workspace.id

        await self._session.execute(
            insert(GitHubUserWorkspaceAccess)
            .values(github_user_id=user_id, workspace_id=workspace_id)
            .on_conflict_do_nothing(
                index_elements=[
                    GitHubUserWorkspaceAccess.github_user_id,
                    GitHubUserWorkspaceAccess.workspace_id,
                ]
            )
        )
        return workspace_id

    async def reconcile_repositories(
        self,
        user_id: int,
        installation_id: int,
        repository_ids: tuple[int, ...],
        snapshot_started_at: datetime,
    ) -> None:
        provider_installation_id = cast(
            UUID | None,
            await self._session.scalar(
                select(ProviderInstallation.id)
                .where(
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.external_id == installation_id,
                )
                .with_for_update(key_share=True)
            ),
        )
        if provider_installation_id is None:
            raise RuntimeError("GitHub installation must be linked before repositories")

        # Apply holds the user generation lock, then installations in external-id order.
        # NO KEY UPDATE serializes removals while allowing repository onboarding FK checks.
        revoked = set(
            (
                await self._session.scalars(
                    select(GitHubInstallationAccessRevocation.repository_external_id).where(
                        GitHubInstallationAccessRevocation.provider_installation_id
                        == provider_installation_id,
                        GitHubInstallationAccessRevocation.revoked_at >= snapshot_started_at,
                    )
                )
            ).all()
        )
        repository_ids = tuple(
            repository_id
            for repository_id in repository_ids
            if 0 not in revoked and repository_id not in revoked
        )

        stale = delete(GitHubUserRepositoryAccess).where(
            GitHubUserRepositoryAccess.github_user_id == user_id,
            GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id,
        )
        if repository_ids:
            stale = stale.where(
                GitHubUserRepositoryAccess.repository_external_id.not_in(repository_ids)
            )
        await self._session.execute(stale)

        if repository_ids:
            await self._session.execute(
                insert(GitHubUserRepositoryAccess)
                .values(
                    [
                        {
                            "github_user_id": user_id,
                            "provider_installation_id": provider_installation_id,
                            "repository_external_id": repository_id,
                        }
                        for repository_id in repository_ids
                    ]
                )
                .on_conflict_do_nothing(
                    index_elements=[
                        GitHubUserRepositoryAccess.github_user_id,
                        GitHubUserRepositoryAccess.provider_installation_id,
                        GitHubUserRepositoryAccess.repository_external_id,
                    ]
                )
            )

    async def wake_receipts(self, installation_id: int) -> None:
        await self._session.execute(
            update(WebhookEvent)
            .where(
                WebhookEvent.installation_external_id == installation_id,
                or_(
                    WebhookEvent.event.in_(("installation", "installation_repositories")),
                    # Every delivery deferred after its last attempt, whatever the reason.
                    WebhookEvent.projection_deferred_at.is_not(None),
                ),
                WebhookEvent.projected_at.is_(None),
                WebhookEvent.payload.is_not(None),
            )
            .values(retry_after=None, projection_deferred_at=None, projection_attempt_count=0)
        )

    async def revoke_unlisted(
        self, user_id: int, workspace_ids: tuple[UUID, ...], installation_ids: tuple[int, ...]
    ) -> None:
        statement = delete(GitHubUserWorkspaceAccess).where(
            GitHubUserWorkspaceAccess.github_user_id == user_id
        )
        if workspace_ids:
            statement = statement.where(
                GitHubUserWorkspaceAccess.workspace_id.not_in(workspace_ids)
            )
        await self._session.execute(statement)

        stale_repositories = delete(GitHubUserRepositoryAccess).where(
            GitHubUserRepositoryAccess.github_user_id == user_id
        )
        if installation_ids:
            listed_installations = select(ProviderInstallation.id).where(
                ProviderInstallation.provider == "github",
                ProviderInstallation.external_id.in_(installation_ids),
            )
            stale_repositories = stale_repositories.where(
                GitHubUserRepositoryAccess.provider_installation_id.not_in(listed_installations)
            )
        await self._session.execute(stale_repositories)


class SqlAlchemyGitHubInstallationLinkUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def links(self) -> SqlAlchemyGitHubInstallationLinkStore:
        return SqlAlchemyGitHubInstallationLinkStore(self.session)
