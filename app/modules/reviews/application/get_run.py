"""Application service and read-model contract for a single review run."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.list_runs import RunListItem


class RunDetailRepository(Protocol):
    async def get_run(self, run_id: UUID) -> RunListItem | None: ...


class GetRun:
    def __init__(self, repository: RunDetailRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> RunListItem | None:
        return await self._repository.get_run(run_id)
