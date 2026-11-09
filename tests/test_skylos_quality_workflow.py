"""The Skylos quality gate (#263): ``scripts/skylos_quality.sh``, its config and its CI job.

The script runs against a fake ``uvx`` on ``PATH`` that logs the Skylos command line and exits
with a scripted code; Skylos itself is not installed in the test environment.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
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
report='{"code_health": []}'
if [[ -n "${FAKE_UVX_REPORT:-}" ]]; then
  report="${FAKE_UVX_REPORT}"
fi
while (($#)); do
  if [[ "$1" == -o ]]; then
    printf '%s' "${report}" > "$2"
  fi
  shift
done
exit "${codes[$((call - 1))]}"
"""
# Logs the command line, then runs the real uv: the checker's verdict stays end to end.
FAKE_UV = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "${FAKE_UV_LOG}"
exec "${REAL_UV}" "$@"
"""
CHECKER = REPO_ROOT / "scripts/skylos_changed_functions.py"


def _pinned_version() -> str:
    pins = re.findall(
        r"^SKYLOS_VERSION=(\d+\.\d+\.\d+)$", SCRIPT.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert len(pins) == 1, pins
    version: str = pins[0]
    return version


def _run_script(
    tmp_path: Path, *args: str, exits: str = "0 0", report: str = ""
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    """Run the script with a fake ``uvx``; returns the result and the logged command lines.

    ``uv`` is logged to ``uv.log`` in ``tmp_path`` and runs for real.
    """
    real_uv = shutil.which("uv")
    assert real_uv, "uv must be on PATH"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, fake in (("uvx", FAKE_UVX), ("uv", FAKE_UV)):
        (bin_dir / name).write_text(fake, encoding="utf-8")
        (bin_dir / name).chmod(0o755)
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
            "FAKE_UVX_REPORT": report,
            "FAKE_UV_LOG": str(tmp_path / "uv.log"),
            "REAL_UV": real_uv,
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
    # --gate keeps the policy of the merge base; the verdict is the checker's, so --force.
    assert calls[1].startswith(skylos)
    assert " --diff-base origin/main " in calls[1] and "--diff" not in functions
    assert f"--select {','.join(FUNCTION_RULES)}" in calls[1]
    assert {"--quality", "--gate", "--force"} <= set(functions)
    assert not {"--danger", "--secrets", "--summary"} & set(functions)
    assert f"--format json-ci -o {reports}/changed-functions.json" in calls[1]


def test_the_checker_runs_on_the_project_python_through_uv(tmp_path: Path) -> None:
    reports = tmp_path / "reports"
    python = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["tool"]["mypy"]["python_version"]

    result, _ = _run_script(tmp_path, "origin/main", str(reports))

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "uv.log").read_text(encoding="utf-8").splitlines() == [
        f"run --no-project --python {python} scripts/skylos_changed_functions.py "
        f"{reports}/changed-functions.json {','.join(FUNCTION_RULES)}"
    ]


@pytest.mark.parametrize(
    ("exits", "code"),
    [("1 0", 1), ("0 1", 1), ("1 1", 1), ("1 2", 2), ("2 1", 2), ("2 0", 2)],
)
def test_the_worst_pass_decides_and_both_passes_run(tmp_path: Path, exits: str, code: int) -> None:
    result, calls = _run_script(tmp_path, "abc123", str(tmp_path / "reports"), exits=exits)

    assert result.returncode == code
    assert len(calls) == 2, calls


def _health(change: str, rule: str = "SKY-C304") -> dict[str, Any]:
    return {
        "rule_id": rule,
        "file": f"{REPO_ROOT}/app/example.py",
        "line": 7,
        "message": "Function has 55 code lines (limit: 50)",
        "code_health_change": change,
    }


@pytest.mark.parametrize(
    ("change", "rule", "code"),
    [
        ("introduced", "SKY-C304", 1),
        ("worsened", "SKY-Q301", 1),
        # No base version: a moved legacy file, or a new file that pass 1 checks line by line.
        ("unverified", "SKY-C304", 0),
        ("introduced", "SKY-L028", 0),
    ],
)
def test_only_functions_the_change_made_worse_fail(
    tmp_path: Path, change: str, rule: str, code: int
) -> None:
    report = json.dumps({"code_health": [_health(change, rule)]})

    result, _ = _run_script(tmp_path, "origin/main", str(tmp_path / "reports"), report=report)

    assert result.returncode == code
    assert ("app/example.py:7" in result.stdout) is bool(code)


def _check(tmp_path: Path, report: str) -> subprocess.CompletedProcess[str]:
    path = tmp_path / "changed-functions.json"
    path.write_text(report, encoding="utf-8")
    summary = tmp_path / "summary.md"
    return subprocess.run(
        [sys.executable, str(CHECKER), str(path), ",".join(FUNCTION_RULES)],
        cwd=REPO_ROOT,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
        capture_output=True,
        encoding="utf-8",
        check=False,
    )


def test_the_checker_lists_each_failing_function_in_the_log_and_summary(tmp_path: Path) -> None:
    report = json.dumps({"code_health": [_health("worsened"), _health("unverified")]})

    result = _check(tmp_path, report)

    assert result.returncode == 1
    failing = "SKY-C304  app/example.py:7  Function has 55 code lines (limit: 50) (worsened)"
    assert failing in result.stdout
    assert "unverified" not in result.stdout
    assert failing in (tmp_path / "summary.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("report", ["", "[]", "{}", '{"code_health": {}}'])
def test_an_unreadable_report_is_incomplete(tmp_path: Path, report: str) -> None:
    result = _check(tmp_path, report)

    assert result.returncode == 2
    assert "cannot read" in result.stderr


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
        "EVENT_NAME": "${{ github.event_name }}",
        "PUSH_BEFORE": "${{ github.event.before }}",
    }
    upload = _step(job, "Upload Skylos report")
    assert upload["if"] == "always()"
    assert upload["with"]["path"] == "${{ runner.temp }}/skylos/"
    # Both passes always write a report: a missing one means the gate step broke.
    assert upload["with"]["if-no-files-found"] == "error"


FAKE_GATE_SCRIPT = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "${FAKE_GATE_ARGS}"
"""


