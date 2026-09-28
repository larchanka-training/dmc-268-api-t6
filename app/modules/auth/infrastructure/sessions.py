"""Flush-only persistence for local auth sessions."""

from __future__ import annotations

from datetime import datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.exchange_github_code import AuthenticatedUser
from app.modules.auth.application.refresh_session import (
    RefreshSessionRecord,
    RefreshTokenFamily,
)
from app.modules.auth.infrastructure.models import (
    AuthRefreshFamily,
    AuthRefreshSession,
    GitHubUserProfile,
)
from app.modules.workspaces.infrastructure.models import GitHubUserWorkspaceAccess


class SqlAlchemyAuthSessionStore:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(
        self, user: AuthenticatedUser, token_hash: str, family_id: UUID, expires_at: datetime
    ) -> None:
        await self._session.execute(
            insert(GitHubUserProfile)
            .values(id=user.id, login=user.login, name=user.name, avatar_url=user.avatar_url)
            .on_conflict_do_update(
                index_elements=[GitHubUserProfile.id],
                set_={
                    "login": user.login,
                    "name": user.name,
                    "avatar_url": user.avatar_url,
                    "updated_at": func.now(),
                },
            )
        )
        self._session.add(
            AuthRefreshFamily(
                id=family_id,
                github_user_id=user.id,
                expires_at=expires_at,
            )
        )
        await self._session.flush()
        self._session.add(
            AuthRefreshSession(
                id=uuid4(),
                family_id=family_id,
                github_user_id=user.id,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def find_family_id(self, token_hash: str) -> UUID | None:
        return cast(
            UUID | None,
            await self._session.scalar(
                select(AuthRefreshSession.family_id).where(
                    AuthRefreshSession.token_hash == token_hash
                )
            ),
        )

    async def lock_family(self, family_id: UUID) -> RefreshTokenFamily | None:
        row = await self._session.scalar(
            select(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id).with_for_update()
        )
        if row is None:
            return None
        return RefreshTokenFamily(row.id, row.expires_at, row.revoked_at)

    async def get_session(self, token_hash: str) -> RefreshSessionRecord | None:
        row = await self._session.scalar(
            select(AuthRefreshSession).where(AuthRefreshSession.token_hash == token_hash)
        )
        if row is None:
            return None
        return RefreshSessionRecord(
            row.id,
            row.family_id,
            row.github_user_id,
            row.expires_at,
            row.rotated_at,
            row.revoked_at,
        )

    async def current_identity(self, user_id: int) -> tuple[AuthenticatedUser, tuple[UUID, ...]]:
        profile = await self._session.scalar(
            select(GitHubUserProfile).where(GitHubUserProfile.id == user_id)
        )
        if profile is None:
            raise RuntimeError("refresh session has no user profile")
        workspace_ids = (
            await self._session.scalars(
                select(GitHubUserWorkspaceAccess.workspace_id)
                .where(GitHubUserWorkspaceAccess.github_user_id == user_id)
                .order_by(GitHubUserWorkspaceAccess.workspace_id)
            )
        ).all()
        return (
            AuthenticatedUser(profile.id, profile.login, profile.name, profile.avatar_url),
            tuple(workspace_ids),
        )

    async def rotate(
        self,
        session_id: UUID,
        *,
        new_token_hash: str,
        at: datetime,
        expires_at: datetime,
    ) -> None:
        row = await self._session.scalar(
            select(AuthRefreshSession).where(AuthRefreshSession.id == session_id)
        )
        if row is None or row.rotated_at is not None or row.revoked_at is not None:
            raise RuntimeError("refresh session changed after family lock")
        row.rotated_at = at
        await self._session.execute(
            update(AuthRefreshFamily)
            .where(AuthRefreshFamily.id == row.family_id)
            .values(expires_at=expires_at)
        )
        self._session.add(
            AuthRefreshSession(
                id=uuid4(),
                family_id=row.family_id,
                github_user_id=row.github_user_id,
                token_hash=new_token_hash,
                expires_at=expires_at,
            )
        )
        await self._session.flush()

    async def revoke_family(self, family_id: UUID, at: datetime) -> None:
        await self._session.execute(
            update(AuthRefreshFamily).where(AuthRefreshFamily.id == family_id).values(revoked_at=at)
        )
        await self._session.execute(
            update(AuthRefreshSession)
            .where(AuthRefreshSession.family_id == family_id)
            .values(revoked_at=at)
        )


class SqlAlchemyAuthSessionUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def sessions(self) -> SqlAlchemyAuthSessionStore:
        return SqlAlchemyAuthSessionStore(self.session)
