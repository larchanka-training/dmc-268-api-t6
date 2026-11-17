"""Settings of the review bot, read from the environment.

Secrets (App key, bot token, chat id) and the team roster come from env variables only; the
thresholds have the defaults agreed in #601 and are set in review_bot/compose.yml.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

DEFAULT_REPOS = "larchanka-training/dmc-268-api-t6=api,larchanka-training/dmc-268-ui-t6=ui"


class ConfigError(ValueError):
    """A setting is missing or malformed. The message names the variable, never its value."""


@dataclass(frozen=True)
class Member:
    github: str
    telegram: str

    @property
    def key(self) -> str:
        return self.github.lower()


@dataclass(frozen=True)
class Repo:
    owner: str
    name: str
    alias: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class Thresholds:
    batch_min_prs: int = 2
    quiet: timedelta = timedelta(minutes=30)
    max_wait: timedelta = timedelta(hours=2)
    digest_at: time = time(18, 0)
    digest_tz: ZoneInfo = field(default_factory=lambda: ZoneInfo("Europe/Berlin"))
    # The digest is sent once a day, on the first poll in this window after digest_at: a bot
    # started late in the evening does not send a "by 18:00" digest at midnight.
    digest_window: timedelta = timedelta(hours=1)
    age_shown_after: timedelta = timedelta(hours=10)
    urgent_label: str = "urgent"


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_private_key: str
    telegram_token: str
    chat_id: str
    team: tuple[Member, ...]
    repos: tuple[Repo, ...]
    db_path: str
    poll_seconds: int
    heartbeat_path: str
    thresholds: Thresholds


def parse_team(raw: str) -> tuple[Member, ...]:
    """The roster: a JSON list of {"github": login, "telegram": username}."""
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigError("REVIEW_BOT_TEAM is not valid JSON") from error
    if not isinstance(data, list) or not data:
        raise ConfigError("REVIEW_BOT_TEAM must be a non-empty JSON list")
    members: list[Member] = []
    seen: set[str] = set()
    for item in data:
        if not isinstance(item, dict):
            raise ConfigError("REVIEW_BOT_TEAM entries must be objects")
        github = item.get("github")
        telegram = item.get("telegram")
        if not isinstance(github, str) or not github.strip():
            raise ConfigError("REVIEW_BOT_TEAM entry without a github login")
        if not isinstance(telegram, str) or not telegram.strip():
            raise ConfigError("REVIEW_BOT_TEAM entry without a telegram username")
        member = Member(github=github.strip(), telegram=telegram.strip().lstrip("@"))
        if member.key in seen:
            raise ConfigError("REVIEW_BOT_TEAM lists a github login twice")
        seen.add(member.key)
        members.append(member)
    return tuple(members)


def parse_repos(raw: str) -> tuple[Repo, ...]:
    """`owner/name=alias` pairs separated by commas."""
    repos: list[Repo] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        full, sep, alias = chunk.partition("=")
        owner, slash, name = full.strip().partition("/")
        if not sep or not slash or not owner or not name or not alias.strip():
            raise ConfigError("REVIEW_BOT_REPOS must be owner/name=alias pairs")
        repos.append(Repo(owner=owner, name=name, alias=alias.strip()))
    if not repos:
        raise ConfigError("REVIEW_BOT_REPOS is empty")
    return tuple(repos)


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"{name} is not set")
    return value


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 1) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be an integer") from error
    if value < minimum:
        raise ConfigError(f"{name} must be at least {minimum}")
    return value


def _clock_time(env: Mapping[str, str], name: str, default: time) -> time:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return time.fromisoformat(raw)
    except ValueError as error:
        raise ConfigError(f"{name} must be HH:MM") from error


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    tz_name = env.get("REVIEW_BOT_DIGEST_TIMEZONE", "").strip() or "Europe/Berlin"
    try:
        tz = ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ConfigError("REVIEW_BOT_DIGEST_TIMEZONE is not a known time zone") from error
    thresholds = Thresholds(
        batch_min_prs=_int(env, "REVIEW_BOT_BATCH_MIN_PRS", 2),
        quiet=timedelta(minutes=_int(env, "REVIEW_BOT_QUIET_MINUTES", 30)),
        max_wait=timedelta(minutes=_int(env, "REVIEW_BOT_MAX_WAIT_MINUTES", 120)),
        digest_at=_clock_time(env, "REVIEW_BOT_DIGEST_TIME", time(18, 0)),
        digest_tz=tz,
        age_shown_after=timedelta(hours=_int(env, "REVIEW_BOT_AGE_HOURS", 10)),
        urgent_label=env.get("REVIEW_BOT_URGENT_LABEL", "").strip() or "urgent",
    )
    return Settings(
        app_id=_required(env, "REVIEW_BOT_APP_ID"),
        app_private_key=_required(env, "REVIEW_BOT_APP_PRIVATE_KEY"),
        telegram_token=_required(env, "REVIEW_BOT_TELEGRAM_BOT_TOKEN"),
        chat_id=_required(env, "REVIEW_BOT_TELEGRAM_CHAT_ID"),
        team=parse_team(_required(env, "REVIEW_BOT_TEAM")),
        repos=parse_repos(env.get("REVIEW_BOT_REPOS", "").strip() or DEFAULT_REPOS),
        db_path=env.get("REVIEW_BOT_DB_PATH", "").strip() or "/data/review-bot.sqlite3",
        poll_seconds=_int(env, "REVIEW_BOT_POLL_SECONDS", 60),
        heartbeat_path=env.get("REVIEW_BOT_HEARTBEAT_PATH", "").strip() or "/tmp/review-bot.alive",
        thresholds=thresholds,
    )
