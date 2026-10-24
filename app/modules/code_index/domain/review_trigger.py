"""What a review writes to the index state row (#144, ADR 0001 §4, §6.1).

A pure decision over the state row and a time read from the database clock. The adapter
runs it on an unlocked read first, to learn whether the review writes at all, and again on
the locked row with the time read after the lock; only that second decision is applied.
It only picks the writes and never reaches the trace: readiness comes from `RagIndex` alone.
"""

from __future__ import annotations

from collections.abc import Set
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum

from app.modules.code_index.domain.enums import PurgeReason, RagIndexStatus

# ADR §6.1: a review queues a full from `empty` at most once a day and once per target.
_REVIEW_FULL_EVERY = timedelta(hours=24)
# ADR §4: `last_used_at` is written at most once an hour per repository.
_LAST_USED_EVERY = timedelta(hours=1)


class ReviewTriggerAction(StrEnum):
    """`enable` is `enable()`; both requests are `request_build(target_commit_sha=None)`."""

    NONE = "none"
    ENABLE = "enable"
    REQUEST_FULL = "request_full"
    REQUEST_UPDATE = "request_update"


@dataclass(frozen=True)
class ReviewTrigger:
    """`extend_last_used` sets `last_used_at` to now; `record_throttle` sets
    `review_full_requested_at` to now and `review_full_requested_sha` to the row's
    `target_commit_sha`."""

    action: ReviewTriggerAction
    extend_last_used: bool
    record_throttle: bool


_NO_WRITES = ReviewTrigger(ReviewTriggerAction.NONE, extend_last_used=False, record_throttle=False)


def review_trigger(
    *,
    status: RagIndexStatus | None,
    usable: bool,
    purge_reason: PurgeReason | None,
    wake_requested_at: datetime | None,
    lease_active: bool,
    last_error_code: str | None,
    target_commit_sha: str | None,
    last_used_at: datetime | None,
    review_full_requested_at: datetime | None,
    review_full_requested_sha: str | None,
    healable_error_codes: Set[str],
    now: datetime,
) -> ReviewTrigger:
    """Pick the writes for the row read at ``now``; ``status`` None means no row.

    ``status`` and ``usable`` are what `rag_index(...)` derives over the same row, so
    `stale` and whether a review may read the index are decided there. ``lease_active``
    means a build holds the row's lease at ``now``. ``healable_error_codes`` are the
    `last_error_code` values a later attempt can heal; any other stored code is a
    terminal refusal of the row's target.
    """
    if status is None or status is RagIndexStatus.DISABLED:
        return _NO_WRITES
    # Both limits together; a NULL target (after a purge) leaves only the 24 h one.
    full_allowed = (
        review_full_requested_at is None or now - review_full_requested_at >= _REVIEW_FULL_EVERY
    ) and (target_commit_sha is None or target_commit_sha != review_full_requested_sha)
    if purge_reason is not None:
        # Only a TTL purge comes back; the converge pass owns `removed` and `disabled`,
        # and `enable()` sets `last_used_at` itself.
        if purge_reason == PurgeReason.TTL and full_allowed:
            return ReviewTrigger(
                ReviewTriggerAction.ENABLE, extend_last_used=False, record_throttle=True
            )
        return _NO_WRITES
    # A review wakes no row whose marker is already set, no row a build holds the lease
    # of, and no terminal refusal of the row's own target.
    wakes = (
        wake_requested_at is None
        and not lease_active
        and (last_error_code is None or last_error_code in healable_error_codes)
    )
    extend = last_used_at is None or now - last_used_at >= _LAST_USED_EVERY
    if status is RagIndexStatus.EMPTY:
        if wakes and full_allowed:
            # The full and its throttle record are written each time; the extension
            # keeps the hourly rule of a read.
            return ReviewTrigger(
                ReviewTriggerAction.REQUEST_FULL, extend_last_used=extend, record_throttle=True
            )
        return _NO_WRITES
    if not usable:
        return _NO_WRITES
    # A usable index is extended even when no update is requested.
    action = (
        ReviewTriggerAction.REQUEST_UPDATE
        if status is RagIndexStatus.STALE and wakes
        else ReviewTriggerAction.NONE
    )
    return ReviewTrigger(action, extend_last_used=extend, record_throttle=False)
