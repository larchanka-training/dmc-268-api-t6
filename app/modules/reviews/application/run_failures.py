"""Classify Run attempt failures and choose delays for retryable queue messages.

Only codes in ``RETRYABLE_ERROR_CODES`` can enter T9 before ``MAX_ATTEMPTS``;
``llm_payment_required`` and ``budget_exceeded`` are terminal codes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Literal

from app.modules.reviews.application.review_output import InvalidReviewOutput

MAX_ATTEMPTS = 3
LEASE = timedelta(minutes=5)
HEARTBEAT_INTERVAL = timedelta(seconds=60)
FAST_ATTEMPT_DEADLINE = timedelta(minutes=8)
RECONCILER_QUEUED_GRACE = timedelta(minutes=10)
# The longest Retry-After a Run waits for before its next attempt; a longer one is capped.
RETRY_AFTER_CEILING = timedelta(days=1)
_CEILING_SECONDS = RETRY_AFTER_CEILING // timedelta(seconds=1)


def parse_retry_after(
    value: str | None, *, date: str | None = None, now: Callable[[], datetime] | None = None
) -> timedelta | None:
    """The delay of a GitHub ``Retry-After``: ASCII delta-seconds or an HTTP-date.

    An HTTP-date counts from the response ``Date`` when it parses, else from ``now()``
    (UTC by default); a date already past is no wait, ``timedelta(0)``, yet still a limit.
    Every HTTP-date is GMT (RFC 9110 §5.6.7), so one parsed without a zone (asctime or
    ``-0000``) is read as UTC. Either form is capped at ``RETRY_AFTER_CEILING``; anything
    else is None.
    """
    if not value:
        return None
    if value.isascii() and value.isdecimal():
        digits = value.lstrip("0") or "0"
        # More digits than the ceiling has is beyond it: never convert them, since Python
        # refuses integers longer than its digit limit and timedelta overflows long before.
        if len(digits) > len(str(_CEILING_SECONDS)):
            return RETRY_AFTER_CEILING
        return timedelta(seconds=min(int(digits), _CEILING_SECONDS))
    retry_at = _aware_http_date(value)
    if retry_at is None:
        return None
    reference = _aware_http_date(date) if date else None
    if reference is None:
        # CONTRACT: an injected now() returns an aware datetime, like the default.
        reference = (now or _utc_now)()
    # Aware datetimes subtract without a conversion to UTC, which could overflow year 9999.
    return min(max(retry_at - reference, timedelta(0)), RETRY_AFTER_CEILING)


def _aware_http_date(value: str) -> datetime | None:
    try:
        parsed = parsedate_to_datetime(value)
    except ValueError:
        # Python 3.10+ raises ValueError for every unreadable date.
        return None
    # Asctime and a "-0000" zone give a naive datetime; an HTTP-date is GMT by definition.
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _utc_now() -> datetime:
    return datetime.now(UTC)


# "Retry: да" in the §6 catalog: T9 while attempt < 3, then failed and a copy in reviews.dlq.
# Policy codes of RULES_FORMAT_SPEC §9 (``PolicyErrorCode``); only reads of the policy
# documents that can succeed later are retried.
RETRYABLE_POLICY_ERROR_CODES = frozenset({"policy_fetch_transient", "policy_rate_limited"})
TERMINAL_POLICY_ERROR_CODES = frozenset(
    {
        "policy_document_too_large",
        "policy_document_decode_error",
        "policy_document_control_character",
        "policy_document_type_unsupported",
        "policy_ignore_block_duplicate",
        "policy_ignore_block_nested",
        "policy_ignore_block_unclosed",
        "policy_ignore_info_invalid",
        "policy_ignore_pattern_invalid",
        "policy_ignore_limit_exceeded",
        "policy_path_invalid",
        "policy_match_budget_exceeded",
        "policy_prompt_budget_exceeded",
        "policy_access_denied",
        "policy_revision_unavailable",
        "policy_snapshot_integrity_error",
        "policy_context_violation",
    }
)
RETRYABLE_ERROR_CODES = (
    frozenset(
        {
            "llm_timeout",
            "llm_rate_limited",
            "llm_unavailable",
            "diff_fetch_failed",
            "bitbucket_fetch_failed",
            "bitbucket_commit_messages_fetch_failed",
            "commit_messages_fetch_failed",
            "verification_unavailable",
            "internal_error",
        }
    )
    | RETRYABLE_POLICY_ERROR_CODES
)
KNOWN_ERROR_CODES = (
    RETRYABLE_ERROR_CODES
    | TERMINAL_POLICY_ERROR_CODES
    | {
        "llm_invalid_output",
        "llm_context_overflow",
        "llm_payment_required",
        "verification_invalid_output",
        "verification_context_overflow",
        "verification_budget_exceeded",
        "verification_deadline_exceeded",
        "budget_exceeded",
        "deadline_exceeded",
        "github_forbidden",
        "bitbucket_source_unavailable",
        "bitbucket_review_limit_exceeded",
        "bitbucket_request_budget_exceeded",
        "bitbucket_invalid_response",
        "bitbucket_response_budget_exceeded",
        "bitbucket_engine_unsupported",
        "github_publish_failed",
        "pr_base_changed",
        "lease_expired",
    }
)


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
        self,
        error_code: str,
        message: str = "",
        *,
        retry_after: timedelta | None = None,
        rate_limited: bool = False,
    ) -> None:
        super().__init__(f"{error_code}: {message}" if message else error_code)
        self.error_code = error_code
        self.message = message or error_code
        self.retry_after = retry_after
        self.rate_limited = rate_limited


class RequiredSourceUnavailable(RunFailure):
    """Required per-run source authority is unavailable, rather than an optional file.

    Producers reserve this for operational authority/credential failures. Ordinary
    provider file/tree faults remain optional; refusal, cancellation and deadlines
    keep their own control outcomes. No exception detail or credentials enter the text.
    """

    def __init__(
        self,
        error_code: Literal["diff_fetch_failed", "commit_messages_fetch_failed"],
        *,
        retry_after: timedelta | None = None,
    ) -> None:
        if error_code not in {"diff_fetch_failed", "commit_messages_fetch_failed"}:
            raise ValueError("unsupported required source failure code")
        super().__init__(
            error_code,
            "Required review source authority is unavailable",
            retry_after=retry_after,
        )


class RunCancelled(Exception):
    """``cancel_requested`` was seen on a checkpoint (T11)."""


class RunSkipped(Exception):
    """The running attempt has nothing to review: ``running → skipped`` without a verdict."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def classify_failure(exc: BaseException) -> RunFailure:
    """Map an attempt exception to its catalog code; unknown errors are ``internal_error``.

    Preserve recognized ``error_code`` values from gateway errors; unknown or
    absent codes become ``internal_error``. An ``llm_rate_limited`` failure keeps the
    provider's Retry-After (``retry_after_s``) as its retry delay, up to the ceiling.
    """
    if isinstance(exc, RunFailure):
        return exc
    if isinstance(exc, InvalidReviewOutput):
        return RunFailure("llm_invalid_output", str(exc))
    code = getattr(exc, "error_code", None)
    if isinstance(code, str) and code in KNOWN_ERROR_CODES:
        if code == "llm_rate_limited":
            # Only this code reads the hint: RetryDelays treats any retry_after as a rate
            # limit. A missing, zero, negative or NaN hint is none; the ceiling is applied
            # to the float first, so a huge or infinite one cannot overflow timedelta.
            retry_after_s = getattr(exc, "retry_after_s", None)
            if isinstance(retry_after_s, (int, float)) and retry_after_s > 0:
                seconds = min(retry_after_s, RETRY_AFTER_CEILING.total_seconds())
                return RunFailure(code, str(exc), retry_after=timedelta(seconds=seconds))
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

        A Retry-After over 10m keeps the 10m queue and becomes the delay itself, up to
        ``RETRY_AFTER_CEILING``: the caller sets ``available_at`` from it, RunGuard drops
        the early copy without an attempt and T18 republishes the Run after the wait.
        The caller checks retryability and the attempt cap before using this method.
        """
        rate_limited = failure.error_code == "llm_rate_limited" or failure.retry_after is not None
        if rate_limited and failure.retry_after is not None and failure.retry_after > self.medium:
            return "10m", min(max(failure.retry_after, self.long), RETRY_AFTER_CEILING)
        if rate_limited or attempt >= 2:
            return "2m", self.medium
        return "30s", self.short
