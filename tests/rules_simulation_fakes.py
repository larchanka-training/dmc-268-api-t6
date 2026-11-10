"""An in-memory rules simulation store of one caller, shared by the #311 unit and contract tests."""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Self
from uuid import UUID

from app.modules.reviews.application.rules_simulation import (
    ACTIVE_SIMULATION_STATUSES,
    DEADLINE_EXCEEDED,
    RulesSimulation,
    RulesSimulationRequest,
    RulesSimulationStatus,
)

CALLER_ID = 42
VISIBLE_REPOSITORY_ID = UUID("00000000-0000-4000-8000-0000000000a2")


class FakeRulesSimulations:
    """Store and unit of work in one; a change is seen by others only after ``commit``.

    ``rows`` maps a simulation id to its owner's user id and the simulation.
    """

    def __init__(
        self,
        *,
        user_id: int = CALLER_ID,
        visible: frozenset[UUID] = frozenset({VISIBLE_REPOSITORY_ID}),
    ) -> None:
        self.user_id = user_id
        self.visible = visible
        self.rows: dict[UUID, tuple[int, RulesSimulation]] = {}
        self.entered = 0
        self._working: dict[UUID, tuple[int, RulesSimulation]] | None = None

    def for_user(self, user_id: int) -> FakeRulesSimulations:
        """Another caller over the same rows."""
        other = FakeRulesSimulations(user_id=user_id, visible=self.visible)
        other.rows = self.rows
        return other

    @property
    def simulations(self) -> Self:
        return self

    async def __aenter__(self) -> Self:
        self.entered += 1
        self._working = dict(self.rows)
        return self

    async def __aexit__(self, *args: object) -> None:
        self._working = None

    async def commit(self) -> None:
        self.rows.clear()
        self.rows.update(self._rows)

    async def rollback(self) -> None:
        self._working = dict(self.rows)

    @property
    def _rows(self) -> dict[UUID, tuple[int, RulesSimulation]]:
        assert self._working is not None, "the unit of work is not active"
        return self._working

    async def repository_visible(self, repository_id: UUID) -> bool:
        return repository_id in self.visible

    async def expire_overdue(self, *, cutoff: datetime, now: datetime) -> None:
        for simulation_id, (owner, item) in list(self._rows.items()):
            if (
                owner == self.user_id
                and item.status in ACTIVE_SIMULATION_STATUSES
                and item.created_at <= cutoff
            ):
                self._rows[simulation_id] = (
                    owner,
                    dataclasses.replace(
                        item,
                        status=RulesSimulationStatus.FAILED,
                        error_code=DEADLINE_EXCEEDED,
                        finished_at=now,
                    ),
                )

    async def create(
        self, simulation_id: UUID, request: RulesSimulationRequest, *, created_at: datetime
    ) -> RulesSimulation | None:
        if await self.active_id() is not None:
            return None
        simulation = RulesSimulation(
            id=simulation_id,
            repository_id=request.repository_id,
            mode=request.mode,
            path=request.path,
            status=RulesSimulationStatus.QUEUED,
            error_code=None,
            created_at=created_at,
            finished_at=None,
        )
        self._rows[simulation_id] = (self.user_id, simulation)
        return simulation

    async def active_id(self) -> UUID | None:
        return next(
            (
                item.id
                for owner, item in self._rows.values()
                if owner == self.user_id and item.status in ACTIVE_SIMULATION_STATUSES
            ),
            None,
        )

    async def get(self, simulation_id: UUID) -> RulesSimulation | None:
        row = self._rows.get(simulation_id)
        if row is None or row[0] != self.user_id or row[1].repository_id not in self.visible:
            return None
        return row[1]
