"""Entry point: poll GitHub every minute, answer Telegram commands in between."""

from __future__ import annotations

import logging
import os
import signal
import sys
import time
from pathlib import Path

from review_bot.config import ConfigError, load_settings
from review_bot.github import GitHubAppClient
from review_bot.service import Bot
from review_bot.store import Store
from review_bot.telegram import TelegramClient

log = logging.getLogger("review_bot")


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("REVIEW_BOT_LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    # urllib logs nothing by itself; keep any library from logging request URLs (the bot token
    # is part of the Telegram URL).
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    # PID 1 in the container: without a handler SIGTERM is ignored and `docker stop` waits.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    try:
        settings = load_settings()
    except ConfigError as error:
        log.error("configuration error: %s", error)
        return 2
    store = Store(settings.db_path)
    bot = Bot(
        settings,
        store,
        GitHubAppClient(settings.app_id, settings.app_private_key),
        TelegramClient(settings.telegram_token),
    )
    heartbeat = Path(settings.heartbeat_path)
    log.info(
        "review bot started: %d repos, %d team members, poll every %ds",
        len(settings.repos),
        len(settings.team),
        settings.poll_seconds,
    )
    while True:
        started = time.monotonic()
        try:
            bot.tick()
        except Exception:
            log.exception("poll failed")
        heartbeat.touch()
        # Telegram long polling fills the rest of the minute, so /mine is answered at once.
        while (left := settings.poll_seconds - (time.monotonic() - started)) > 1:
            try:
                ok = bot.poll_commands(timeout=int(min(left, 50)))
            except Exception:
                log.exception("command handling failed")
                ok = False
            if not ok:
                time.sleep(min(left, 5))


if __name__ == "__main__":
    sys.exit(main())
