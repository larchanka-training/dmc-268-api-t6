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
