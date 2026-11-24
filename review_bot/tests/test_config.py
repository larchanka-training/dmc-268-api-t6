from __future__ import annotations

from datetime import time, timedelta
from pathlib import Path

import pytest

from review_bot.config import ConfigError, Member, load_settings, parse_repos, parse_team

SECRET_ENV = {
    "REVIEW_BOT_APP_ID": "1",
    "REVIEW_BOT_APP_PRIVATE_KEY": "pem",
    "REVIEW_BOT_TELEGRAM_BOT_TOKEN": "123:secret",
    "REVIEW_BOT_TELEGRAM_CHAT_ID": "-100777",
    "REVIEW_BOT_TEAM": '[{"github": "alice-example", "telegram": "@alice_tg"}]',
}


def test_defaults_are_the_agreed_thresholds() -> None:
    settings = load_settings(SECRET_ENV)
    limits = settings.thresholds
    assert limits.batch_min_prs == 2
    assert limits.quiet == timedelta(minutes=30)
    assert limits.max_wait == timedelta(hours=2)
    assert limits.digest_at == time(18, 0)
    assert str(limits.digest_tz) == "Europe/Berlin"
    assert limits.age_shown_after == timedelta(hours=10)
    assert limits.urgent_label == "urgent"
    assert [r.alias for r in settings.repos] == ["api", "ui"]
    assert settings.team == (Member("alice-example", "alice_tg"),)


def test_thresholds_can_be_changed() -> None:
    env = {
        **SECRET_ENV,
        "REVIEW_BOT_BATCH_MIN_PRS": "3",
        "REVIEW_BOT_QUIET_MINUTES": "15",
        "REVIEW_BOT_MAX_WAIT_MINUTES": "60",
        "REVIEW_BOT_DIGEST_TIME": "17:30",
        "REVIEW_BOT_AGE_HOURS": "8",
    }
    limits = load_settings(env).thresholds
    assert (limits.batch_min_prs, limits.quiet, limits.max_wait) == (
        3,
        timedelta(minutes=15),
        timedelta(hours=1),
    )
    assert (limits.digest_at, limits.age_shown_after) == (time(17, 30), timedelta(hours=8))


@pytest.mark.parametrize("missing", sorted(SECRET_ENV))
def test_a_missing_secret_is_named_without_values(missing: str) -> None:
    env = {k: v for k, v in SECRET_ENV.items() if k != missing}
    with pytest.raises(ConfigError) as error:
        load_settings(env)
    assert missing in str(error.value)
    for value in SECRET_ENV.values():
        assert value not in str(error.value)


@pytest.mark.parametrize(
    "raw",
    [
        "not json",
        "[]",
        '[{"github": "a"}]',
        '[{"github": "a", "telegram": "x"}, {"github": "A", "telegram": "y"}]',
    ],
)
def test_a_bad_roster_is_refused(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_team(raw)


def test_the_example_roster_in_the_repo_parses() -> None:
    example = Path(__file__).parents[1] / "team.example.json"
    assert len(parse_team(example.read_text(encoding="utf-8"))) >= 2


@pytest.mark.parametrize(
    "raw",
    [
        " , ",
        "acme/api",
        "acme=api",
        "/api=api",
        "acme/=api",
        "acme/api= ",
    ],
)
def test_bad_repos_are_refused(raw: str) -> None:
    with pytest.raises(ConfigError):
        parse_repos(raw)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("REVIEW_BOT_BATCH_MIN_PRS", "not-an-int"),
        ("REVIEW_BOT_BATCH_MIN_PRS", "0"),
        ("REVIEW_BOT_POLL_SECONDS", "-5"),
        ("REVIEW_BOT_DIGEST_TIME", "25:00"),
    ],
)
def test_bad_numeric_or_time_settings_are_refused(name: str, value: str) -> None:
    with pytest.raises(ConfigError) as error:
        load_settings({**SECRET_ENV, name: value})
    assert name in str(error.value)
