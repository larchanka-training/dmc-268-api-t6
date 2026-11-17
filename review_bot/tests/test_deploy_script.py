"""review_bot/deploy.sh writes review-bot.env from the CI bundle (no Docker needed)."""

from __future__ import annotations

import base64
import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "deploy.sh"
VALUES = {
    "REVIEW_BOT_APP_ID": "12345",
    "REVIEW_BOT_APP_PRIVATE_KEY": "-----BEGIN KEY-----\nAAAA\n-----END KEY-----",
    "REVIEW_BOT_TELEGRAM_BOT_TOKEN": "1:abc",
    "REVIEW_BOT_TELEGRAM_CHAT_ID": "-100",
    "REVIEW_BOT_TEAM": '[{"github": "example-a", "telegram": "example_a"}]',
}


def bundle(values: dict[str, str]) -> str:
    lines = "".join(
        f"{name}={base64.b64encode(value.encode()).decode()}\n" for name, value in values.items()
    )
    return base64.b64encode(lines.encode()).decode()


def write(tmp_path: Path, values: dict[str, str]) -> subprocess.CompletedProcess[str]:
    command = 'source "$1"; write_env_file "$2"'
    return subprocess.run(
        ["bash", "-c", command, "bash", str(SCRIPT), bundle(values)],
        env={
            **os.environ,
            "REVIEW_BOT_DEPLOY_SOURCE_ONLY": "1",
            "REVIEW_BOT_DIR": str(tmp_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )


def test_the_bundle_becomes_a_private_env_file(tmp_path: Path) -> None:
    result = write(tmp_path, VALUES)
    assert result.returncode == 0, result.stderr
    env_file = tmp_path / "review-bot.env"
    assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
    text = env_file.read_text(encoding="utf-8")
    for name, value in VALUES.items():
        assert f"{name}='{value}'\n" in text
    assert list(tmp_path.iterdir()) == [env_file]


@pytest.mark.parametrize(
    "values",
    [
        {**VALUES, "OTHER_SECRET": "x"},
        {k: v for k, v in VALUES.items() if k != "REVIEW_BOT_TEAM"},
        {**VALUES, "REVIEW_BOT_TEAM": "it's"},
    ],
)
def test_a_bad_bundle_writes_nothing_and_shows_no_value(
    tmp_path: Path, values: dict[str, str]
) -> None:
    result = write(tmp_path, values)
    assert result.returncode != 0
    assert list(tmp_path.iterdir()) == []
    for value in values.values():
        assert value not in result.stdout + result.stderr
