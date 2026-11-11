"""The Markdown link workflow (#270, Part 2): a separate check, outside the required checks of
ruleset `main`, that runs ``.github/scripts/check_md_links.py`` on every pull request.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/md-links.yml"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def test_runs_on_every_pr_and_on_every_push_to_main() -> None:
    # YAML 1.1 reads the key `on` as True. No paths filter: a PR that moves or deletes a file
    # breaks the links to it without touching any Markdown.
    triggers = _workflow()[True]

    assert triggers == {"pull_request": None, "push": {"branches": ["main"]}}


def test_runs_the_script_through_uv_after_a_pinned_checkout() -> None:
    workflow = _workflow()
    (job,) = workflow["jobs"].values()
    steps = job["steps"]

    assert workflow["permissions"] == {"contents": "read"}
    # The job name is the check name: never one of the required contexts of ruleset `main`.
    assert job["name"] == "Markdown links"
    assert [step["name"] for step in steps] == ["Checkout", "Setup uv", "Check relative links"]
    refs = [step["uses"] for step in steps if "uses" in step]
    assert all(re.fullmatch(r"[\w-]+/[\w-]+@[0-9a-f]{40}", ref) for ref in refs), refs
    actions = [(step["uses"].split("@")[0], step.get("with")) for step in steps if "uses" in step]
    assert actions == [
        ("actions/checkout", {"persist-credentials": False}),
        # The same uv and Python as the other jobs that set up uv.
        ("astral-sh/setup-uv", {"version": "0.12.11", "python-version": "3.13"}),
    ]
    # uv runs tools here (AGENTS.md); the script is stdlib only, so it needs no project environment.
    runs = [step["run"].strip() for step in steps if "run" in step]
    assert runs == ["uv run --no-project python .github/scripts/check_md_links.py"]
