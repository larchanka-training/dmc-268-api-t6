"""The SYNC drift workflow (#270): a separate check, outside the required checks of ruleset
`main`, that runs ``scripts/check_sync_drift.py`` against a checkout of ui ``main``.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/sync-drift.yml"
SCRIPT = "python3 scripts/check_sync_drift.py"


def _workflow() -> dict[Any, Any]:
    workflow: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_sync_drift", REPO_ROOT / "scripts/check_sync_drift.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runs_on_prs_that_touch_sync_paths_and_on_every_push_to_main() -> None:
    # YAML 1.1 reads the key `on` as True. No schedule (tech lead, 09.11), no pull_request_target.
    triggers = _workflow()[True]

    assert triggers == {
        "pull_request": {
            "paths": [
                ".agents/**",
                ".github/**",
                "scripts/check_sync_drift.py",
            ]
        },
        "push": {"branches": ["main"]},
    }


def test_a_pr_that_changes_any_file_the_script_scans_runs_the_check() -> None:
    # A new SYNC file anywhere under a scanned directory, e.g. `.github/ISSUE_TEMPLATE/`, must
    # trigger the check on its PR, not only on the push to `main`. GitHub's `*` stops at `/`.
    paths = _workflow()[True]["pull_request"]["paths"]

    for directory in _script().SCAN_DIRS:
        assert f"{directory}/**" in paths, directory


def test_compares_this_tree_with_a_checkout_of_ui_main() -> None:
    workflow = _workflow()
    (job,) = workflow["jobs"].values()
    steps = job["steps"]

    assert workflow["permissions"] == {"contents": "read"}
    # The job name is the check name: never one of the required contexts of ruleset `main`.
    assert job["name"] == "SYNC drift vs ui main"
    uses = [step["uses"] for step in steps if "uses" in step]
    assert all(re.fullmatch(r"actions/checkout@[0-9a-f]{40}", ref) for ref in uses), uses
    api, ui = (step.get("with", {}) for step in steps if "uses" in step)
    assert api == {"persist-credentials": False}
    assert ui == {
        "repository": "larchanka-training/dmc-268-ui-t6",
        "ref": "main",
        "path": "ui-main",
        "persist-credentials": False,
    }
    runs = [(step["run"].strip(), step.get("if")) for step in steps if "run" in step]
    # The JSON step runs after a red marker step too, so one run lists both kinds of drift.
    assert runs == [
        (f"{SCRIPT} markers --ui ui-main --api .", None),
        (f"{SCRIPT} json-dirs --ui ui-main --api .", "${{ !cancelled() }}"),
    ]
