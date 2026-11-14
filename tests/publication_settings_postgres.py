"""Shared setup for publication settings PostgreSQL tests."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.reviews.application.try_enqueue_webhook_run import (
    PendingRunMessage,
    RunInsertCandidate,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunStore
from tests.portal_postgres import NOW, PR_NEW, RUN_ATTEMPTED


async def _ready(factory: async_sessionmaker[AsyncSession]) -> None:
    async with factory.begin() as session:
        await session.execute(
            text("UPDATE runs SET state='succeeded' WHERE id=:run"), {"run": RUN_ATTEMPTED}
        )
        await session.execute(
            text("UPDATE code_changes SET head_sha=:head WHERE id=:pr"),
            {"head": "f" * 40, "pr": PR_NEW},
        )


async def _insert(session: AsyncSession) -> UUID:
    store = SqlAlchemyWebhookRunStore(session)
    candidate = await store.lock_candidate(PR_NEW)
    assert isinstance(candidate, RunInsertCandidate)
    message = await store.insert_webhook_run(candidate, NOW)
    assert isinstance(message, PendingRunMessage)
    return message.run_id
