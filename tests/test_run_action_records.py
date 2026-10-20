"""Unit tests for ``insert_run_action`` invariants."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest

from app.modules.reviews.infrastructure.run_action_records import insert_run_action

RUN_ID = UUID("00000000-0000-0000-0000-000000000170")
STARTED = datetime(2026, 10, 12, 9, 0, 0, tzinfo=UTC)


class _Session:
    def __init__(self, run_attempt: int | None) -> None:
        self._run_attempt = run_attempt
        self._scalar_calls = 0

    async def scalar(self, _statement: object) -> Any:
        self._scalar_calls += 1
        if self._scalar_calls == 1:
            return self._run_attempt
        raise AssertionError("insert_run_action must fail before a second scalar")


@pytest.mark.anyio
async def test_insert_run_action_rejects_run_with_attempt_below_one() -> None:
    session = _Session(0)
    with pytest.raises(ValueError, match="attempt must be >= 1"):
        await insert_run_action(
            session,  # type: ignore[arg-type]
            run_id=RUN_ID,
            tool="vcs.fetch_diff",
            request={},
            response={"files": 1},
            started_at=STARTED,
            duration_ms=1,
        )
