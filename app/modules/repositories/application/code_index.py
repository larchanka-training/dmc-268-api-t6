"""Ports to the graph index the repository use cases write through (#143, ADR 0001 §6.2)."""

from __future__ import annotations

from typing import Protocol
from uuid import UUID

from app.modules.code_index.domain.enums import PurgeReason


class CodeIndexPurger(Protocol):
    """Works in the caller's transaction: flushes, never commits."""

    async def purge(self, repository_id: UUID, reason: PurgeReason) -> None:
        """Purge the index logically (ADR 0001 §4): its generation moves on, and garbage
        collection deletes its rows; the target is physical deletion within the hour.

        A missing or already purged index keeps its generation, so a repeat changes
        nothing.
        """
        ...


class CodeIndexEnabler(Protocol):
    """Works in the caller's transaction: flushes, never commits."""

    async def enable(self, repository_id: UUID) -> None:
        """Request a full build (ADR 0001 §4, §6.2): create the index with the wake-up
        marker, or re-arm a purged one (a TTL purge included) in a new generation.

        A live index and a `failed` one are left alone, so a repeat changes nothing and a
        failed index is retried only through off and on.
        """
        ...
