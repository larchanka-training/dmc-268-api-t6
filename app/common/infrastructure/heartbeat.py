"""Liveness file of a background worker process.

A worker without an HTTP port proves it is alive by touching a file; the container healthcheck
runs ``python -m app.common.infrastructure.heartbeat <path> <max-age-seconds>`` and fails when the
last beat is older than that. ``WORKER_HEARTBEAT_FILE`` enables it; without it nothing is written.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

HEARTBEAT_PERIOD = 10.0


def heartbeat_file(env: Mapping[str, str]) -> Path | None:
    value = env.get("WORKER_HEARTBEAT_FILE")
    return Path(value) if value else None


def reset(path: Path) -> None:
    """Drop the beat of a previous process: a restarted container keeps its /tmp."""
    path.unlink(missing_ok=True)


async def beat(path: Path, period: float = HEARTBEAT_PERIOD) -> NoReturn:
    while True:
        path.touch()
        await asyncio.sleep(period)


def is_fresh(path: Path, max_age: float, now: float) -> bool:
    try:
        return now - path.stat().st_mtime <= max_age
    except FileNotFoundError:
        return False


def main(argv: list[str]) -> int:
    path, max_age = argv
    return 0 if is_fresh(Path(path), float(max_age), time.time()) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
