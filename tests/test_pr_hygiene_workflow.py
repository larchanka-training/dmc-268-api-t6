"""The PR hygiene workflow: separate pull-request check running check_pr_hygiene.py."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/pr-hygiene.yml"
SCRIPT = "uv run --no-project python .github/scripts/check_pr_hygiene.py"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def test_runs_on_pull_request_metadata_and_sync_events() -> None:
    triggers = _workflow()[True]
    assert triggers == {
        "pull_request": {
            "types": ["opened", "edited", "synchronize", "reopened", "ready_for_review"]
        }
    }


def test_uses_pinned_actions_read_permissions_and_uv_step_order() -> None:
    workflow = _workflow()
    (job,) = workflow["jobs"].values()
    steps = job["steps"]

    assert workflow["permissions"] == {"contents": "read"}
    assert job["name"] == "PR hygiene"
    assert job["timeout-minutes"] == 5
    assert [step["name"] for step in steps] == [
        "Checkout",
        "Setup uv",
        "Check PR hygiene",
    ]
    refs = [step["uses"] for step in steps if "uses" in step]
    assert all(re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", ref) for ref in refs), refs
    actions = [(step["uses"].split("@")[0], step.get("with")) for step in steps if "uses" in step]
    assert actions == [
        ("actions/checkout", {"fetch-depth": 0, "persist-credentials": False}),
        ("astral-sh/setup-uv", {"version": "0.12.11", "python-version": "3.13"}),
    ]
    runs = [step["run"].strip() for step in steps if "run" in step]
    assert runs == [SCRIPT]
    for step in steps:
        assert "${{" not in step.get("run", ""), step["name"]