def _git(repo: Path, *args: str) -> str:
    """Git without the caller's ``GIT_*`` keys (a hook's index) and user or system config."""
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env |= {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
    identity = ["-c", "user.name=t", "-c", "user.email=t@example.test"]
    return subprocess.run(
        ["git", *identity, *args], cwd=repo, env=env, check=True, capture_output=True, text=True
    ).stdout.strip()


def _gate_args(tmp_path: Path, repo: Path, event: str, before: str) -> list[str]:
    """Run the ``Skylos gate`` step in ``repo`` with a fake script; returns its arguments."""
    (repo / "scripts").mkdir(exist_ok=True)
    script = repo / "scripts/skylos_quality.sh"
    script.write_text(FAKE_GATE_SCRIPT, encoding="utf-8")
    script.chmod(0o755)
    args = tmp_path / "gate-args"
    step = _step(_workflow()["jobs"][JOB_ID], "Skylos gate")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=repo,
        env={
            **os.environ,
            "EVENT_NAME": event,
            "PUSH_BEFORE": before,
            "RUNNER_TEMP": str(tmp_path),
            "FAKE_GATE_ARGS": str(args),
        },
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return args.read_text(encoding="utf-8").splitlines()


def test_a_pull_request_is_checked_against_the_base_it_merges_into(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "base when the PR was opened")
    _git(repo, "switch", "-q", "-c", "feature")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "the PR")
    _git(repo, "switch", "-q", "main")
    # main moves on after the PR is opened; the event's base.sha can still name the old tip.
    _git(repo, "commit", "-q", "--allow-empty", "-m", "merged to main later")
    base = _git(repo, "rev-parse", "HEAD")
    # actions/checkout checks a PR out as its merge commit: the base first, the PR second.
    _git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature into main", "feature")

    assert _gate_args(tmp_path, repo, "pull_request", "") == [base, f"{tmp_path}/skylos"]


def test_a_push_is_checked_against_the_push_range(tmp_path: Path) -> None:
    before = "4ffabb1020f53a5d6652adf8021c2ede16dda4f0"

    assert _gate_args(tmp_path, tmp_path, "push", before) == [before, f"{tmp_path}/skylos"]
