"""The writes a review makes to the index state row it locked (#144, ADR 0001 §4, §6.1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.modules.code_index.application.vcs import HEALABLE_ERROR_CODES
from app.modules.code_index.domain.enums import PurgeReason, RagIndexStatus
from app.modules.code_index.domain.review_trigger import (
    ReviewTrigger,
    ReviewTriggerAction,
    review_trigger,
)

SHA_A = "a" * 40
SHA_B = "b" * 40
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
_NO_WRITES = ReviewTrigger(ReviewTriggerAction.NONE, extend_last_used=False, record_throttle=False)
_EXTEND_ONLY = ReviewTrigger(ReviewTriggerAction.NONE, extend_last_used=True, record_throttle=False)
_ENABLE = ReviewTrigger(ReviewTriggerAction.ENABLE, extend_last_used=False, record_throttle=True)
_FULL = ReviewTrigger(ReviewTriggerAction.REQUEST_FULL, extend_last_used=True, record_throttle=True)
_UPDATE = ReviewTrigger(
    ReviewTriggerAction.REQUEST_UPDATE, extend_last_used=False, record_throttle=False
)
_UPDATE_EXTENDED = ReviewTrigger(
    ReviewTriggerAction.REQUEST_UPDATE, extend_last_used=True, record_throttle=False
)


def _row(status: RagIndexStatus | None, **overrides: Any) -> dict[str, Any]:
    """A locked row with no marker, no code, no throttle and `last_used_at` 5 min old."""
    row: dict[str, Any] = {
        "status": status,
        "purge_reason": None,
        "wake_requested_at": None,
        "last_error_code": None,
        "target_commit_sha": SHA_A,
        "last_used_at": NOW - timedelta(minutes=5),
        "review_full_requested_at": None,
        "review_full_requested_sha": None,
    }
    return row | overrides


def _ttl(**overrides: Any) -> dict[str, Any]:
    """A TTL-purged row: the purge cleared the target, the code and the marker."""
    return _row(
        RagIndexStatus.EMPTY,
        purge_reason=PurgeReason.TTL,
        target_commit_sha=None,
        last_used_at=NOW - timedelta(days=31),
        **overrides,
    )


def _decide(row: dict[str, Any]) -> ReviewTrigger:
    return review_trigger(**row, healable_error_codes=HEALABLE_ERROR_CODES, now=NOW)


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            _row(None, target_commit_sha=None, last_used_at=None), _NO_WRITES, id="no-row"
        ),
        pytest.param(_row(RagIndexStatus.DISABLED, last_used_at=None), _NO_WRITES, id="toggle-off"),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                purge_reason=PurgeReason.REMOVED,
                target_commit_sha=None,
                last_used_at=None,
            ),
            _NO_WRITES,
            id="purged-removed",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                purge_reason=PurgeReason.DISABLED,
                target_commit_sha=None,
                last_used_at=None,
            ),
            _NO_WRITES,
            id="purged-disabled",
        ),
        pytest.param(_row(RagIndexStatus.FAILED, last_used_at=None), _NO_WRITES, id="failed"),
        pytest.param(_row(RagIndexStatus.BUILDING, last_used_at=None), _NO_WRITES, id="building"),
        pytest.param(_ttl(), _ENABLE, id="purged-ttl-enables"),
        pytest.param(_row(RagIndexStatus.EMPTY), _FULL, id="live-empty-requests-a-full"),
        pytest.param(_row(RagIndexStatus.READY, last_used_at=None), _EXTEND_ONLY, id="ready"),
        pytest.param(_row(RagIndexStatus.STALE), _UPDATE, id="stale-requests-an-update"),
    ],
)
def test_a_review_makes_the_writes_of_the_decision_table(
    row: dict[str, Any], expected: ReviewTrigger
) -> None:
    assert _decide(row) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=SHA_B,
                review_full_requested_at=NOW - timedelta(hours=23, minutes=59),
                review_full_requested_sha=SHA_A,
            ),
            _NO_WRITES,
            id="new-target-within-24h-refused",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=SHA_B,
                review_full_requested_at=NOW - timedelta(hours=24),
                review_full_requested_sha=SHA_A,
            ),
            _FULL,
            id="new-target-after-24h-allowed",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=SHA_A,
                review_full_requested_at=NOW - timedelta(hours=24),
                review_full_requested_sha=SHA_A,
            ),
            _NO_WRITES,
            id="same-target-after-24h-refused",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=SHA_A,
                review_full_requested_at=NOW - timedelta(days=30),
                review_full_requested_sha=SHA_A,
            ),
            _NO_WRITES,
            id="same-target-refused-for-good",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=None,
                review_full_requested_at=NOW - timedelta(hours=23, minutes=59),
            ),
            _NO_WRITES,
            id="null-target-within-24h-refused",
        ),
        pytest.param(
            _row(
                RagIndexStatus.EMPTY,
                target_commit_sha=None,
                review_full_requested_at=NOW - timedelta(hours=24),
                review_full_requested_sha=SHA_A,
            ),
            _FULL,
            id="null-target-after-24h-allowed",
        ),
        pytest.param(
            _ttl(review_full_requested_at=NOW - timedelta(hours=23, minutes=59)),
            _NO_WRITES,
            id="ttl-within-24h-refused",
        ),
        pytest.param(
            _ttl(review_full_requested_at=NOW - timedelta(hours=24)),
            _ENABLE,
            id="ttl-after-24h-enables",
        ),
    ],
)
def test_a_review_full_waits_24h_and_never_repeats_a_target(
    row: dict[str, Any], expected: ReviewTrigger
) -> None:
    assert _decide(row) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(_row(RagIndexStatus.READY, last_used_at=None), _EXTEND_ONLY, id="ready-null"),
        pytest.param(
            _row(RagIndexStatus.READY, last_used_at=NOW - timedelta(minutes=59)),
            _NO_WRITES,
            id="ready-59min",
        ),
        pytest.param(
            _row(RagIndexStatus.READY, last_used_at=NOW - timedelta(hours=1)),
            _EXTEND_ONLY,
            id="ready-1h",
        ),
        pytest.param(
            _row(RagIndexStatus.STALE, last_used_at=None), _UPDATE_EXTENDED, id="stale-null"
        ),
        pytest.param(
            _row(RagIndexStatus.STALE, last_used_at=NOW - timedelta(minutes=59)),
            _UPDATE,
            id="stale-59min",
        ),
        pytest.param(
            _row(RagIndexStatus.STALE, last_used_at=NOW - timedelta(hours=1)),
            _UPDATE_EXTENDED,
            id="stale-1h",
        ),
    ],
)
def test_a_usable_index_extends_last_used_at_at_most_once_an_hour(
    row: dict[str, Any], expected: ReviewTrigger
) -> None:
    assert _decide(row) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param(
            _row(RagIndexStatus.STALE, wake_requested_at=NOW - timedelta(minutes=1)),
            _NO_WRITES,
            id="stale-marker-set",
        ),
        pytest.param(
            _row(
                RagIndexStatus.STALE,
                wake_requested_at=NOW - timedelta(minutes=1),
                last_used_at=NOW - timedelta(hours=2),
            ),
            _EXTEND_ONLY,
            id="stale-marker-set-still-extends",
        ),
        pytest.param(
            _row(RagIndexStatus.STALE, last_error_code="vcs_unavailable"),
            _UPDATE,
            id="stale-healable-code",
        ),
        pytest.param(
            _row(RagIndexStatus.STALE, last_error_code="limit_exceeded:download_bytes"),
            _NO_WRITES,
            id="stale-terminal-code",
        ),
        pytest.param(
            _row(RagIndexStatus.EMPTY, wake_requested_at=NOW - timedelta(minutes=1)),
            _NO_WRITES,
            id="empty-marker-set",
        ),
        pytest.param(
            _row(RagIndexStatus.EMPTY, last_error_code="vcs_unavailable"),
            _FULL,
            id="empty-healable-code",
        ),
        pytest.param(
            _row(RagIndexStatus.EMPTY, last_error_code="limit_exceeded:download_bytes"),
            _NO_WRITES,
            id="empty-terminal-code",
        ),
    ],
)
def test_a_review_wakes_no_set_marker_and_no_terminal_refusal(
    row: dict[str, Any], expected: ReviewTrigger
) -> None:
    assert _decide(row) == expected
