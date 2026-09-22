import asyncio
import signal


async def run() -> None:
    """Keep the process alive until the AMQP consumer is implemented."""
    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for stop_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(stop_signal, stopped.set)
    await stopped.wait()
