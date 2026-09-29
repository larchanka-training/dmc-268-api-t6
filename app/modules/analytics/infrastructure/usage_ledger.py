"""Insert-only ``usage_events`` (Р-8) behind the gateway's ``UsageLedger`` port."""

from __future__ import annotations

from decimal import Decimal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.analytics.infrastructure.models import UsageEvent
from app.modules.reviews.application.llm import LlmUsage, RunCallContext


class SqlAlchemyUsageLedger:
    """Each write is its own short transaction, made after the provider call returned."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def run_cost_usd(self, run_id: UUID) -> Decimal:
        async with self._session_factory() as session:
            total = await session.scalar(
                select(func.coalesce(func.sum(UsageEvent.cost_usd), 0)).where(
                    UsageEvent.run_id == run_id
                )
            )
        return Decimal(total or 0)

    async def record(self, context: RunCallContext, usage: LlmUsage) -> None:
        async with self._session_factory.begin() as session:
            session.add(
                UsageEvent(
                    run_id=context.run_id,
                    workspace_id=context.workspace_id,
                    provider=usage.provider[:50],
                    model=usage.model[:100],
                    operation=usage.operation[:50],
                    tokens_in=usage.tokens_in,
                    tokens_out=usage.tokens_out,
                    cache_read_tokens=usage.cache_read_tokens,
                    cost_usd=usage.cost_usd,
                )
            )
