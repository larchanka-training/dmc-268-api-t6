"""Unit-of-work composition for durable review-output publication."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyReviewOutputRepository


class SqlAlchemyReviewOutputUnitOfWork(SqlAlchemyUnitOfWork):
    """Expose review-output writes on one caller-owned transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        super().__init__(session_factory)

    @property
    def reviews(self) -> SqlAlchemyReviewOutputRepository:
        return SqlAlchemyReviewOutputRepository(self.session)
