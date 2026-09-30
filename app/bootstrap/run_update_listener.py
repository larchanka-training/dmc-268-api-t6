"""Bridge ``NOTIFY run_updated`` from any process into the portal-api SSE hub (D12)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

import psycopg
from sqlalchemy.engine import make_url

from app.modules.reviews.application.run_events import RunUpdated, RunUpdatePublisher

_LOGGER = logging.getLogger(__name__)

CHANNEL = "run_updated"


def _event(payload: str) -> RunUpdated | None:
    try:
        data = json.loads(payload)
        return RunUpdated(run_id=UUID(data["run_id"]), status=str(data["status"]))
    except (ValueError, KeyError, TypeError):
        _LOGGER.warning("Ignoring malformed %s payload", CHANNEL)
        return None


async def listen_forever(database_url: str, hub: RunUpdatePublisher, retry: float = 5.0) -> None:
    """LISTEN on a dedicated autocommit connection; reconnect after a lost connection."""
    conninfo = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    while True:
        try:
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as conn:
                await conn.execute(f"LISTEN {CHANNEL}")
                async for notify in conn.notifies():
                    event = _event(notify.payload)
                    if event is not None:
                        await hub.publish(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("%s listener lost its connection; reconnecting", CHANNEL)
            await asyncio.sleep(retry)


@asynccontextmanager
async def run_update_listener(
    database_url: str | None, hub: RunUpdatePublisher
) -> AsyncIterator[None]:
    if not database_url:
        yield
        return
    task = asyncio.create_task(listen_forever(database_url, hub))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
