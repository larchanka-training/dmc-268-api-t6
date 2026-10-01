"""``llm.call`` run actions (docs/PIPELINE_SPEC.md §2) behind the ``LlmCallTrace`` port."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.llm import LlmCallRecord
from app.modules.reviews.infrastructure.models import RunAction
from app.modules.reviews.infrastructure.run_action_payloads import place_response

LLM_CALL_TOOL = "llm.call"


class SqlAlchemyLlmCallTrace:
    """One short transaction per call, after the call returned or failed.

    A response over 64 KB goes to ``run_action_responses`` through ``place_response``
    (#34), like every other run action.
    """

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
            response, response_ref = await place_response(session, run_id, record.response_json())
            session.add(
                RunAction(
                    run_id=run_id,
                    index=index + 1,
                    tool=LLM_CALL_TOOL,
                    request=record.request_json(),
                    response=response,
                    response_ref=response_ref,
                    started_at=record.started_at,
                    duration_ms=record.duration_ms,
                )
            )
