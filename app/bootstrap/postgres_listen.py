"""Shared LISTEN loop for portal notifications."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import psycopg
from sqlalchemy.engine import make_url

_LOGGER = logging.getLogger(__name__)


async def listen_notify(
    database_url: str,
    channel: str,
    on_payload: Callable[[str], Awaitable[None]],
    retry: float = 5.0,
) -> None:
    """LISTEN on a dedicated autocommit connection; reconnect after a lost connection."""
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                await conn.execute(f"LISTEN {channel}")
                async for notify in conn.notifies():
                    await on_payload(notify.payload)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s listener lost its connection; reconnecting", channel)
            await asyncio.sleep(retry)


@asynccontextmanager
async def notify_listener(
    database_url: str | None,
    channel: str,
    on_payload: Callable[[str], Awaitable[None]],
) -> AsyncIterator[None]:
    if not database_url:
        yield
        return
    task = asyncio.create_task(listen_notify(database_url, channel, on_payload))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
