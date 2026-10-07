"""Classify Run attempt failures and choose delays for retryable queue messages.

Only codes in ``RETRYABLE_ERROR_CODES`` can enter T9 before ``MAX_ATTEMPTS``;
``llm_payment_required`` and ``budget_exceeded`` are terminal codes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from app.modules.reviews.application.review_output import InvalidReviewOutput

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=5)
HEARTBEAT_INTERVAL = timedelta(seconds=60)
FAST_ATTEMPT_DEADLINE = timedelta(minutes=8)
RECONCILER_QUEUED_GRACE = timedelta(minutes=10)

# "Retry: да" in the §6 catalog: T9 while attempt < 3, then failed and a copy in reviews.dlq.
RETRYABLE_ERROR_CODES = frozenset(
    {"llm_timeout", "llm_rate_limited", "llm_unavailable", "diff_fetch_failed", "internal_error"}
)
KNOWN_ERROR_CODES = RETRYABLE_ERROR_CODES | {
    "llm_invalid_output",
    "llm_context_overflow",
    "llm_payment_required",
    "budget_exceeded",
    "deadline_exceeded",
    "github_forbidden",
    "github_publish_failed",
    "lease_expired",
}


def cancellation_reason(*, pr_open: bool, head_current: bool, cancel_requested: bool) -> str | None:
    """T6, T11, T14 and T15 decide by PostgreSQL: closed PR, then a new head, then a user cancel."""
    if not pr_open:
        return "pr_closed"
    if not head_current:
        return "superseded"
    return "cancelled_by_user" if cancel_requested else None


class RunFailure(Exception):
    """An attempt error carrying a code; ``classify_failure`` checks external codes."""

    def __init__(
        self, error_code: str, message: str = "", *, retry_after: timedelta | None = None
    ) -> None:
        super().__init__(f"{error_code}: {message}" if message else error_code)
        self.error_code = error_code
        self.message = message or error_code
        self.retry_after = retry_after


class RunCancelled(Exception):
    """``cancel_requested`` was seen on a checkpoint (T11)."""


def classify_failure(exc: BaseException) -> RunFailure:
    """Map an attempt exception to its catalog code; unknown errors are ``internal_error``.

    Preserve recognized ``error_code`` values from gateway errors; unknown or
    absent codes become ``internal_error``.
    """
    if isinstance(exc, RunFailure):
        return exc
    if isinstance(exc, InvalidReviewOutput):
        return RunFailure("llm_invalid_output", str(exc))
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code in KNOWN_ERROR_CODES:
        return RunFailure(code, str(exc))
    return RunFailure("internal_error", f"{type(exc).__name__}: {exc}")


@dataclass(frozen=True)
class RetryDelays:
    """Queue delays shared by T9 scheduling and RabbitMQ retry TTLs."""

    short: timedelta = timedelta(seconds=30)
    medium: timedelta = timedelta(minutes=2)
    long: timedelta = timedelta(minutes=10)

    def for_failure(self, attempt: int, failure: RunFailure) -> tuple[str, timedelta]:
        """Choose 10m for a long Retry-After, 2m for limits/later attempts, else 30s.

        The caller checks retryability and the attempt cap before using this method.
        """
        rate_limited = failure.error_code == "llm_rate_limited" or failure.retry_after is not None
        if rate_limited and failure.retry_after is not None and failure.retry_after > self.medium:
            return "10m", self.long
        if rate_limited or attempt >= 2:
            return "2m", self.medium
        return "30s", self.short
