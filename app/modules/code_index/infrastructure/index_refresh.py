"""PostgreSQL adapter of a webhook trigger's index update request (#143).

The delivery's repository resolves by its installation and repository external ids
under the same effective-RAG condition as the leader's enumeration; the request itself
is `SqlAlchemyCodeIndexStore.request_build` on the same session.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.code_index.infrastructure.effective_rag import GITHUB_PROVIDER, RAG_ON
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


class SqlAlchemyIndexRefreshRepositories:
    """`IndexRefreshRepositories` on the caller's session; never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def resolve(
        self, installation_external_id: int, repository_external_id: int
    ) -> UUID | None:
        repository_id: UUID | None = await self._session.scalar(
            select(Repository.id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(
                GITHUB_PROVIDER,
                RAG_ON,
                ProviderInstallation.external_id == installation_external_id,
                Repository.external_id == repository_external_id,
            )
        )
        return repository_id


class SqlAlchemyIndexRefreshUnitOfWork(SqlAlchemyCodeIndexUnitOfWork):
    @property
    def repositories(self) -> SqlAlchemyIndexRefreshRepositories:
        return SqlAlchemyIndexRefreshRepositories(self.session)
