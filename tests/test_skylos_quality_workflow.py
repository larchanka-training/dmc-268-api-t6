"""The Skylos quality gate (#263): ``scripts/skylos_quality.sh``, its config and its CI job.

The script runs against a fake ``uvx`` on ``PATH`` that logs the Skylos command line and exits
with a scripted code; Skylos itself is not installed in the test environment.
"""

from __future__ import annotations

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts/skylos_quality.sh"
WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"
PYPROJECT = REPO_ROOT / "pyproject.toml"
JOB_ID = "skylos-quality"
# Required checks of ruleset `main` on 09.11.2026: the tech lead adds this job after the merge.
REQUIRED_CHECKS = {
    "OpenAPI lint",
    "Python lint / type / test",
    "Webhook container smoke",
    "Edge Caddyfile validate",
}
# Skylos 5.x lists these as advice in a diff scan; the gate counts them only by name.
FUNCTION_RULES = ["SKY-Q301", "SKY-C303", "SKY-C304"]
SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")

FAKE_UVX = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_UVX_LOG}"
codes=(${FAKE_UVX_EXIT})
call="$(wc -l < "${FAKE_UVX_LOG}")"
exit "${codes[$((call - 1))]}"
"""


def _pinned_version() -> str:
    pins = re.findall(
        r"^SKYLOS_VERSION=(\d+\.\d+\.\d+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert len(pins) == 1, pins
    version: str = pins[0]
    return version


def _run_script(
    tmp_path: Path, *args: str, exits: str = "0 0"
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script with a fake ``uvx``; returns the result and the logged command lines."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uvx = bin_dir / "uvx"
    uvx.write_text(FAKE_UVX, encoding="utf-8")
    uvx.chmod(0o755)
    log = tmp_path / "uvx.log"
    log.touch()
    result = subprocess.run(
        [str(SCRIPT), *args],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "FAKE_UVX_LOG": str(log),
            "FAKE_UVX_EXIT": exits,
        },
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    return result, log.read_text(encoding="utf-8").splitlines()


def test_two_gated_passes_with_the_pinned_skylos(tmp_path: Path) -> None:
    reports = tmp_path / "reports"

    result, calls = _run_script(tmp_path, "origin/main", str(reports))

    assert result.returncode == 0, result.stderr
    assert reports.is_dir()
    skylos = f"--from skylos=={_pinned_version()} skylos ."
    assert len(calls) == 2, calls
    lines, functions = (call.split() for call in calls)
    # Pass 1: new findings on the changed lines, every analyzer the gate limits count.
    assert calls[0].startswith(skylos)
    assert {"--quality", "--danger", "--secrets", "--gate", "--summary"} <= set(lines)
    assert " --diff-base origin/main " in calls[0] and " --diff origin/main " in calls[0]
    assert f"--format json-ci -o {reports}/changed-lines.json" in calls[0]
    # Pass 2: size and complexity of the changed functions against the merge base (file level).
    assert calls[1].startswith(skylos)
    assert " --diff-base origin/main " in calls[1] and "--diff" not in functions
    assert f"--select {','.join(FUNCTION_RULES)}" in calls[1]
    assert {"--quality", "--gate", "--summary"} <= set(functions)
    assert "--danger" not in functions and "--secrets" not in functions
    assert f"--format json-ci -o {reports}/changed-functions.json" in calls[1]


@pytest.mark.parametrize(
    ("exits", "code"),
    [("1 0", 1), ("0 1", 1), ("1 1", 1), ("1 2", 2), ("2 1", 2), ("2 0", 2)],
)
def test_the_worst_pass_decides_and_both_passes_run(tmp_path: Path, exits: str, code: int) -> None:
    result, calls = _run_script(tmp_path, "abc123", str(tmp_path / "reports"), exits=exits)

    assert result.returncode == code
    assert len(calls) == 2, calls


def test_reports_default_to_a_temporary_directory(tmp_path: Path) -> None:
    result, calls = _run_script(tmp_path, "origin/main")

    assert result.returncode == 0, result.stderr
    reports = Path(calls[0].split(" -o ")[1].split()[0]).parent
    assert reports.is_dir() and not reports.is_relative_to(REPO_ROOT)


@pytest.mark.parametrize("args", [(), ("",), ("a", "b", "c")])
def test_a_missing_base_is_a_usage_error(tmp_path: Path, args: tuple[str, ...]) -> None:
    result, calls = _run_script(tmp_path, *args)

    assert result.returncode == 2
    assert "usage: scripts/skylos_quality.sh BASE_REF [REPORT_DIR]" in result.stderr
    assert calls == []


def test_skylos_is_pinned_only_in_the_script() -> None:
    _pinned_version()
    tracked = subprocess.run(
        ["git", "grep", "-n", "-I", "-E", r"skylos(==|@)[0-9]"],
        cwd=REPO_ROOT,
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert tracked.returncode == 1, tracked.stdout


def _skylos_config() -> dict[str, Any]:
    config: dict[str, Any] = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["skylos"]
    return config


def test_limits_block_any_new_finding() -> None:
    config = _skylos_config()

    # The curator's starting thresholds (#263).
    assert (config["complexity"], config["max_args"], config["max_lines"]) == (10, 5, 50)
    gate = config["gate"]
    assert (gate["max_quality"], gate["max_high"], gate["max_dead_code"]) == (0, 0, 0)
    assert gate["rules"] == FUNCTION_RULES


def test_every_exclusion_states_its_reason() -> None:
    config = _skylos_config()
    excluded = {"terraform", "test-prs-dataset", ".agents/skills/create-issue"}
    assert set(config["exclude"]) == excluded
    lines = PYPROJECT.read_text(encoding="utf-8").splitlines()
    for entry in config["exclude"]:
        index = lines.index(f'    "{entry}",')
        assert lines[index - 1].lstrip().startswith("# "), entry


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def _step(job: dict[str, Any], name: str) -> dict[str, Any]:
    steps = [step for step in job["steps"] if step.get("name") == name]
    assert len(steps) == 1, f"step {name!r} must exist exactly once"
    step: dict[str, Any] = steps[0]
    return step


def test_job_gates_pull_requests_and_pushes_to_main() -> None:
    workflow = _workflow()
    job = workflow["jobs"][JOB_ID]

    assert job["name"] == "Skylos quality"
    assert job["name"] not in REQUIRED_CHECKS
    # PR and push to main (workflow triggers); a manual run has no diff range. YAML 1.1 reads
    # the `on` key as True.
    triggers: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))[True]
    assert set(triggers) == {"pull_request", "push", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert job["if"] == "github.event_name != 'workflow_dispatch'"
    assert job["permissions"] == {"contents": "read"}
    assert "needs" not in job
    for other_id, other in workflow["jobs"].items():
        needs = other.get("needs", [])
        assert JOB_ID not in ([needs] if isinstance(needs, str) else needs), other_id
    for step in job["steps"]:
        assert "${{" not in step.get("run", ""), step["name"]
        if "uses" in step:
            assert SHA_PINNED.match(step["uses"]), step["uses"]

    checkout = _step(job, "Checkout")
    assert checkout["with"] == {"fetch-depth": 0, "persist-credentials": False}
    gate = _step(job, "Skylos gate")
    assert gate["env"] == {
        "BASE_SHA": "${{ github.event_name == 'pull_request'"
        " && github.event.pull_request.base.sha || github.event.before }}"
    }
    assert gate["run"].strip() == 'scripts/skylos_quality.sh "${BASE_SHA}" "${RUNNER_TEMP}/skylos"'
    upload = _step(job, "Upload Skylos report")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/skylos/"
