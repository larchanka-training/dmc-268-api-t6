"""Transactional Workspace links for authenticated GitHub installations."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from hashlib import sha256
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import and_, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.integrations.webhooks.infrastructure.webhook_notifications import (
    notify_webhook_work,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.application.link_github_installations import (
    GitHubInstallation,
    InstallationSnapshotReservation,
)
from app.modules.workspaces.infrastructure.github_repository_rebinding import (
    SqlAlchemyGitHubRepositoryRebinding,
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

    async def lock_installations(self, installations: tuple[GitHubInstallation, ...]) -> None:
        accounts = sorted(
            {
                item.account_external_id
                for item in installations
                if item.account_external_id is not None
            }
        )
        for account_id in accounts:
            key = int.from_bytes(
                sha256(f"github-reinstall-account:{account_id}".encode()).digest()[:8],
                "big",
                signed=True,
            )
            await self._session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": key})
        # Include missing-account snapshots in the same global row-lock ordering.
        # Rebinding can read several archived rows before a later snapshot target.
        await self._session.execute(
            select(ProviderInstallation.id)
            .where(
                ProviderInstallation.provider == "github",
                or_(
                    ProviderInstallation.external_id.in_([item.id for item in installations]),
                    ProviderInstallation.account_external_id.in_(accounts),
                ),
            )
            .order_by(ProviderInstallation.external_id)
            .with_for_update(key_share=True)
        )

    async def current_workspace_ids(self, user_id: int) -> tuple[UUID, ...]:
        result = await self._session.scalars(
            select(GitHubUserWorkspaceAccess.workspace_id)
            .where(GitHubUserWorkspaceAccess.github_user_id == user_id)
            .order_by(GitHubUserWorkspaceAccess.workspace_id)
        )
        return tuple(result.all())

    async def link(self, user_id: int, installation: GitHubInstallation) -> UUID:
        archive_workspace = await SqlAlchemyGitHubRepositoryRebinding(
            self._session
        ).archived_workspace(installation)
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
            if archive_workspace is None:
                self._session.add(workspace)
                await self._session.flush()
            created_installation_id = cast(
                UUID | None,
                await self._session.scalar(
                    insert(ProviderInstallation)
                    .values(
                        id=uuid4(),
                        workspace_id=archive_workspace or workspace.id,
                        provider="github",
                        external_id=installation.id,
                        account_external_id=installation.account_external_id,
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
                if archive_workspace is None:
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
                workspace_id = archive_workspace or workspace.id

        if installation.account_external_id is not None:
            # The conditional UPDATE locks the row and rechecks identity after a
            # concurrent insert/update. An established owner is never overwritten.
            verified_id = await self._session.scalar(
                update(ProviderInstallation)
                .where(
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.external_id == installation.id,
                    or_(
                        and_(
                            ProviderInstallation.account_external_id.is_(None),
                            ProviderInstallation.deleted_at.is_(None),
                        ),
                        ProviderInstallation.account_external_id
                        == installation.account_external_id,
                    ),
                )
                .values(account_external_id=installation.account_external_id)
                .returning(ProviderInstallation.id)
            )
            if verified_id is None:
                stored = await self._session.scalar(
                    select(ProviderInstallation)
                    .where(
                        ProviderInstallation.provider == "github",
                        ProviderInstallation.external_id == installation.id,
                    )
                    .execution_options(populate_existing=True)
                )
                if (
                    stored is None
                    or stored.deleted_at is None
                    or stored.account_external_id is not None
                ):
                    raise ValueError("GitHub installation account identity changed")
                # Late OAuth cannot establish previously absent evidence for a
                # retired installation and make its archive eligible for rebind.

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
        account_external_id: int | None = None,
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

        target = await self._session.get(
            ProviderInstallation, provider_installation_id, populate_existing=True
        )
        if target is None:
            raise RuntimeError("GitHub installation disappeared during reconciliation")
        if target.deleted_at is not None:
            repository_ids = ()
        await SqlAlchemyGitHubRepositoryRebinding(self._session).rebind(
            target,
            GitHubInstallation(installation_id, "", repository_ids, account_external_id),
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
        result = await self._session.execute(
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

        if cast(CursorResult[tuple[object, ...]], result).rowcount > 0:
            await notify_webhook_work(self._session)

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
