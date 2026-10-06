"""SQLAlchemy transaction composition for review run input processing."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository


class SqlAlchemyRunProcessingUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose diff snapshots and blob cache persistence in one explicit transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def repository(self) -> SqlAlchemyRunRepository:
        return SqlAlchemyRunRepository(session=self.session, allow_unscoped=True)

    @property
    def blob_cache(self) -> SqlAlchemyBlobCache:
        return SqlAlchemyBlobCache(self.session)
