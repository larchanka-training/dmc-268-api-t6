from __future__ import annotations

from dataclasses import dataclass

from fakes import Clock, FakeGitHub, FakeTelegram, make_settings

from review_bot.service import Bot
from review_bot.store import Store


@dataclass
class Harness:
    github: FakeGitHub
    telegram: FakeTelegram
    clock: Clock
    db_path: str
    bot: Bot

    def restart(self) -> None:
        """A new process on the same SQLite file: nothing survives but the database."""
        self.bot.store.close()
        self.bot = Bot(
            make_settings(self.db_path), Store(self.db_path), self.github, self.telegram, self.clock
        )

    def tick(self) -> None:
        self.bot.tick()
