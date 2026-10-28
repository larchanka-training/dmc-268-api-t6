"""Resolve a signed Bitbucket push to an active portal index under ordered locks."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.workspaces.infrastructure.models import Workspace


class SqlAlchemyBitbucketIndexRefreshRepositories:
    """Read the binding on the caller's transaction; never commit."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> UUID | None:
        workspace_id = await self._session.scalar(
            select(ProviderInstallation.workspace_id).where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.provider == "bitbucket",
            )
        )
        if workspace_id is None:
            return None
        # Management, receipt admission and projection take these rows in the same
        # hierarchy. The first installation read is immutable and takes no row lock.
        workspace = await self._session.scalar(
            select(Workspace.id).where(Workspace.id == workspace_id).with_for_update()
        )
        if workspace is None:
            return None
        installation = await self._session.scalar(
            select(ProviderInstallation)
            .where(
                ProviderInstallation.id == installation_id,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
            )
            .with_for_update()
        )
        if installation is None or installation.deleted_at is not None:
            return None
        connection = await self._session.scalar(
            select(BitbucketConnection)
            .where(
                BitbucketConnection.provider_installation_id == installation_id,
                BitbucketConnection.workspace_id == workspace_id,
            )
            .with_for_update()
        )
        if (
            connection is None
            or connection.revoked_at is not None
            or connection.token_ciphertext is None
        ):
            return None
        repository = await self._session.scalar(
            select(Repository)
            .where(
                Repository.id == repository_id,
                Repository.provider_installation_id == installation_id,
            )
            .with_for_update()
        )
        if (
            repository is None
            or not repository.enabled
            or not repository.installation_available
            or not repository.rag_enabled
            or repository.default_branch not in branches
        ):
            return None
        identity = await self._session.scalar(
            select(BitbucketRepositoryIdentity).where(
                BitbucketRepositoryIdentity.repository_id == repository_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
                BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
            )
        )
        if identity is None:
            return None
        secret = await self._session.scalar(
            select(BitbucketRepositoryWebhookSecret)
            .where(BitbucketRepositoryWebhookSecret.repository_id == repository_id)
            .with_for_update()
        )
        if (
            secret is None
            or secret.binding_generation != binding_generation
            or secret.generation != webhook_generation
            or secret.secret_ciphertext is None
            or secret.confirmed_at is None
            or secret.revoked_at is not None
        ):
            return None
        return repository.id


class SqlAlchemyBitbucketIndexRefreshUnitOfWork(SqlAlchemyCodeIndexUnitOfWork):
    @property
    def repositories(self) -> SqlAlchemyBitbucketIndexRefreshRepositories:
        return SqlAlchemyBitbucketIndexRefreshRepositories(self.session)
