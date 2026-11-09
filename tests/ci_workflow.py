"""Shared helpers of the tests over jobs of ``.github/workflows/ci-cd.yml`` (#378, #263)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"
# Required checks of ruleset `main` on 09.11.2026
# (gh api repos/larchanka-training/dmc-268-api-t6/rules/branches/main): a new job is never one
# of them, the tech lead adds it after the merge.
REQUIRED_CHECKS = {
    "OpenAPI lint",
    "Python lint / type / test",
    "Webhook container smoke",
    "Edge Caddyfile validate",
}


def ci_workflow() -> dict[Any, Any]:
    """``ci-cd.yml`` as YAML 1.1 reads it: the ``on`` key is ``True``."""
    workflow: dict[Any, Any] = yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def job_step(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps = [step for step in job["steps"] if step.get("name") == name]
    assert len(steps) == 1, f"step {name!r} must exist exactly once"
    step: dict[str, Any] = steps[0]
    return step


def git_env() -> dict[str, str]:
    """No inherited ``GIT_*`` key (a hook's ``GIT_INDEX_FILE``) and no user or system config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.test",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.test",
    }
    return env


def git(repo: Path, *args: str) -> str:
    """Git in ``repo`` under :func:`git_env`; returns the stripped stdout."""
    return subprocess.run(
        ["git", *args], cwd=repo, env=git_env(), check=True, capture_output=True, text=True
    ).stdout.strip()
