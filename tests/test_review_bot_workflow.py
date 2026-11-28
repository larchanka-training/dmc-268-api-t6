"""The review bot jobs of ci-cd.yml (#601): its checks, and a deploy that skips cleanly.

``Decide whether to deploy`` runs as written in the workflow, in a throwaway git repository.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"
SECRET_FLAGS = (
    "HAS_APP_ID",
    "HAS_APP_PRIVATE_KEY",
    "HAS_TELEGRAM_BOT_TOKEN",
    "HAS_TELEGRAM_CHAT_ID",
    "HAS_TEAM",
)


def _job(job_id: str) -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    job: dict[str, Any] = workflow["jobs"][job_id]
    return job


def _step(job_id: str, name: str) -> dict[str, Any]:
    (step,) = [s for s in _job(job_id)["steps"] if s.get("name") == name]
    result: dict[str, Any] = step
    return result


def _git(repo: Path, *args: str) -> str:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    result = subprocess.run(
        ["git", *args], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


@pytest.fixture
def history(tmp_path: Path) -> tuple[Path, str, str, str]:
    """(repo, base, a commit outside review_bot/, a commit inside it)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "review_bot").mkdir()
    (repo / "review_bot" / "x.py").write_text("a\n")
    (repo / "app.py").write_text("a\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "app.py").write_text("b\n")
    _git(repo, "commit", "-q", "-am", "app")
    outside = _git(repo, "rev-parse", "HEAD")
    (repo / "review_bot" / "x.py").write_text("b\n")
    _git(repo, "commit", "-q", "-am", "bot")
    inside = _git(repo, "rev-parse", "HEAD")
    return repo, base, outside, inside


def _decide(
    repo: Path, tmp_path: Path, event: str, before: str, sha: str, secrets: bool
) -> tuple[str, str]:
    output = tmp_path / "github_output"
    output.write_text("")
    env = {
        "PATH": os.environ["PATH"],
        "EVENT": event,
        "BEFORE": before,
        "GITHUB_SHA": sha,
        "GITHUB_OUTPUT": str(output),
        **{flag: "true" if secrets else "false" for flag in SECRET_FLAGS},
    }
    script = _step("deploy-review-bot", "Decide whether to deploy")["run"]
    result = subprocess.run(
        ["bash", "-e", "-c", script], cwd=repo, env=env, capture_output=True, text=True, check=True
    )
    return output.read_text(), result.stdout


def test_a_push_without_review_bot_changes_skips(
    history: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    repo, base, outside, _ = history
    output, log = _decide(repo, tmp_path, "push", base, outside, secrets=True)
    assert output == "deploy=false\n"
    assert "review_bot/ unchanged" in log


def test_a_push_with_changes_deploys_when_the_secrets_exist(
    history: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    repo, _, outside, inside = history
    assert _decide(repo, tmp_path, "push", outside, inside, secrets=True)[0] == "deploy=true\n"


def test_missing_secrets_skip_with_their_names(
    history: tuple[Path, str, str, str], tmp_path: Path
) -> None:
    repo, _, outside, inside = history
    output, log = _decide(repo, tmp_path, "push", outside, inside, secrets=False)
    assert output == "deploy=false\n"
    assert "::notice::" in log
    for name in ("REVIEW_BOT_APP_PRIVATE_KEY", "REVIEW_BOT_TELEGRAM_CHAT_ID", "REVIEW_BOT_TEAM"):
        assert name in log


@pytest.mark.parametrize(
    ("event", "before"), [("workflow_dispatch", ""), ("push", "0" * 40), ("push", "f" * 40)]
)
def test_a_manual_run_or_an_unknown_base_deploys(
    history: tuple[Path, str, str, str], tmp_path: Path, event: str, before: str
) -> None:
    repo, _, outside, _ = history
    assert _decide(repo, tmp_path, event, before, outside, secrets=True)[0] == "deploy=true\n"


def test_the_deploy_job_follows_the_monitoring_pattern() -> None:
    job = _job("deploy-review-bot")
    assert job["needs"] == "deploy-staging"
    assert "needs.deploy-staging.result == 'success'" in job["if"]
    assert job["environment"] == {"name": "staging"}
    # Never the staging-deploy lock: the bot must not delay the application deploys.
    assert job["concurrency"]["group"] == "review-bot-deploy"
    for name in ("Bundle review bot secrets", "Upload review bot files", "Deploy review bot"):
        assert _step("deploy-review-bot", name)["if"] == "steps.plan.outputs.deploy == 'true'"
    uploaded = _step("deploy-review-bot", "Upload review bot files")["with"]["source"].split(",")
    for path in uploaded:
        assert (REPO_ROOT / path).exists(), path


def test_the_checks_job_runs_the_review_bot_gates() -> None:
    job = _job("review-bot-checks")
    assert job["defaults"]["run"]["working-directory"] == "review_bot"
    commands = [step.get("run") for step in job["steps"]]
    for command in ("uv sync --locked", "uv run ruff check .", "uv run mypy", "uv run pytest -rs"):
        assert command in commands
