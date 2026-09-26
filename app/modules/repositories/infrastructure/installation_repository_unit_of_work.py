"""SQLAlchemy persistence composition for installation repository synchronization."""

from __future__ import annotations

from typing import cast
from uuid import UUID

from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.repositories.application.installation_repositories import RepositorySnapshot
from app.modules.repositories.application.onboard_repository import PersistedRuleVersion
from app.modules.repositories.infrastructure.models import Repository
from app.modules.repositories.infrastructure.rule_version_repository import (
    SqlAlchemyRepositoryRuleVersionStore,
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
                },
            )
            .returning(Repository.id)
        )
        repository_id = cast(UUID | None, await self._session.scalar(statement))
        if repository_id is None:
            raise RuntimeError("repository upsert did not return an id")
        return repository_id

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
