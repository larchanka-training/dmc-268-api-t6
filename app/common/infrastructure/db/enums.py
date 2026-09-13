from enum import StrEnum


class Engine(StrEnum):
    FAST = "fast"
    DEEP = "deep"


class WaitForCi(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


class ReviewEvent(StrEnum):
    COMMENT = "comment"
    REQUEST_CHANGES = "request_changes"


class CodeChangeState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"
    MERGED = "merged"


class RunState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    PUBLISHING = "publishing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    SKIPPED = "skipped"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"


class LedgerKind(StrEnum):
    TOP_UP = "top_up"
    USAGE_DEBIT = "usage_debit"
    ADJUSTMENT = "adjustment"
