"""Port of the index leader's enumeration (#143).

The leader tasks list candidate repositories across every state row, so these methods
take no `repository_id`; each step on one repository goes through `CodeIndexStore`,
whose contract stays one repository per call.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Protocol
from uuid import UUID

from app.modules.code_index.application.index_store import CodeIndexUnitOfWork


class CodeIndexMaintenance(Protocol):
    """Candidate repositories of the leader tasks; never commits.

    Each method returns up to `limit` repository ids after the cursor `after`, ordered by
    `repository_id`, and compares times with `clock_timestamp()`.
    """

    async def unused(self, unused_for: timedelta, after: UUID | None, limit: int) -> list[UUID]:
        """Live indexes (no purge reason, not `failed`) unused for longer than `unused_for`.

        The condition `CodeIndexStore.purge_unused` re-checks under its row lock.
        """
        ...

    async def with_garbage(self, after: UUID | None, limit: int) -> list[UUID]:
        """Indexes with an expired lease or with rows `collect_garbage` would delete."""
        ...


class CodeIndexMaintenanceUnitOfWork(CodeIndexUnitOfWork, Protocol):
    @property
    def maintenance(self) -> CodeIndexMaintenance: ...
