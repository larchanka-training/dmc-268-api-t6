"""SQLAlchemy persistence composition for installation repository synchronization."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.code_index.infrastructure.purger import SqlAlchemyCodeIndexPurger
from app.modules.integrations.webhooks.infrastructure.models import GitHubInstallationRemovalEffect
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.application.sync_installation_repositories import (
    InstallationIdentityChanged,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
)
from app.modules.repositories.infrastructure.rules_status import request_rules_refresh
from app.modules.workspaces.application.link_github_installations import GitHubInstallation
from app.modules.workspaces.infrastructure.github_installation_locks import lock_github_account
from app.modules.workspaces.infrastructure.github_repository_rebinding import (
    SqlAlchemyGitHubRepositoryRebinding,
)
from app.modules.workspaces.infrastructure.models import (
    GitHubInstallationAccessRevocation,
    GitHubUserRepositoryAccess,
)


class SqlAlchemyInstallationRepositoryStore:
    """Session-bound, flush-only adapter for repository and rule-version writes."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._rule_versions = SqlAlchemyRepositoryRuleVersionStore(session)

    async def lock_active_installation(self, provider_installation_id: UUID) -> bool:
        # Account first, then installations in OAuth's global order. The initial
        # read owns no row lock; recheck activity after acquiring the account fence.
        installation = await self._session.get(ProviderInstallation, provider_installation_id)
        if installation is None:
            return False
        initial_account_id = installation.account_external_id
        if installation.provider == "github" and initial_account_id is not None:
            await lock_github_account(self._session, initial_account_id)
            await self._session.execute(
                select(ProviderInstallation.id)
                .where(
                    ProviderInstallation.provider == "github",
                    or_(
                        ProviderInstallation.id == provider_installation_id,
                        ProviderInstallation.account_external_id == initial_account_id,
                    ),
                )
                .order_by(ProviderInstallation.external_id)
                .with_for_update(key_share=True)
            )
        active = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == provider_installation_id,
                ProviderInstallation.deleted_at.is_(None),
            )
            .with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if active is not None and active.account_external_id != initial_account_id:
            # Never acquire a new account fence or earlier archive row while the
            # target row is held. The application restarts the entire short UOW.
            raise InstallationIdentityChanged("account identity changed before row lock")
        return active is not None

    async def upsert_repository(
        self, provider_installation_id: UUID, snapshot: RepositorySnapshot
    ) -> UUID | None:
        """Defer preserved archives until OAuth, otherwise upsert atomically.

        The use case holds the account and installation locks across this decision
        and write. A permanently clean target in a different Workspace can onboard.
        """
        target = await self._session.get(
            ProviderInstallation, provider_installation_id, populate_existing=True
        )
        if (
            target is not None
            and target.provider == "github"
            and target.account_external_id is not None
        ):
            existing = await self._session.scalar(
                select(Repository.id).where(
                    Repository.provider_installation_id == provider_installation_id,
                    Repository.external_id == snapshot.external_id,
                )
            )
            accepted = await self._session.scalar(
                select(GitHubUserRepositoryAccess.github_user_id)
                .where(
                    GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id,
                    GitHubUserRepositoryAccess.repository_external_id == snapshot.external_id,
                )
                .limit(1)
            )
            # OAuth may explicitly accept a clean target while rebinding is denied
            # (for example, its snapshot has no numeric account evidence).
            if existing is None and accepted is None:
                archive = await SqlAlchemyGitHubRepositoryRebinding(
                    self._session
                ).archived_workspace(
                    GitHubInstallation(
                        target.external_id, "", (snapshot.external_id,), target.account_external_id
                    )
                )
                if archive == target.workspace_id:
                    archived_repository = await self._session.scalar(
                        select(Repository.id)
                        .join(ProviderInstallation)
                        .where(
                            ProviderInstallation.provider == "github",
                            ProviderInstallation.external_id != target.external_id,
                            ProviderInstallation.account_external_id == target.account_external_id,
                            ProviderInstallation.deleted_at.is_not(None),
                            Repository.external_id == snapshot.external_id,
                        )
                    )
                    if archived_repository is not None:
                        return None
        statement = (
            insert(Repository)
            .values(
                provider_installation_id=provider_installation_id,
                external_id=snapshot.external_id,
                full_name=snapshot.full_name,
                default_branch=snapshot.default_branch,
                web_url=snapshot.web_url,
            )
            .on_conflict_do_update(
                index_elements=[Repository.provider_installation_id, Repository.external_id],
                set_={
                    "full_name": snapshot.full_name,
                    "default_branch": snapshot.default_branch,
                    "web_url": snapshot.web_url,
                    "installation_available": True,
                },
            )
            .returning(Repository.id)
        )
        repository_id = cast(UUID | None, await self._session.scalar(statement))
        if repository_id is None:
            raise RuntimeError("repository upsert did not return an id")
        return repository_id

    async def record_removal_delivery(self, delivery_id: str) -> bool:
        # PostgreSQL's unique key serializes concurrent copies. A failed transaction
        # rolls the marker back along with grants, repository flags and tombstones.
        recorded = await self._session.scalar(
            insert(GitHubInstallationRemovalEffect)
            .values(delivery_id=delivery_id)
            .on_conflict_do_nothing(index_elements=[GitHubInstallationRemovalEffect.delivery_id])
            .returning(GitHubInstallationRemovalEffect.delivery_id)
        )
        return recorded is not None

    async def disable_repository(
        self, provider_installation_id: UUID, external_id: int
    ) -> UUID | None:
        """Remove App availability if the repository still belongs to this installation.

        The use case deduplicates delivery effects before calling this method.
        A distinct later removal must revoke any access restored in the meantime.
        Returns the repository id, so the use case can purge its index.
        """
        await self._record_revocation(provider_installation_id, external_id)
        statement = (
            update(Repository)
            .where(
                Repository.provider_installation_id == provider_installation_id,
                Repository.external_id == external_id,
            )
            .values(installation_available=False)
            .returning(Repository.id)
        )
        repository_id = await self._session.scalar(statement)
        await self._session.execute(
            delete(GitHubUserRepositoryAccess).where(
                GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id,
                GitHubUserRepositoryAccess.repository_external_id == external_id,
            )
        )
        return repository_id

    async def disable_installation(self, provider_installation_id: UUID) -> list[UUID]:
        """Returns the ids of the installation's repositories, so their indexes are purged."""
        await self._record_revocation(provider_installation_id, 0)
        await self._session.execute(
            update(ProviderInstallation)
            .where(ProviderInstallation.id == provider_installation_id)
            .values(
                deleted_at=func.coalesce(ProviderInstallation.deleted_at, func.clock_timestamp())
            )
        )
        repository_ids = list(
            await self._session.scalars(
                update(Repository)
                .where(Repository.provider_installation_id == provider_installation_id)
                .values(installation_available=False)
                .returning(Repository.id)
            )
        )
        await self._session.execute(
            delete(GitHubUserRepositoryAccess).where(
                GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id
            )
        )
        return repository_ids

    async def _record_revocation(self, installation_id: UUID, repository_id: int) -> None:
        # Serialize with OAuth reconciliation before touching grants. Database time
        # after the lock also covers removals whose transaction began before OAuth.
        # NO KEY UPDATE allows onboarding FK KEY SHARE checks without a lock inversion.
        await self._session.execute(
            select(ProviderInstallation.id)
            .where(ProviderInstallation.id == installation_id)
            .with_for_update(key_share=True)
        )
        await self._session.execute(
            insert(GitHubInstallationAccessRevocation)
            .values(
                provider_installation_id=installation_id,
                repository_external_id=repository_id,
                revoked_at=func.clock_timestamp(),
            )
            .on_conflict_do_update(
                index_elements=[
                    GitHubInstallationAccessRevocation.provider_installation_id,
                    GitHubInstallationAccessRevocation.repository_external_id,
                ],
                set_={"revoked_at": func.clock_timestamp()},
            )
        )

    async def request_rules_refresh(self, repository_id: UUID) -> None:
        await request_rules_refresh(self._session, repository_id)

    async def get_active_rule_version(self, repository_id: UUID) -> PersistedRuleVersion | None:
        return await self._rule_versions.get_active_rule_version(repository_id)

    async def get_or_create_initial_rule_version(
        self, repository_id: UUID, version: int, rules: list[dict[str, object]]
    ) -> PersistedRuleVersion:
        return await self._rule_versions.get_or_create_initial_rule_version(
            repository_id, version, rules
        )


class SqlAlchemyInstallationRepositoriesUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose installation repository persistence in one explicit transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def repositories(self) -> SqlAlchemyInstallationRepositoryStore:
        return SqlAlchemyInstallationRepositoryStore(self.session)

    @property
    def index_purger(self) -> SqlAlchemyCodeIndexPurger:
        return SqlAlchemyCodeIndexPurger(self.session)
