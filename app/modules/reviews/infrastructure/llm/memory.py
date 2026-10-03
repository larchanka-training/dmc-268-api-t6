"""In-process ledger and trace: the gateway without a database (eval #30, tests)."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from app.modules.reviews.application.llm import LlmCallRecord, LlmUsage, RunCallContext


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

    async def record_call(self, run_id: UUID, record: LlmCallRecord) -> None:
        self.records.append((run_id, record))
