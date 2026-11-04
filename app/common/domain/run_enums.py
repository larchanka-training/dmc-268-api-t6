from __future__ import annotations

from enum import StrEnum


class Engine(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"
