"""Bridge ``NOTIFY run_step`` from any process into the portal-api step hub."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol
from uuid import UUID

import psycopg
from sqlalchemy.engine import make_url

from app.modules.reviews.application.run_events import RunStep
from app.modules.reviews.infrastructure.run_action_records import CHANNEL

_LOGGER = logging.getLogger(__name__)


class RunStepPublisher(Protocol):
    async def publish(self, event: RunStep) -> None: ...


def _event(payload: str) -> RunStep | None:
    try:
        data = json.loads(payload)
        if not isinstance(data, dict):
            raise TypeError
        ok = data["ok"]
        if not isinstance(ok, bool):
            raise TypeError
        return RunStep(
            run_id=UUID(data["run_id"]),
            tool=str(data["tool"]),
            index=int(data["index"]),
            ok=ok,
            started_at=datetime.fromisoformat(str(data["started_at"])),
            duration_ms=int(data["duration_ms"]),
        )
    except (ValueError, KeyError, TypeError):
        _LOGGER.warning("Ignoring malformed %s payload", CHANNEL)
        return None


async def listen_forever(database_url: str, hub: RunStepPublisher, retry: float = 5.0) -> None:
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
async def run_step_listener(database_url: str | None, hub: RunStepPublisher) -> AsyncIterator[None]:
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
