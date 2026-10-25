"""Insert one ``run_actions`` row and ``NOTIFY run_step`` in the caller's transaction."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reviews.application.run_events import utc_rfc3339
from app.modules.reviews.infrastructure.llm.models import (
    CONVENTIONS_OPERATION,
    REVIEW_OPERATION,
)
from app.modules.reviews.infrastructure.llm_call_trace import LLM_CALL_TOOL
from app.modules.reviews.infrastructure.models import Run, RunAction
from app.modules.reviews.infrastructure.run_action_payloads import place_response

_SSE_LLM_OPERATIONS = frozenset({CONVENTIONS_OPERATION, REVIEW_OPERATION})

CHANNEL = "run_step"


def step_succeeded(response: Any) -> bool:
    """False only when the response object contains ``error``; null and other values are true."""
    return not (isinstance(response, dict) and "error" in response)


async def insert_run_action(
    session: AsyncSession,
    *,
    run_id: UUID,
    tool: str,
    request: dict[str, Any],
    response: Any,
    started_at: datetime,
    duration_ms: int,
) -> None:
    """Assign the next index, insert the row, and notify on ``session``. Flush only.

    ``NOTIFY`` uses this session, so a rollback of the caller's transaction emits nothing.
    ``ok`` is taken from ``response`` before the body is stored.
    """
    run_attempt = await session.scalar(
        select(Run.attempt).where(Run.id == run_id).with_for_update()
    )
    if run_attempt is None:
        msg = f"run {run_id} not found"
        raise ValueError(msg)
    if run_attempt < 1:
        msg = f"run {run_id} attempt must be >= 1 before recording a step (got {run_attempt})"
        raise ValueError(msg)
    current = await session.scalar(
        select(func.coalesce(func.max(RunAction.index), -1)).where(RunAction.run_id == run_id)
    )
    assert current is not None
    index = current + 1
    ok = step_succeeded(response)
    stored, response_ref = await place_response(session, run_id, response)
    session.add(
        RunAction(
            run_id=run_id,
            index=index,
            tool=tool,
            request=request,
            response=stored,
            response_ref=response_ref,
            started_at=started_at,
            duration_ms=duration_ms,
        )
    )
    await session.flush()
    notify: dict[str, object] = {
        "run_id": str(run_id),
        "tool": tool,
        "index": index,
        "ok": ok,
        "started_at": utc_rfc3339(started_at),
        "duration_ms": duration_ms,
        "attempt": run_attempt,
    }
    if tool == LLM_CALL_TOOL:
        operation = request.get("operation")
        if operation in _SSE_LLM_OPERATIONS:
            notify["llm_operation"] = operation
    payload = json.dumps(notify, separators=(",", ":"))
    await session.execute(select(func.pg_notify(CHANNEL, payload)))
