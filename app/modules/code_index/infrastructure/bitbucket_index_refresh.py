"""A signed Bitbucket default-branch push wakes only its active portal index."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.infrastructure.effective_rag import INDEXABLE_PROVIDER, RAG_ON
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


class SqlAlchemyBitbucketIndexRefresh:
    """Short transaction; no provider I/O and no cross-tenant lookup by remote UUID."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def execute(
        self,
        installation_id: UUID,
        repository_id: UUID,
        repository_uuid: UUID,
        branches: frozenset[str],
        binding_generation: int,
        webhook_generation: int,
    ) -> bool:
        if not branches:
            return False
        async with self._sessions.begin() as session:
            row = (
                await session.execute(
                    select(Repository.id, Repository.default_branch)
                    .select_from(Repository)
                    .join(
                        ProviderInstallation,
                        ProviderInstallation.id == Repository.provider_installation_id,
                    )
                    .join(
                        BitbucketRepositoryIdentity,
                        BitbucketRepositoryIdentity.repository_id == Repository.id,
                    )
                    .join(
                        BitbucketRepositoryWebhookSecret,
                        BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
                    )
                    .join(
                        BitbucketConnection,
                        BitbucketConnection.provider_installation_id == ProviderInstallation.id,
                    )
                    .where(
                        Repository.id == repository_id,
                        Repository.provider_installation_id == installation_id,
                        Repository.enabled.is_(True),
                        BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
                        BitbucketRepositoryIdentity.workspace_id
                        == ProviderInstallation.workspace_id,
                        BitbucketRepositoryWebhookSecret.binding_generation == binding_generation,
                        BitbucketRepositoryWebhookSecret.generation == webhook_generation,
                        BitbucketRepositoryWebhookSecret.secret_ciphertext.is_not(None),
                        BitbucketRepositoryWebhookSecret.confirmed_at.is_not(None),
                        BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
                        BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
                        RAG_ON,
                        INDEXABLE_PROVIDER,
                    )
                    .with_for_update(
                        of=(Repository, BitbucketConnection, BitbucketRepositoryWebhookSecret)
                    )
                )
            ).one_or_none()
            if row is None or row.default_branch not in branches:
                return False
            return await SqlAlchemyCodeIndexStore(session).request_build(
                repository_id, target_commit_sha=None
            )
