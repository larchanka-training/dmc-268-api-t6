"""Shared fakes and clock for leader background tasks (#556)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

type Runs = list[tuple[str, float, float]]


@dataclass
class Clock:
    """A callable clock whose time can be read and set manually."""

    now: float = 0.0

    def __call__(self) -> float:
        return self.now


@dataclass
class Recorder:
    """A task that records when it ran with which budget and spends `cost` seconds."""

    name: str
    clock: Clock
    runs: Runs
    cost: float = 0.0
    error: BaseException | None = None
    remains: bool = False
    hang: bool = False
    hang_advance: float = 0.0
    hung_once: bool = False

    async def run(self, budget_seconds: float) -> bool:
        self.runs.append((self.name, self.clock.now, budget_seconds))
        if self.hang:
            self.clock.now += self.hang_advance
            await asyncio.Event().wait()
        if self.hung_once:
            self.hung_once = False
            await asyncio.Event().wait()
        self.clock.now += self.cost
        if self.error is not None:
            raise self.error
        return self.remains
