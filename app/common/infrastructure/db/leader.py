"""One leader per lock key across processes, via a PostgreSQL session advisory lock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

_LOGGER = logging.getLogger(__name__)

WORKER_LEADER_LOCK = 3_468_001
RECONCILER_LEADER_LOCK = 3_468_002


async def run_as_leader(
    engine: AsyncEngine,
    key: int,
    period: float,
    tick: Callable[[], Awaitable[object]],
    *,
    name: str,
) -> None:
    """Run ``tick`` every ``period`` seconds only while this process holds the lock.

    The lock lives on a dedicated AUTOCOMMIT connection, so no transaction stays
    open between ticks. A lost connection drops the lock; the loop reconnects.
    """
    while True:
        try:
            async with engine.connect() as connection:
                await connection.execution_options(isolation_level="AUTOCOMMIT")
                try:
                    while not await connection.scalar(
                        text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                    ):
                        await asyncio.sleep(period)
                    _LOGGER.info("%s leader lock acquired", name)
                    while True:
                        try:
                            await tick()
                        except Exception:
                            _LOGGER.exception("%s leader tick failed", name)
                        await asyncio.sleep(period)
                        # Fails when the session, and with it the lock, is gone.
                        await connection.scalar(text("SELECT 1"))
                finally:
                    # Never return a lock-holding session to the pool.
                    await connection.invalidate()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s leader loop lost its database connection", name)
            await asyncio.sleep(period)
