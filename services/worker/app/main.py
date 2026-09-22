import asyncio

from app.entrypoints.worker.runner import run

if __name__ == "__main__":
    asyncio.run(run())
