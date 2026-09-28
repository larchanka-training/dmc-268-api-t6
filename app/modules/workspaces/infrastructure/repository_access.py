"""Current repository visibility shared by Run and future repository queries."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, select

from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import (
    GitHubUserRepositoryAccess,
    GitHubUserWorkspaceAccess,
)


def repository_access_predicate(scope: AuthScope) -> ColumnElement[bool]:
    """Require the JWT claim and the user's latest installation/repository grants."""
    return (
        select(1)
        .select_from(ProviderInstallation)
        .join(
            GitHubUserWorkspaceAccess,
            and_(
                GitHubUserWorkspaceAccess.workspace_id == ProviderInstallation.workspace_id,
                GitHubUserWorkspaceAccess.github_user_id == scope.user_id,
            ),
        )
        .join(
            GitHubUserRepositoryAccess,
            and_(
                GitHubUserRepositoryAccess.provider_installation_id == ProviderInstallation.id,
                GitHubUserRepositoryAccess.repository_external_id == Repository.external_id,
                GitHubUserRepositoryAccess.github_user_id == scope.user_id,
            ),
        )
        .where(
            ProviderInstallation.id == Repository.provider_installation_id,
            ProviderInstallation.provider == "github",
            ProviderInstallation.workspace_id.in_(scope.workspace_ids),
        )
        .correlate(Repository)
        .exists()
    )
