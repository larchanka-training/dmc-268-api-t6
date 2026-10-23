"""Cancel an owned task without interrupting cancellation already in progress."""

from __future__ import annotations

import asyncio


def cancel_once[T](task: asyncio.Task[T]) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()
