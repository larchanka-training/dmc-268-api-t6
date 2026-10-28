"""Current repository visibility shared by Run and future repository queries."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_, or_, select

from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import (
    GitHubUserRepositoryAccess,
    GitHubUserWorkspaceAccess,
)


def repository_access_predicate(scope: AuthScope) -> ColumnElement[bool]:
    """Require the JWT claim and the user's latest installation/repository grants."""
    github_access = (
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
    bitbucket_access = (
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
            BitbucketConnection,
            BitbucketConnection.provider_installation_id == ProviderInstallation.id,
        )
        .join(
            BitbucketRepositoryIdentity,
            BitbucketRepositoryIdentity.provider_installation_id == ProviderInstallation.id,
        )
        .where(
            ProviderInstallation.id == Repository.provider_installation_id,
            ProviderInstallation.provider == "bitbucket",
            ProviderInstallation.workspace_id.in_(scope.workspace_ids),
            ProviderInstallation.deleted_at.is_(None),
            BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
            BitbucketConnection.revoked_at.is_(None),
            BitbucketRepositoryIdentity.repository_id == Repository.id,
            BitbucketRepositoryIdentity.workspace_id == ProviderInstallation.workspace_id,
            Repository.installation_available.is_(True),
        )
        .correlate(Repository)
        .exists()
    )
    return or_(github_access, bitbucket_access)
