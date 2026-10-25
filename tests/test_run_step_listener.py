"""Unit coverage for the run.step NOTIFY frame parser (#170)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import UUID

from app.bootstrap.run_step_listener import _event
from app.modules.reviews.application.run_events import RunStep

RUN_ID = UUID("11111111-2222-3333-4444-555555555555")
STARTED = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


def _payload(**overrides: object) -> str:
    body: dict[str, object] = {
        "run_id": str(RUN_ID),
        "tool": "vcs.fetch_diff",
        "index": 0,
        "ok": True,
        "started_at": "2026-10-12T09:00:00Z",
        "duration_ms": 5,
        "attempt": 2,
    }
    body.update(overrides)
    return json.dumps(body)


def test_event_keeps_conventions_and_attempt_above_one() -> None:
    event = _event(
        _payload(tool="llm.call", attempt=2, llm_operation="conventions", index=3, duration_ms=8)
    )

    assert event == RunStep(RUN_ID, "llm.call", 3, True, STARTED, 8, 2, "conventions")


def test_event_keeps_review_and_drops_verification_and_a_non_positive_attempt() -> None:
    review = _event(_payload(tool="llm.call", llm_operation="review"))
    verification = _event(_payload(tool="llm.call", llm_operation="verification"))
    missing_attempt = _event(_payload(attempt=0))

    assert review is not None
    assert review.llm_operation == "review"
    assert review.attempt == 2
    assert verification is None
    assert missing_attempt is None
    assert _event("not-json") is None
