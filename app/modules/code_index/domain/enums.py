"""Statuses, purge reasons and edge kinds of the graph index (ADR 0001 §3, §4, §6)."""

from __future__ import annotations

from enum import StrEnum


class IndexStatus(StrEnum):
    EMPTY = "empty"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class PurgeReason(StrEnum):
    TTL = "ttl"
    REMOVED = "removed"
    DISABLED = "disabled"


class EdgeKind(StrEnum):
    """Stored edge kinds; `defines` (ADR §3) is the symbol row itself, not an edge row."""

    IMPORTS = "imports"
    USES_IMPORTED = "uses_imported"


class RagIndexStatus(StrEnum):
    """The index status readers see (#145): the ADR list plus `disabled`.

    Derived from the repository toggle and the state row, never stored: `stale` is a
    READY row whose target commit moved on. The member order is the API contract order.
    """

    DISABLED = "disabled"
    EMPTY = "empty"
    BUILDING = "building"
    READY = "ready"
    STALE = "stale"
    FAILED = "failed"
