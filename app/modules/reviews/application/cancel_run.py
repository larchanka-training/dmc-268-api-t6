"""Application service for an idempotent review-run cancellation request."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.list_runs import RunListItem


class CancelRunRepository(Protocol):
    async def request_cancel(self, run_id: UUID) -> bool: ...

    async def get_run(self, run_id: UUID) -> RunListItem | None: ...


class CancelRun:
    def __init__(self, repository: CancelRunRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> RunListItem | None:
        if not await self._repository.request_cancel(run_id):
            return None
        return await self._repository.get_run(run_id)
