"""Transaction-bound hints to inspect the durable webhook queue."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL = "webhook_work_available"


async def notify_webhook_work(session: AsyncSession) -> None:
    """Deliver an empty hint on commit; rollback discards it with the queue change."""
    await session.execute(text("SELECT pg_notify(:channel, '')"), {"channel": CHANNEL})
