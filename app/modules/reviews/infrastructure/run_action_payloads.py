"""Where a ``run_actions`` response is stored (docs/PIPELINE_SPEC.md §2, D1).

Up to 64 KB of serialized JSON stays inline in ``run_actions.response``. A larger
response is a ``run_action_responses`` row referenced by ``response_ref``; a row holds
at most 1 MiB, a longer response is replaced by a truncation wrapper.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.reviews.infrastructure.models import RunActionResponseBody

INLINE_LIMIT_BYTES = 64 * 1024
ROW_LIMIT_BYTES = 1024 * 1024


def _serialized(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def truncated(raw: str, limit: int = ROW_LIMIT_BYTES) -> dict[str, Any]:
    """The longest prefix of ``raw`` whose wrapper serializes within ``limit`` bytes."""
    original_bytes = len(raw.encode())

    def wrapper(size: int) -> dict[str, Any]:
        return {"truncated": True, "original_bytes": original_bytes, "text": raw[:size]}

    low, high = 0, len(raw)
    while low < high:
        middle = (low + high + 1) // 2
        if len(_serialized(wrapper(middle)).encode()) <= limit:
            low = middle
        else:
            high = middle - 1
    return wrapper(low)


async def place_response(
    session: AsyncSession, run_id: UUID, response: Any
) -> tuple[Any, str | None]:
    """Return ``(response, response_ref)`` for a new ``run_actions`` row; flushes only."""
    if response is None:
        return None, None
    raw = _serialized(response)
    size = len(raw.encode())
    if size <= INLINE_LIMIT_BYTES:
        return response, None
    body = RunActionResponseBody(
        run_id=run_id, body=response if size <= ROW_LIMIT_BYTES else truncated(raw)
    )
    session.add(body)
    await session.flush()
    return None, str(body.id)
