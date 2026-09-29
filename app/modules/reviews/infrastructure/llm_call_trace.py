"""``llm.call`` run actions (docs/PIPELINE_SPEC.md §2) behind the ``LlmCallTrace`` port."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.llm import LlmCallRecord, serialized_size
from app.modules.reviews.infrastructure.models import RunAction

LLM_CALL_TOOL = "llm.call"
INLINE_RESPONSE_LIMIT = 64 * 1024


class SqlAlchemyLlmCallTrace:
    """One short transaction per call, after the call returned or failed."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None:
        async with self._session_factory.begin() as session:
            index = await session.scalar(
                select(func.coalesce(func.max(RunAction.index), -1)).where(
                    RunAction.run_id == run_id
                )
            )
            assert index is not None
            session.add(
                RunAction(
                    run_id=run_id,
                    index=index + 1,
                    tool=LLM_CALL_TOOL,
                    request=record.request_json(),
                    response=inline_response(record.response_json()),
                    response_ref=None,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                )
            )


def inline_response(value: Any) -> Any:
    """Keep a response up to 64 KB inline; cut a larger one to a marked prefix.

    Interim until the #34 table for large bodies exists (``response_ref``, §2): the
    wrapper has the §2 shape, bounded by the inline limit instead of 1 MiB.
    """
    size = serialized_size(value)
    if size <= INLINE_RESPONSE_LIMIT:
        return value
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if serialized_size(_wrapper(size, text[:middle])) <= INLINE_RESPONSE_LIMIT:
            low = middle
        else:
            high = middle - 1
    return _wrapper(size, text[:low])


def _wrapper(original_bytes: int, text: str) -> dict[str, object]:
    return {"truncated": True, "original_bytes": original_bytes, "text": text}
