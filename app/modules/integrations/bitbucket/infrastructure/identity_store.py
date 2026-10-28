"""Workspace-scoped Bitbucket repository UUID lookup."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketInstallationIdentity,
    BitbucketRepositoryIdentity,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


async def resolve_bitbucket_repository(
    session: AsyncSession,
    *,
    workspace_id: UUID,
    installation_id: UUID,
    repository_uuid: UUID,
) -> UUID | None:
    """Find an active binding without accepting a slug or another owner's UUID."""
    statement = (
        select(Repository.id)
        .select_from(BitbucketRepositoryIdentity)
        .join(Repository, Repository.id == BitbucketRepositoryIdentity.repository_id)
        .join(
            BitbucketInstallationIdentity,
            BitbucketInstallationIdentity.provider_installation_id
            == BitbucketRepositoryIdentity.provider_installation_id,
        )
        .join(
            ProviderInstallation,
            ProviderInstallation.id == BitbucketInstallationIdentity.provider_installation_id,
        )
        .where(
            BitbucketRepositoryIdentity.workspace_id == workspace_id,
            BitbucketRepositoryIdentity.provider_installation_id == installation_id,
            BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
            BitbucketInstallationIdentity.workspace_id == workspace_id,
            ProviderInstallation.workspace_id == workspace_id,
            ProviderInstallation.provider == "bitbucket",
            ProviderInstallation.deleted_at.is_(None),
            Repository.provider_installation_id == installation_id,
            Repository.installation_available.is_(True),
        )
    )
    result = await session.execute(statement)
    return result.scalar_one_or_none()
