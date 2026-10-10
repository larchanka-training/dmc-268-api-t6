"""Wake the webhook worker to inspect its durable queue after LISTEN or NOTIFY."""

from __future__ import annotations

import asyncio
import logging

import psycopg
from sqlalchemy.engine import make_url

from app.modules.integrations.webhooks.infrastructure.webhook_notifications import CHANNEL

_LOGGER = logging.getLogger(__name__)


async def listen_forever(database_url: str, wake: asyncio.Event, retry: float = 5.0) -> None:
    """Reconnect a dedicated autocommit listener; an event coalesces the work hints."""
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(
                conninfo, autocommit=True
            ) as connection:
                await connection.execute(f"LISTEN {CHANNEL}")
                # Install LISTEN before inspecting durable state, including after reconnect.
                wake.set()
                async for _ in connection.notifies():
                    wake.set()
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s listener lost its connection; reconnecting", CHANNEL)
            await asyncio.sleep(retry)
