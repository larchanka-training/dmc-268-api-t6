"""Bridge ``NOTIFY run_updated`` from any process into the portal-api SSE hub (D12)."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import UUID

from app.bootstrap.postgres_listen import listen_notify, notify_listener
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


async def _publish(payload: str, hub: RunUpdatePublisher) -> None:
    event = _event(payload)
    if event is not None:
        await hub.publish(event)


async def listen_forever(database_url: str, hub: RunUpdatePublisher, retry: float = 5.0) -> None:
    """LISTEN on a dedicated autocommit connection; reconnect after a lost connection."""
    await listen_notify(
        database_url,
        CHANNEL,
        lambda payload: _publish(payload, hub),
        retry,
    )


@asynccontextmanager
async def run_update_listener(
    database_url: str | None, hub: RunUpdatePublisher
) -> AsyncIterator[None]:
    async with notify_listener(
        database_url,
        CHANNEL,
        lambda payload: _publish(payload, hub),
    ):
        yield
