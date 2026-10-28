"""PostgreSQL adapter of the index leader's enumeration (#143).

Like `SqlAlchemyCodeIndexStore`, it runs on the caller's session and never commits. The
garbage probe applies the branches of `collect_garbage`'s condition to the correlated state
columns, so the leader lists exactly the repositories garbage collection has work in.
"""

from __future__ import annotations

from datetime import timedelta
from uuid import UUID

from sqlalchemy import ColumnElement, and_, case, func, not_, or_, select, true
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.code_index.application.maintenance import (
    DisarmCandidate,
    HeadCandidate,
    WakeRequest,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.domain.enums import IndexStatus, PurgeReason
from app.modules.code_index.infrastructure.effective_rag import INDEXABLE_PROVIDER, RAG_ON
from app.modules.code_index.infrastructure.index_store import (
    DISPLACED_GRACE,
    SqlAlchemyCodeIndexUnitOfWork,
    garbage_branches,
)
from app.modules.code_index.infrastructure.models import CodeIndexFile, CodeIndexState
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository

# A repository to re-arm (ADR §4): no state row, or one purged by a removal or a toggle.
_REARM = and_(
    RAG_ON,
    INDEXABLE_PROVIDER,
    or_(
        CodeIndexState.repository_id.is_(None),
        CodeIndexState.purge_reason.in_([PurgeReason.REMOVED, PurgeReason.DISABLED]),
    ),
)
# A live index to disarm, and its purge reason: turned off by the user, else lost access.
_DISARM = and_(CodeIndexState.purge_reason.is_(None), not_(and_(RAG_ON, INDEXABLE_PROVIDER)))
_DISARM_REASON = case(
    (not_(Repository.rag_enabled), PurgeReason.DISABLED.value), else_=PurgeReason.REMOVED.value
)


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
        # garbage while any row is. One probe per branch of the garbage condition, each
        # stopping at its first row: the older-generation, abandoned and below-floor
        # branches range over the primary key prefix (repository, generation, valid_from),
        # and the closed branch reads the partial index `ix_code_index_files_closed`. A
        # repository without garbage thus reads none of its live rows; one probe of the OR
        # would filter every one of them. A probe is the first row in key order, not an
        # EXISTS: PostgreSQL drops the ORDER BY of an EXISTS and, guessing a third of the
        # rows match a correlated inequality, may prefer a sequential scan while the
        # visibility map lags behind a build; a sequential scan would have to sort for
        # the first row. `path` is a NOT NULL key column, so a non-NULL probe is a row.
        branches = garbage_branches(
            CodeIndexFile,
            generation=CodeIndexState.generation,
            published=CodeIndexState.published_publication,
            floor=CodeIndexState.floor_publication,
            leased=case((leased, CodeIndexState.reserved_publication)),
            displaced_due=and_(
                CodeIndexState.published_at.is_not(None),
                CodeIndexState.published_at <= now - DISPLACED_GRACE,
            ),
        )
        garbage = (
            select(CodeIndexFile.path)
            .where(CodeIndexFile.repository_id == CodeIndexState.repository_id, branch)
            .order_by(CodeIndexFile.generation, CodeIndexFile.valid_from)
            .limit(1)
            .scalar_subquery()
            .is_not(None)
            for branch in branches
        )
        return await self._list(
            or_(and_(leased, CodeIndexState.lease_expires_at <= now), *garbage), after, limit
        )

    async def wake_requests(self, limit: int) -> list[WakeRequest]:
        rows = await self._session.execute(
            select(
                CodeIndexState.repository_id,
                CodeIndexState.generation,
                CodeIndexState.wake_requested_at,
            )
            .where(
                CodeIndexState.wake_requested_at.is_not(None),
                or_(
                    CodeIndexState.lease_expires_at.is_(None),
                    CodeIndexState.lease_expires_at <= func.clock_timestamp(),
                ),
            )
            .order_by(CodeIndexState.wake_requested_at, CodeIndexState.repository_id)
            .limit(limit)
        )
        return [
            WakeRequest(row.repository_id, row.generation, row.wake_requested_at) for row in rows
        ]

    async def heads(self, after: UUID | None, limit: int) -> list[HeadCandidate]:
        rows = await self._session.execute(
            select(
                CodeIndexState.repository_id,
                CodeIndexState.generation,
                Repository.full_name,
                ProviderInstallation.provider,
                ProviderInstallation.external_id,
                func.clock_timestamp(),
            )
            .join(Repository, Repository.id == CodeIndexState.repository_id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(
                RAG_ON,
                INDEXABLE_PROVIDER,
                CodeIndexState.purge_reason.is_(None),
                CodeIndexState.status != IndexStatus.FAILED,
                true() if after is None else CodeIndexState.repository_id > after,
            )
            .order_by(CodeIndexState.repository_id)
            .limit(limit)
        )
        return [
            HeadCandidate(
                IndexedRepository(
                    repository_id=repository_id,
                    provider=provider,
                    installation_external_id=external_id,
                    full_name=full_name,
                ),
                generation,
                listed_at,
            )
            for repository_id, generation, full_name, provider, external_id, listed_at in (
                rows.tuples()
            )
        ]

    async def to_rearm(self, limit: int) -> list[UUID]:
        rows = await self._session.scalars(
            select(Repository.id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .outerjoin(CodeIndexState, CodeIndexState.repository_id == Repository.id)
            .where(_REARM)
            .order_by(Repository.id)
            .limit(limit)
        )
        return list(rows)

    async def to_disarm(self, limit: int) -> list[DisarmCandidate]:
        rows = await self._session.execute(
            select(CodeIndexState.repository_id, _DISARM_REASON)
            .join(Repository, Repository.id == CodeIndexState.repository_id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(_DISARM)
            .order_by(CodeIndexState.repository_id)
            .limit(limit)
        )
        return [
            DisarmCandidate(repository_id, PurgeReason(reason))
            for repository_id, reason in rows.tuples()
        ]

    async def _share_repository(self, repository_id: UUID) -> None:
        # The lock first, the condition in a statement of its own: under READ COMMITTED
        # it reads what a writer the lock waited for committed.
        await self._session.execute(
            select(Repository.id).where(Repository.id == repository_id).with_for_update(read=True)
        )

    async def rearm_due(self, repository_id: UUID) -> bool:
        await self._share_repository(repository_id)
        due = await self._session.scalar(
            select(Repository.id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .outerjoin(CodeIndexState, CodeIndexState.repository_id == Repository.id)
            .where(Repository.id == repository_id, _REARM)
        )
        return due is not None

    async def disarm_reason(self, repository_id: UUID) -> PurgeReason | None:
        await self._share_repository(repository_id)
        reason = await self._session.scalar(
            select(_DISARM_REASON)
            .select_from(CodeIndexState)
            .join(Repository, Repository.id == CodeIndexState.repository_id)
            .join(
                ProviderInstallation, ProviderInstallation.id == Repository.provider_installation_id
            )
            .where(CodeIndexState.repository_id == repository_id, _DISARM)
        )
        return None if reason is None else PurgeReason(reason)


class SqlAlchemyCodeIndexMaintenanceUnitOfWork(SqlAlchemyCodeIndexUnitOfWork):
    @property
    def maintenance(self) -> SqlAlchemyCodeIndexMaintenance:
        return SqlAlchemyCodeIndexMaintenance(self.session)
