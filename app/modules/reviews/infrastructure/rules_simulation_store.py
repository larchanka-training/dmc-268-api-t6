"""PostgreSQL adapter of rules playground simulations (#311), scoped by the portal claim."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWork
from app.modules.auth.application.scope import AuthScope
from app.modules.repositories.infrastructure.models import Repository
from app.modules.reviews.application.rules_simulation import (
    ACTIVE_SIMULATION_STATUSES,
    DEADLINE_EXCEEDED,
    RulesSimulation,
    RulesSimulationRequest,
    RulesSimulationStatus,
)
from app.modules.reviews.infrastructure.models import RulesSimulation as RulesSimulationRow
from app.modules.workspaces.infrastructure.repository_access import repository_access_predicate

# The predicate of ``uq_rules_simulations_one_active_per_user``, as ON CONFLICT infers it.
_ACTIVE_INDEX_WHERE = text("status IN ('queued', 'running')")


class RulesSimulationStoreError(RuntimeError):
    """A database failure while storing a simulation, raised without the statement's values.

    A driver error quotes its parameters, and they hold the pasted code and rules.
    """


class SqlAlchemyRulesSimulationStore:
    def __init__(self, session: AsyncSession, scope: AuthScope | None) -> None:
        if scope is None:
            raise ValueError("SqlAlchemyRulesSimulationStore requires an AuthScope")
        self._session = session
        self._scope = scope

    async def repository_visible(self, repository_id: UUID) -> bool:
        found = await self._session.scalar(
            select(Repository.id).where(
                Repository.id == repository_id, repository_access_predicate(self._scope)
            )
        )
        return found is not None

    async def expire_overdue(self, *, cutoff: datetime, now: datetime) -> None:
        await self._session.execute(
            update(RulesSimulationRow)
            .where(
                RulesSimulationRow.github_user_id == self._scope.user_id,
                RulesSimulationRow.status.in_(ACTIVE_SIMULATION_STATUSES),
                RulesSimulationRow.created_at <= cutoff,
            )
            .values(
                status=RulesSimulationStatus.FAILED,
                error_code=DEADLINE_EXCEEDED,
                finished_at=now,
            )
            .execution_options(synchronize_session=False)
        )

    async def create(
        self, simulation_id: UUID, request: RulesSimulationRequest, *, created_at: datetime
    ) -> RulesSimulation | None:
        statement = (
            insert(RulesSimulationRow)
            .values(
                id=simulation_id,
                repository_id=request.repository_id,
                github_user_id=self._scope.user_id,
                mode=request.mode,
                path=request.path,
                content=request.content,
                rules_md=request.rules_md,
                status=RulesSimulationStatus.QUEUED,
                created_at=created_at,
            )
            .on_conflict_do_nothing(
                index_elements=[RulesSimulationRow.github_user_id],
                index_where=_ACTIVE_INDEX_WHERE,
            )
            .returning(RulesSimulationRow.id)
        )
        try:
            inserted = await self._session.scalar(statement)
        except DBAPIError:
            raise RulesSimulationStoreError("storing the rules simulation failed") from None
        if inserted is None:
            return None
        return RulesSimulation(
            id=simulation_id,
            repository_id=request.repository_id,
            mode=request.mode,
            path=request.path,
            status=RulesSimulationStatus.QUEUED,
            error_code=None,
            created_at=created_at,
            finished_at=None,
        )

    async def active_id(self) -> UUID | None:
        active: UUID | None = await self._session.scalar(
            select(RulesSimulationRow.id).where(
                RulesSimulationRow.github_user_id == self._scope.user_id,
                RulesSimulationRow.status.in_(ACTIVE_SIMULATION_STATUSES),
            )
        )
        return active

    async def get(self, simulation_id: UUID) -> RulesSimulation | None:
        # Columns only: the stored code and rules are never read back here.
        row = (
            await self._session.execute(
                select(
                    RulesSimulationRow.id,
                    RulesSimulationRow.repository_id,
                    RulesSimulationRow.mode,
                    RulesSimulationRow.path,
                    RulesSimulationRow.status,
                    RulesSimulationRow.error_code,
                    RulesSimulationRow.created_at,
                    RulesSimulationRow.finished_at,
                )
                .join(Repository, Repository.id == RulesSimulationRow.repository_id)
                .where(
                    RulesSimulationRow.id == simulation_id,
                    RulesSimulationRow.github_user_id == self._scope.user_id,
                    repository_access_predicate(self._scope),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return RulesSimulation(
            id=row.id,
            repository_id=row.repository_id,
            mode=row.mode,
            path=row.path,
            status=row.status,
            error_code=row.error_code,
            created_at=row.created_at,
            finished_at=row.finished_at,
        )


class SqlAlchemyRulesSimulationUnitOfWork(SqlAlchemyUnitOfWork):
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], scope: AuthScope | None
    ) -> None:
        if scope is None:
            raise ValueError("SqlAlchemyRulesSimulationUnitOfWork requires an AuthScope")
        super().__init__(session_factory)
        self._scope = scope

    @property
    def simulations(self) -> SqlAlchemyRulesSimulationStore:
        return SqlAlchemyRulesSimulationStore(self.session, self._scope)
