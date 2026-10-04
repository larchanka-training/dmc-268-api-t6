"""Liveness file of the background workers: the staging healthcheck reads its age (#35)."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.common.infrastructure.heartbeat import beat, heartbeat_file, is_fresh, reset


def test_reset_removes_a_beat_left_by_the_previous_process(tmp_path: Path) -> None:
    path = tmp_path / "worker.heartbeat"
    path.touch()

    reset(path)
    reset(path)  # nothing to remove: no error

    assert not path.exists()


def test_beat_touches_the_file_every_period(tmp_path: Path) -> None:
    path = tmp_path / "worker.heartbeat"

    async def scenario() -> list[float]:
        task = asyncio.create_task(beat(path, period=0.01))
        seen: list[float] = []
        for _ in range(3):
            await asyncio.sleep(0.03)
            seen.append(path.stat().st_mtime_ns)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return seen

    seen = asyncio.run(scenario())

    assert seen == sorted(seen)
    assert len(set(seen)) > 1


def test_freshness_is_the_age_of_the_last_beat(tmp_path: Path) -> None:
    path = tmp_path / "worker.heartbeat"
    assert not is_fresh(path, max_age=60, now=1_000.0)

    path.touch()
    os.utime(path, (900.0, 900.0))

    assert is_fresh(path, max_age=100, now=1_000.0)
    assert not is_fresh(path, max_age=99, now=1_000.0)


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"WORKER_HEARTBEAT_FILE": ""}, None),
        ({"WORKER_HEARTBEAT_FILE": "/tmp/worker.heartbeat"}, Path("/tmp/worker.heartbeat")),
    ],
)
def test_heartbeat_is_enabled_only_by_its_variable(
    env: dict[str, str], expected: Path | None
) -> None:
    assert heartbeat_file(env) == expected


def test_healthcheck_command_exits_by_freshness(tmp_path: Path) -> None:
    """The exact command of deploy/compose/staging.yml, run as the container runs it."""
    path = tmp_path / "worker.heartbeat"

    def check() -> int:
        command = [sys.executable, "-m", "app.common.infrastructure.heartbeat", str(path), "60"]
        return subprocess.run(command, check=False, capture_output=True).returncode

    assert check() == 1
    path.touch()
    assert check() == 0
    os.utime(path, (1.0, 1.0))
    assert check() == 1
