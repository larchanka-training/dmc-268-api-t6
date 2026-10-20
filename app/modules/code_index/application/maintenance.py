"""Port of the index leader's enumeration (#143).

The leader tasks list candidate repositories across every state row, so these methods
take no `repository_id`; each step on one repository goes through `CodeIndexStore`,
whose contract stays one repository per call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.modules.code_index.application.index_store import CodeIndexUnitOfWork
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.domain.enums import PurgeReason


@dataclass(frozen=True)
class WakeRequest:
    """A marked state row: the generation to wake and the marker to clear after."""

    repository_id: UUID
    generation: int
    requested_at: datetime


@dataclass(frozen=True)
class HeadCandidate:
    """A live index the leader reconciles with its default-branch head (ADR §6.1).

    `listed_at` is the database clock at the listing, so before any head read that
    follows it: a wake-up marker later than it is a trigger's after the read.
    """

    repository: IndexedRepository
    generation: int
    listed_at: datetime


@dataclass(frozen=True)
class DisarmCandidate:
    """A live index whose RAG is off, with the reason its purge records."""

    repository_id: UUID
    reason: PurgeReason


class CodeIndexMaintenance(Protocol):
    """Candidate repositories of the leader tasks; never commits.

    Each method returns up to `limit` candidates ordered by `repository_id`, except
    `wake_requests` (oldest marker first); `unused`, `with_garbage` and `heads` start
    after the cursor `after`. Times are compared with `clock_timestamp()`.

    The effective RAG of a repository is on while `rag_enabled`, `installation_available`
    and its installation is not deleted.
    """

    async def unused(self, unused_for: timedelta, after: UUID | None, limit: int) -> list[UUID]:
        """Live indexes (no purge reason, not `failed`) unused for longer than `unused_for`.

        The condition `CodeIndexStore.purge_unused` re-checks under its row lock.
        """
        ...

    async def with_garbage(self, after: UUID | None, limit: int) -> list[UUID]:
        """Indexes with an expired lease or with rows `collect_garbage` would delete."""
        ...

    async def wake_requests(self, limit: int) -> list[WakeRequest]:
        """Rows with a wake-up marker and no live lease.

        A row under a live lease keeps its marker: the job reads its target when it
        ends and wakes itself if the target moved (ADR §6.2).
        """
        ...

    async def heads(self, after: UUID | None, limit: int) -> list[HeadCandidate]:
        """Live, not `failed` indexes of GitHub repositories with effective RAG on."""
        ...

    async def to_rearm(self, limit: int) -> list[UUID]:
        """GitHub repositories with effective RAG on whose index is missing or purged as
        `removed` or `disabled`; a TTL purge is left to the next use (ADR §4)."""
        ...

    async def to_disarm(self, limit: int) -> list[DisarmCandidate]:
        """Indexes without a purge reason whose repository has effective RAG off:
        `disabled` when `rag_enabled` is off, else `removed`."""
        ...


class CodeIndexMaintenanceUnitOfWork(CodeIndexUnitOfWork, Protocol):
    @property
    def maintenance(self) -> CodeIndexMaintenance: ...
