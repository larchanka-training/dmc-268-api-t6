from __future__ import annotations

from enum import StrEnum

from app.common.domain.run_enums import Engine as Engine
from app.common.domain.run_enums import RunStatus

RunState = RunStatus


class WaitForCi(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class ReviewEvent(StrEnum):
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class SeverityThreshold(StrEnum):
    ALL = "all"
    WARNING = "warning"
    CRITICAL = "critical"


class CodeChangeState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class FindingSeverity(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class FindingCategory(StrEnum):
    SECURITY = "security"
    CORRECTNESS = "correctness"
    PERFORMANCE = "performance"
    READABILITY = "readability"


class FindingSide(StrEnum):
    LEFT = "LEFT"
    RIGHT = "RIGHT"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class LedgerKind(StrEnum):
    TOP_UP = "top_up"
    USAGE_DEBIT = "usage_debit"
    ADJUSTMENT = "adjustment"
