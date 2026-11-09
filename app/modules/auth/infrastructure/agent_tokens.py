"""Persistence for repository-bound agent tokens."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.agent_tokens import AgentTokenRecord, OwnedAgentToken
from app.modules.auth.infrastructure.models import AuthAgentToken
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import (
    GitHubUserRepositoryAccess,
    GitHubUserWorkspaceAccess,
)


class SqlAlchemyAgentTokenStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def authorized_repo(self, user_id: int, full_name: str) -> tuple[UUID, UUID] | None:
        row = (
            await self._session.execute(
                select(Repository.id, ProviderInstallation.workspace_id)
                .join(
                    ProviderInstallation,
                    Repository.provider_installation_id == ProviderInstallation.id,
                )
                .join(
                    GitHubUserWorkspaceAccess,
                    and_(
                        GitHubUserWorkspaceAccess.workspace_id == ProviderInstallation.workspace_id,
                        GitHubUserWorkspaceAccess.github_user_id == user_id,
                    ),
                )
                .join(
                    GitHubUserRepositoryAccess,
                    and_(
                        GitHubUserRepositoryAccess.provider_installation_id
                        == ProviderInstallation.id,
                        GitHubUserRepositoryAccess.repository_external_id == Repository.external_id,
                        GitHubUserRepositoryAccess.github_user_id == user_id,
                    ),
                )
                .where(
                    Repository.full_name == full_name,
                    Repository.installation_available.is_(True),
                    ProviderInstallation.provider == "github",
                    ProviderInstallation.deleted_at.is_(None),
                )
            )
        ).first()
        return (row[0], row[1]) if row is not None else None

    async def create(self, record: AgentTokenRecord) -> None:
        self._session.add(
            AuthAgentToken(
                id=record.id,
                token_hash=record.token_hash,
                github_user_id=record.user_id,
                repository_id=record.repository_id,
                workspace_id=record.workspace_id,
                expires_at=record.expires_at,
            )
        )
        await self._session.flush()

    async def find(self, token_hash: str) -> AgentTokenRecord | None:
        row = await self._session.scalar(
            select(AuthAgentToken).where(AuthAgentToken.token_hash == token_hash)
        )
        if row is None:
            return None
        return AgentTokenRecord(
            row.id,
            row.token_hash,
            row.github_user_id,
            row.repository_id,
            row.workspace_id,
            row.expires_at,
            row.revoked_at,
        )

    async def list_active(self, user_id: int, at: datetime) -> list[OwnedAgentToken]:
        rows = (
            await self._session.execute(
                select(
                    AuthAgentToken.id,
                    Repository.full_name,
                    AuthAgentToken.created_at,
                    AuthAgentToken.expires_at,
                )
                .join(Repository, AuthAgentToken.repository_id == Repository.id)
                .where(
                    AuthAgentToken.github_user_id == user_id,
                    AuthAgentToken.revoked_at.is_(None),
                    AuthAgentToken.expires_at > at,
                )
                .order_by(AuthAgentToken.created_at.desc())
            )
        ).all()
        return [OwnedAgentToken(row[0], row[1], row[2], row[3]) for row in rows]

    async def revoke(self, token_id: UUID, user_id: int, at: datetime) -> bool:
        row = await self._session.scalar(
            select(AuthAgentToken)
            .where(
                AuthAgentToken.id == token_id,
                AuthAgentToken.github_user_id == user_id,
                AuthAgentToken.revoked_at.is_(None),
            )
            .with_for_update()
        )
        if row is None:
            return False
        row.revoked_at = at
        await self._session.flush()
        return True


class SqlAlchemyAgentTokenUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def tokens(self) -> SqlAlchemyAgentTokenStore:
        return SqlAlchemyAgentTokenStore(self.session)
