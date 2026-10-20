"""Worker signal ownership spans asyncio.run, including Runner teardown."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import threading
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager, suppress
from types import FrameType
from typing import Any

_PROCESS_TIMEOUT = 35.0
_DRAIN_TIMEOUT = 20.0
_CLEANUP_TIMEOUT = 10.0
_LOGGER = logging.getLogger(__name__)


class _SignalOwner:
    def __init__(self) -> None:
        self._stopping = False
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop: asyncio.Event | None = None
        self._timer: threading.Timer | None = None
        self._previous = {sig: signal.getsignal(sig) for sig in (signal.SIGTERM, signal.SIGINT)}

    def install(self) -> None:
        for sig in self._previous:
            signal.signal(sig, self._request_stop)

    def bind(self, loop: asyncio.AbstractEventLoop, stop: asyncio.Event) -> None:
        self._loop, self._stop = loop, stop
        if self._stopping:
            stop.set()

    def _request_stop(self, signum: int, frame: FrameType | None) -> None:
        if self._stopping or self._closed:
            return
        self._stopping = True
        self._timer = threading.Timer(_PROCESS_TIMEOUT, os._exit, args=(128 + signum,))
        self._timer.daemon = True
        self._timer.start()
        if self._loop is not None and self._stop is not None:
            # A signal can arrive after loop.close while asyncio.run is returning.
            # The independent timer stays armed until the synchronous wrapper exits.
            with suppress(RuntimeError):
                self._loop.call_soon_threadsafe(self._stop.set)

    def close(self) -> None:
        self._closed = True
        if self._timer is not None:
            self._timer.cancel()
        for sig, previous in self._previous.items():
            signal.signal(sig, previous)


def run_with_signals(worker: Callable[[asyncio.Event], Coroutine[Any, Any, None]]) -> None:
    owner = _SignalOwner()
    owner.install()

    async def entry() -> None:
        stop = asyncio.Event()
        owner.bind(asyncio.get_running_loop(), stop)
        await worker(stop)

    try:
        asyncio.run(entry())
    finally:
        # asyncio.run has also finished Runner.close (tasks, async generators, executor).
        owner.close()


@asynccontextmanager
async def stop_during_startup(stop: asyncio.Event | None) -> AsyncIterator[asyncio.Event]:
    ready = asyncio.Event()
    if stop is None:
        yield ready
        return
    parent = asyncio.current_task()
    assert parent is not None
    initiated_cancel = False

    async def watch() -> None:
        nonlocal initiated_cancel
        await stop.wait()
        if not ready.is_set() and parent.cancelling() == 0:
            initiated_cancel = True
            parent.cancel()

    watcher = asyncio.create_task(watch())
    try:
        yield ready
    except asyncio.CancelledError:
        if initiated_cancel and parent.cancelling() == 1:
            parent.uncancel()
        else:
            raise
    finally:
        if not watcher.done():
            watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


def _cancel_once(task: asyncio.Task[None]) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()


async def supervise_shutdown(
    stop: asyncio.Event,
    consumers: list[asyncio.Task[None]],
    background: list[asyncio.Task[None]],
) -> None:
    await stop.wait()
    _LOGGER.info("Worker intake stopping; draining active deliveries")
    loop = asyncio.get_running_loop()
    drain_deadline = loop.time() + _DRAIN_TIMEOUT
    cleanup_deadline = drain_deadline + _CLEANUP_TIMEOUT
    for task in background:
        _cancel_once(task)
    # wait observes TaskGroup children without cancelling a gather or their parent.
    # That avoids a second cancellation interrupting retained intake/lease cleanup.
    _, pending = await asyncio.wait(consumers, timeout=max(0, drain_deadline - loop.time()))
    for task in pending:
        _cancel_once(task)
    remaining = {task for task in (*consumers, *background) if not task.done()}
    if remaining:
        _, remaining = await asyncio.wait(remaining, timeout=max(0, cleanup_deadline - loop.time()))
    if remaining:
        _LOGGER.warning("Worker cleanup deadline exceeded; %s tasks still active", len(remaining))
        # Resources stay open beneath outstanding work. The independent process timer
        # bounds even cancellation-resistant work and asyncio.Runner teardown.
        await asyncio.wait(remaining)


async def run_background(
    stop: asyncio.Event | None, operation: Callable[[], Coroutine[Any, Any, None]]
) -> None:
    """Do not start leader scheduling/heartbeat after a queued stop callback wins."""
    if stop is None or not stop.is_set():
        await operation()


async def finish_cleanup(
    cleanup: Coroutine[Any, Any, None], *, prior_error: BaseException | None
) -> None:
    """Retain the final owned-work join through first/repeated caller cancellation."""
    task = asyncio.create_task(cleanup)
    interrupted: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if interrupted is None:
                interrupted = error
        except Exception:
            break
    try:
        task.result()
    except Exception:
        if prior_error is None and interrupted is None:
            raise
        _LOGGER.exception("Worker final owned-work join failed during shutdown")
    if interrupted is not None:
        if isinstance(prior_error, Exception):
            _LOGGER.error(
                "Worker failed before caller cancellation interrupted its final join",
                exc_info=(type(prior_error), prior_error, prior_error.__traceback__),
            )
        raise interrupted
