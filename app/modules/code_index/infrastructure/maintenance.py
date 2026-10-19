"""PostgreSQL adapter of the index leader's enumeration (#143).

Like `SqlAlchemyCodeIndexStore`, it runs on the caller's session and never commits. The
garbage probe applies `garbage_condition` of `collect_garbage` to the correlated state
columns, so the leader lists exactly the repositories garbage collection has work in.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.infrastructure.index_store import (
    DISPLACED_GRACE,
    SqlAlchemyCodeIndexUnitOfWork,
    garbage_condition,
)
from app.modules.code_index.infrastructure.models import CodeIndexFile, CodeIndexState


class SqlAlchemyCodeIndexMaintenance:
    """Candidate repositories of the leader tasks; never commits."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _list(
        self, condition: ColumnElement[bool], after: UUID | None, limit: int
    ) -> list[UUID]:
        rows = await self._session.scalars(
            select(CodeIndexState.repository_id)
            .where(
                condition,
                true() if after is None else CodeIndexState.repository_id > after,
            )
            .order_by(CodeIndexState.repository_id)
            .limit(limit)
        )
        return list(rows)

    async def unused(self, unused_for: timedelta, after: UUID | None, limit: int) -> list[UUID]:
        # A purge keeps last_used_at, so the purge reason is checked too.
        return await self._list(
            and_(
                CodeIndexState.purge_reason.is_(None),
                CodeIndexState.status != IndexStatus.FAILED,
                CodeIndexState.last_used_at < func.clock_timestamp() - unused_for,
            ),
            after,
            limit,
        )

    async def with_garbage(self, after: UUID | None, limit: int) -> list[UUID]:
        now = func.clock_timestamp()
        leased = CodeIndexState.lease_token.is_not(None)
        # The rows of a file and its symbols, edges and imports share its generation,
        # publications and closing, and GC deletes the file row last; so a file row is
        # garbage while any row is. The probe stops at the first garbage file row, but a
        # repository without garbage has every live file row read (the primary key prefix
        # only narrows the scan to the repository). The next #143 migration adds the
        # partial index `code_index_files (repository_id) WHERE valid_to IS NOT NULL`.
        garbage = (
            select(CodeIndexFile.path)
            .where(
                CodeIndexFile.repository_id == CodeIndexState.repository_id,
                garbage_condition(
                    CodeIndexFile,
                    generation=CodeIndexState.generation,
                    published=CodeIndexState.published_publication,
                    floor=CodeIndexState.floor_publication,
                    leased=case((leased, CodeIndexState.reserved_publication)),
                    displaced_due=and_(
                        CodeIndexState.published_at.is_not(None),
                        CodeIndexState.published_at <= now - DISPLACED_GRACE,
                    ),
                ),
            )
            .exists()
        )
        return await self._list(
            or_(and_(leased, CodeIndexState.lease_expires_at <= now), garbage), after, limit
        )


class SqlAlchemyCodeIndexMaintenanceUnitOfWork(SqlAlchemyCodeIndexUnitOfWork):
    @property
    def maintenance(self) -> SqlAlchemyCodeIndexMaintenance:
        return SqlAlchemyCodeIndexMaintenance(self.session)
