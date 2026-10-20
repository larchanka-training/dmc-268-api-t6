"""PostgreSQL adapter of the repositories module's `CodeIndexPurger` port (#143).

The repositories units of work build it on their own session, so the purge commits with
the toggle or the removal that caused it.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.code_index.domain.enums import PurgeReason
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexStore


class SqlAlchemyCodeIndexPurger:
    """`SqlAlchemyCodeIndexStore.purge` on the caller's session; never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._store = SqlAlchemyCodeIndexStore(session)

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        await self._store.purge(repository_id, reason)
