"""SQL read model for the current GitHub user and Workspace access."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.auth.application.get_me import CurrentUser, MyWorkspace
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.models import GitHubUserProfile
from app.modules.repositories.infrastructure.models import ProviderInstallation
from app.modules.workspaces.infrastructure.models import GitHubUserWorkspaceAccess, Workspace


class SqlAlchemyCurrentUserRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_current_user(self, scope: AuthScope) -> CurrentUser | None:
        async with self._session_factory() as session:
            profile = await session.scalar(
                select(GitHubUserProfile).where(GitHubUserProfile.id == scope.user_id)
            )
            if profile is None:
                return None
            rows = (
                await session.execute(
                    select(Workspace.id, Workspace.name, ProviderInstallation.external_id)
                    .join(
                        GitHubUserWorkspaceAccess,
                        GitHubUserWorkspaceAccess.workspace_id == Workspace.id,
                    )
                    .join(
                        ProviderInstallation,
                        ProviderInstallation.workspace_id == Workspace.id,
                    )
                    .where(
                        GitHubUserWorkspaceAccess.github_user_id == scope.user_id,
                        Workspace.id.in_(scope.workspace_ids),
                        ProviderInstallation.provider == "github",
                    )
                    .order_by(Workspace.id, ProviderInstallation.external_id)
                )
            ).all()
        return CurrentUser(
            profile.id,
            profile.login,
            profile.name,
            profile.avatar_url,
            tuple(MyWorkspace(*row) for row in rows),
        )
