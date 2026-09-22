import asyncio

from app.entrypoints.publisher.runner import run

if __name__ == "__main__":
    asyncio.run(run())
