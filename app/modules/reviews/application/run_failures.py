"""Run failure classes, retry policy and attempt limits (docs/PIPELINE_SPEC.md §3, §4, §6)."""

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
    """A failed attempt with an ``error_code`` from the §6 catalog."""

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

    The LLM gateway (#33) raises errors carrying a catalog ``error_code`` attribute; the
    worker accepts any exception with such an attribute so the gateway needs no import
    of this module.
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
    """Delays of the retry queues; tests shorten them, queue TTLs use the same values."""

    short: timedelta = timedelta(seconds=30)
    medium: timedelta = timedelta(minutes=2)
    long: timedelta = timedelta(minutes=10)

    def for_failure(self, attempt: int, failure: RunFailure) -> tuple[str, timedelta]:
        """Return the retry queue key (``30s``, ``2m``, ``10m``) and delay for T9 (§4.2)."""
        rate_limited = failure.error_code == "llm_rate_limited" or failure.retry_after is not None
        if rate_limited and failure.retry_after is not None and failure.retry_after > self.medium:
            return "10m", self.long
        if rate_limited or attempt >= 2:
            return "2m", self.medium
        return "30s", self.short
