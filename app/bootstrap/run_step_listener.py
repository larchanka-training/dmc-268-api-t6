"""Bridge ``NOTIFY run_step`` from any process into the portal-api step hub."""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.bootstrap.postgres_listen import listen_notify, notify_listener
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
        attempt = int(data["attempt"])
        if attempt < 1:
            raise ValueError
        tool = str(data["tool"])
        llm_operation: str | None = None
        if tool == "llm.call":
            raw_operation = data.get("llm_operation")
            if raw_operation is not None:
                llm_operation = str(raw_operation)
                if llm_operation not in ("conventions", "review"):
                    raise ValueError
        return RunStep(
            run_id=UUID(data["run_id"]),
            tool=tool,
            index=int(data["index"]),
            ok=ok,
            started_at=datetime.fromisoformat(str(data["started_at"])),
            duration_ms=int(data["duration_ms"]),
            attempt=attempt,
            llm_operation=llm_operation,
        )
    except (ValueError, KeyError, TypeError):
        _LOGGER.warning("Ignoring malformed %s payload", CHANNEL)
        return None


async def _publish(payload: str, hub: RunStepPublisher) -> None:
    event = _event(payload)
    if event is not None:
        await hub.publish(event)


async def listen_forever(database_url: str, hub: RunStepPublisher, retry: float = 5.0) -> None:
    """LISTEN on a dedicated autocommit connection; reconnect after a lost connection."""
    await listen_notify(
        database_url,
        CHANNEL,
        lambda payload: _publish(payload, hub),
        retry,
    )


@asynccontextmanager
async def run_step_listener(database_url: str | None, hub: RunStepPublisher) -> AsyncIterator[None]:
    async with notify_listener(
        database_url,
        CHANNEL,
        lambda payload: _publish(payload, hub),
    ):
        yield
