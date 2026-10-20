"""Serial AMQP intake with optional cooperative stop (#185)."""

from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Callable

from aio_pika.abc import AbstractIncomingMessage, AbstractQueue

_LOGGER = logging.getLogger(__name__)


def _cancel_once(task: asyncio.Task[object]) -> None:
    if not task.done() and not task.cancelling():
        task.cancel()


async def _await_cleanup(
    task: asyncio.Task[None], *, queue_name: str, prior_error: BaseException | None
) -> None:
    """Retrieve cleanup even when caller cancellation first arrives during finally."""
    interruption: asyncio.CancelledError | None = None
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError as exc:
            interruption = exc
        except Exception:
            break
    try:
        task.result()
    except Exception as exc:
        if interruption is None:
            raise
        _LOGGER.error(
            "Failed to clean up %s intake during cancellation",
            queue_name,
            exc_info=(type(exc), exc, exc.__traceback__),
        )
    if interruption is not None:
        # Caller cancellation wins, while a fatal intake failure remains observable.
        if isinstance(prior_error, Exception):
            _LOGGER.error(
                "Failed %s intake before cancellation",
                queue_name,
                exc_info=(type(prior_error), prior_error, prior_error.__traceback__),
            )
        raise interruption


async def consume_queue(
    queue: AbstractQueue,
    handle: Callable[[AbstractIncomingMessage], Awaitable[None]],
    *,
    stop: asyncio.Event | None = None,
) -> None:
    """Own receive/stop tasks so a stop cannot race close against an active receive."""
    if stop is not None and stop.is_set():
        return
    async with queue.iterator() as messages:
        if stop is None:
            async for message in messages:
                await handle(message)
            return
        stopped = asyncio.create_task(stop.wait())
        receive: asyncio.Task[AbstractIncomingMessage] | None = None
        active: asyncio.Task[None] | None = None
        closing: asyncio.Task[None] | None = None

        async def dispatch(message: AbstractIncomingMessage) -> None:
            if not stop.is_set():
                await handle(message)

        async def close_intake() -> None:
            await messages.close()

        def request_close() -> asyncio.Task[None]:
            nonlocal closing
            if closing is None:
                closing = asyncio.create_task(close_intake())
            return closing

        try:
            if stop.is_set():
                await asyncio.shield(request_close())
                return
            while not stop.is_set():
                receive = asyncio.create_task(anext(messages))
                await asyncio.wait((receive, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    _cancel_once(receive)
                    try:
                        await asyncio.shield(receive)
                    except asyncio.CancelledError:
                        current = asyncio.current_task()
                        if current is not None and current.cancelling():
                            raise
                    except StopAsyncIteration:
                        pass
                    receive = None
                    # The receive's cancellation may already have closed the SDK iterator.
                    # Explicit close also covers cancellation before it started waiting.
                    await asyncio.shield(request_close())
                    break
                try:
                    message = receive.result()
                except StopAsyncIteration:
                    receive = None
                    break
                receive = None
                if stop.is_set():
                    await asyncio.shield(request_close())
                    break
                active = asyncio.create_task(dispatch(message))
                await asyncio.wait((active, stopped), return_when=asyncio.FIRST_COMPLETED)
                if stop.is_set():
                    # No receive exists while work is active, so close cannot resurrect it.
                    close_task = request_close()
                    await asyncio.wait((active, close_task))
                    active.result()
                    close_task.result()
                    break
                active.result()
                active = None
        finally:
            prior_error = sys.exception()
            had_error = prior_error is not None

            async def cleanup_owned() -> None:
                owned = [task for task in (receive, active, closing, stopped) if task is not None]
                for task in owned:
                    # SDK close removes its consumer tag before awaiting basic.cancel.
                    # Interrupting it would prevent __aexit__ from retrying cancellation.
                    if task is not closing:
                        _cancel_once(task)
                cleanup = asyncio.gather(*owned, return_exceptions=True)
                results = await asyncio.shield(cleanup)
                # Own context-exit close too: exhaustion/caller cancellation may bypass
                # an explicit stop branch. Receive cancellation has finished above, so
                # this cannot race its SDK-owned close or trigger a new subscription.
                if closing is None:
                    final_close = asyncio.gather(request_close(), return_exceptions=True)
                    results.extend(await asyncio.shield(final_close))
                for result in results:
                    if isinstance(result, Exception):
                        if not had_error:
                            raise result
                        _LOGGER.error(
                            "Failed to clean up %s intake",
                            queue.name,
                            exc_info=(type(result), result, result.__traceback__),
                        )

            cleanup_task = asyncio.create_task(cleanup_owned())
            await _await_cleanup(cleanup_task, queue_name=queue.name, prior_error=prior_error)
