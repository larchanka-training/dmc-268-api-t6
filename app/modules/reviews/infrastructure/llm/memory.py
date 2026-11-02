"""In-process ledger and trace: the gateway without a database (eval #30, tests)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from app.modules.reviews.application.llm import (
    LLM_RESULT_CACHE_TOOL,
    LlmCallRecord,
    LlmUsage,
    RunCallContext,
    result_cache_hit_request,
)


class InMemoryUsageLedger:
    def __init__(self) -> None:
        self.events: list[tuple[RunCallContext, LlmUsage]] = []

    async def run_cost_usd(self, run_id: UUID) -> Decimal:
        return sum(
            (usage.cost_usd for context, usage in self.events if context.run_id == run_id),
            Decimal(0),
        )

    async def record(self, context: RunCallContext, usage: LlmUsage) -> None:
        self.events.append((context, usage))


class InMemoryLlmCallTrace:
    def __init__(self) -> None:
        self.records: list[tuple[UUID, LlmCallRecord]] = []
        self.actions: list[tuple[UUID, str, dict[str, object], object]] = []

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None:
        self.records.append((run_id, record))

    async def record_result_cache_hit(
        self, run_id: UUID, operation: str, *, started_at: datetime
    ) -> None:
        self.actions.append(
            (run_id, LLM_RESULT_CACHE_TOOL, result_cache_hit_request(operation), {})
        )
