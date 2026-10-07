"""SQLAlchemy persistence composition for installation repository synchronization."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.integrations.webhooks.infrastructure.models import GitHubInstallationRemovalEffect
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
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

    async def upsert_repository(
        self, provider_installation_id: UUID, snapshot: RepositorySnapshot
    ) -> UUID:
        """Create or update a repository without a select-then-insert race."""
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
                    "enabled": True,
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

    async def disable_repository(self, provider_installation_id: UUID, external_id: int) -> None:
        """Disable a repository if it still belongs to this installation.

        The use case deduplicates delivery effects before calling this method.
        A distinct later removal must revoke any access restored in the meantime.
        """
        await self._record_revocation(provider_installation_id, external_id)
        statement = (
            update(Repository)
            .where(
                Repository.provider_installation_id == provider_installation_id,
                Repository.external_id == external_id,
            )
            .values(enabled=False)
        )
        await self._session.execute(statement)
        await self._session.execute(
            delete(GitHubUserRepositoryAccess).where(
                GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id,
                GitHubUserRepositoryAccess.repository_external_id == external_id,
            )
        )

    async def disable_installation(self, provider_installation_id: UUID) -> None:
        await self._record_revocation(provider_installation_id, 0)
        await self._session.execute(
            update(Repository)
            .where(Repository.provider_installation_id == provider_installation_id)
            .values(enabled=False)
        )
        await self._session.execute(
            delete(GitHubUserRepositoryAccess).where(
                GitHubUserRepositoryAccess.provider_installation_id == provider_installation_id
            )
        )

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
