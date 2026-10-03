"""``llm.call`` run actions (docs/PIPELINE_SPEC.md §2) behind the ``LlmCallTrace`` port."""

from __future__ import annotations

from uuid import UUID

from app.modules.reviews.application.llm import LlmCallRecord
from app.modules.reviews.application.run_trace import RunTrace

LLM_CALL_TOOL = "llm.call"


class RunTraceLlmCalls:
    """Write each provider call through the shared ``RunTrace`` of the worker.

    ``SqlAlchemyRunTraceStore.add_action`` locks the Run row for the next index and
    stores a response over 64 KB in ``run_action_responses``; every write is its own
    short transaction after the call returned or failed.
    """

    def __init__(self, trace: RunTrace) -> None:
        self._trace = trace

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None:
        await self._trace.record(
            run_id,
            LLM_CALL_TOOL,
            record.request_json(),
            record.response_json(),
            record.started_at,
            record.duration_ms,
        )
