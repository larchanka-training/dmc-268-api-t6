"""The PR size label job of ci-cd.yml (#378): a ``size/*`` label and, from 1000 lines, a warning.

The steps run as written in the workflow: ``Measure PR size`` on a depth-2 clone of a real PR
merge commit, ``Set size label`` against a fake ``gh`` on ``PATH``. The job only informs.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml  # type: ignore[import-untyped]

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"
JOB_ID = "pr-size"
# Required checks of ruleset `main` on 03.11.2026
# (gh api repos/larchanka-training/dmc-268-api-t6/rules/branches/main): never the size job.
REQUIRED_CHECKS = {
    "OpenAPI lint",
    "Python lint / type / test",
    "Webhook container smoke",
    "Edge Caddyfile validate",
}
SLICING_RULE = (
    "https://github.com/larchanka-training/dmc-268-api-t6/blob/main"
    "/.agents/skills/create-issue/references/slicing.md"
)


def _workflow() -> dict[str, Any]:
    workflow: dict[str, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return workflow


def _job() -> dict[str, Any]:
    job: dict[str, Any] = _workflow()["jobs"][JOB_ID]
    return job


def _step(name: str) -> dict[str, Any]:
    steps = [step for step in _job()["steps"] if step.get("name") == name]
    assert len(steps) == 1, f"step {name!r} must exist exactly once"
    step: dict[str, Any] = steps[0]
    return step


def _git_env() -> dict[str, str]:
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


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=_git_env(), check=True, capture_output=True)


def _write(repo: Path, path: str, lines: int, seed: str | None = None) -> None:
    """``lines`` lines unique to the seed, so that distinct files never pair as a rename."""
    (repo / path).parent.mkdir(parents=True, exist_ok=True)
    text = "".join(f"{seed or path} line {index:06d}\n" for index in range(lines))
    (repo / path).write_text(text, encoding="utf-8")


def _measure(
    tmp_path: Path, base: dict[str, int], change: Callable[[Path], None]
) -> tuple[str, str, str]:
    """Merge a PR branch with ``--no-ff`` as GitHub does, clone with depth 2, run the step.

    Returns the label written to ``GITHUB_OUTPUT``, stdout and the job summary.
    """
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "--initial-branch=main")
    for path, lines in {**base, "README.md": 1, "main_only.txt": 1}.items():
        _write(origin, path, lines)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "base")
    _git(origin, "checkout", "-qb", "pr")
    change(origin)
    _git(origin, "add", "-A")
    _git(origin, "commit", "-qm", "pr")
    _git(origin, "checkout", "-q", "main")
    # The base moves on after the branch point: none of it may count as the PR's size.
    _write(origin, "main_only.txt", 5000)
    _git(origin, "commit", "-qam", "main moves on")
    _git(origin, "merge", "-q", "--no-ff", "-m", "merge", "pr")
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", "--depth", "2", origin.as_uri(), str(clone))

    output = tmp_path / "output"
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-c", _step("Measure PR size")["run"]],
        cwd=clone,
        env={
            **_git_env(),
            "PR_SIZE_EXCLUDE": _job()["env"]["PR_SIZE_EXCLUDE"],
            "GITHUB_OUTPUT": str(output),
            "GITHUB_STEP_SUMMARY": str(summary),
            "GITHUB_SERVER_URL": "https://github.com",
            "GITHUB_REPOSITORY": "larchanka-training/dmc-268-api-t6",
        },
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 0, result.stderr
    outputs = output.read_text(encoding="utf-8").splitlines()
    labels = [line.removeprefix("label=") for line in outputs if line.startswith("label=")]
    assert len(labels) == 1, outputs
    return labels[0], result.stdout, summary.read_text(encoding="utf-8")


def test_job_only_informs_on_pull_requests() -> None:
    workflow = _workflow()
    job = _job()
    # The job name is the check name: it must stay out of the required checks of ruleset `main`.
    assert job["name"] == "PR size label"
    assert job["name"] not in REQUIRED_CHECKS
    assert job["if"] == "github.event_name == 'pull_request'"
    assert job["timeout-minutes"] == 5
    assert "needs" not in job
    for other_id, other in workflow["jobs"].items():
        needs = other.get("needs", [])
        assert JOB_ID not in ([needs] if isinstance(needs, str) else needs), other_id
    # Write access for this job only; job-level permissions replace the root block.
    assert workflow["permissions"] == {"contents": "read"}
    assert job["permissions"] == {"contents": "read", "pull-requests": "write"}
    checkout = _step("Checkout")
    assert checkout["uses"] == "actions/checkout@v4"
    assert checkout["with"] == {"fetch-depth": 2, "persist-credentials": False}
    # Lock and generated files are listed in one place.
    assert _job()["env"]["PR_SIZE_EXCLUDE"].split() == [
        "uv.lock",
        "terraform/**/.terraform.lock.hcl",
        ".test_durations",
        "docs/erd/erd.svg",
    ]
    for step in job["steps"]:
        assert "${{" not in step.get("run", ""), step["name"]


@pytest.mark.parametrize(
    ("lines", "label"),
    [
        (0, "size/S"),
        (299, "size/S"),
        (300, "size/M"),
        (999, "size/M"),
        (1000, "size/L"),
        (2999, "size/L"),
        (3000, "size/XL"),
    ],
)
def test_label_by_added_lines(tmp_path: Path, lines: int, label: str) -> None:
    def change(repo: Path) -> None:
        _write(repo, "app/new.py", lines)

    measured, _, summary = _measure(tmp_path, {}, change)

    assert measured == label
    assert summary.startswith(f"PR size: {lines} changed lines (`{label}`)")


def test_deleted_lines_count_too(tmp_path: Path) -> None:
    def change(repo: Path) -> None:
        _git(repo, "rm", "-q", "old.txt")
        _write(repo, "new.txt", 100)

    label, _, summary = _measure(tmp_path, {"old.txt": 200}, change)

    assert label == "size/M"
    assert summary.startswith("PR size: 300 changed lines")


def test_lock_and_generated_files_do_not_count(tmp_path: Path) -> None:
    base = {
        "uv.lock": 10,
        "terraform/api-staging/.terraform.lock.hcl": 10,
        ".test_durations": 10,
        "docs/erd/erd.svg": 10,
    }

    def change(repo: Path) -> None:
        _write(repo, "uv.lock", 5000, seed="lock v2")
        _write(repo, "terraform/api-staging/.terraform.lock.hcl", 400, seed="hcl v2")
        _write(repo, "terraform/ui-staging/.terraform.lock.hcl", 400)
        _write(repo, ".test_durations", 400, seed="durations v2")
        _write(repo, "docs/erd/erd.svg", 400, seed="svg v2")

    label, stdout, summary = _measure(tmp_path, base, change)

    assert label == "size/S"
    assert summary.startswith("PR size: 0 changed lines")
    assert "::warning" not in stdout


def test_a_nested_uv_lock_and_a_binary_file(tmp_path: Path) -> None:
    def change(repo: Path) -> None:
        # Only the root uv.lock is excluded; a binary file has no lines and counts 0.
        _write(repo, "tools/uv.lock", 7)
        (repo / "logo.png").write_bytes(bytes(range(256)) * 64)

    label, _, summary = _measure(tmp_path, {}, change)

    assert label == "size/S"
    assert summary.startswith("PR size: 7 changed lines")


def test_a_renamed_file_counts_only_its_changed_lines(tmp_path: Path) -> None:
    def change(repo: Path) -> None:
        _git(repo, "mv", "app/old_name.py", "app/new_name.py")
        lines = (repo / "app/new_name.py").read_text(encoding="utf-8").splitlines(keepends=True)
        lines[0] = "edited\n"
        (repo / "app/new_name.py").write_text("".join(lines), encoding="utf-8")

    label, _, summary = _measure(tmp_path, {"app/old_name.py": 2000}, change)

    assert label == "size/S"
    assert summary.startswith("PR size: 2 changed lines")


@pytest.mark.parametrize(("lines", "label"), [(1000, "size/L"), (3000, "size/XL")])
def test_l_and_xl_warn_with_the_slicing_rule(tmp_path: Path, lines: int, label: str) -> None:
    def change(repo: Path) -> None:
        _write(repo, "app/new.py", lines)

    _, stdout, summary = _measure(tmp_path, {}, change)

    assert stdout.count("::warning") == 1
    assert f"::warning::PR size {lines} lines ({label})" in stdout
    assert f"]({SLICING_RULE})" in summary
    assert "explain in the PR body why" in summary


def test_s_and_m_write_one_summary_line(tmp_path: Path) -> None:
    def change(repo: Path) -> None:
        _write(repo, "app/new.py", 999)

    _, stdout, summary = _measure(tmp_path, {}, change)

    assert "::warning" not in stdout
    assert summary == (
        "PR size: 999 changed lines (`size/M`), lock and generated files excluded.\n"
    )


FAKE_GH = """#!/usr/bin/env bash
# Logs "<METHOD> <path> [<field>]"; fails the method named in FAKE_GH_FAIL; GET answers
# FAKE_GH_LABELS through the caller's --jq filter.
method=GET path= field= jq_filter=
while (( $# )); do
  case "$1" in
    api) ;;
    -X) method="$2"; shift ;;
    -f) field="$2"; shift ;;
    --jq) jq_filter="$2"; shift ;;
    *) path="$1" ;;
  esac
  shift
done
echo "${method} ${path} ${field}" >> "${FAKE_GH_LOG}"
if [[ "${method}" == "${FAKE_GH_FAIL:-}" ]]; then
  echo "HTTP 403: Resource not accessible by integration" >&2
  exit 1
fi
if [[ "${method}" == GET ]]; then
  jq -r "${jq_filter}" <<< "${FAKE_GH_LABELS}"
fi
"""


def _set_label(
    tmp_path: Path, current: list[str], label: str, fail: str = ""
) -> tuple[list[str], str]:
    """Run the step with a fake ``gh``; returns the logged API calls and stdout."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    log = tmp_path / "gh.log"
    log.touch()
    step = _step("Set size label")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "REPO": "o/r",
            "PR_NUMBER": "7",
            "LABEL": label,
            "FAKE_GH_LOG": str(log),
            "FAKE_GH_FAIL": fail,
            "FAKE_GH_LABELS": json.dumps([{"name": name} for name in current]),
        },
        capture_output=True,
        encoding="utf-8",
        check=False,
    )
    # A label API error never fails the job.
    assert result.returncode == 0, result.stderr
    return log.read_text(encoding="utf-8").splitlines(), result.stdout


def test_set_size_label_step_wiring() -> None:
    step = _step("Set size label")
    assert step["continue-on-error"] is True
    assert step["env"] == {
        "GH_TOKEN": "${{ github.token }}",
        "REPO": "${{ github.repository }}",
        "PR_NUMBER": "${{ github.event.pull_request.number }}",
        "LABEL": "${{ steps.size.outputs.label }}",
    }
    assert _step("Measure PR size")["id"] == "size"


def test_swaps_the_size_label_and_keeps_other_labels(tmp_path: Path) -> None:
    calls, stdout = _set_label(tmp_path, ["ai-review", "size/M", "size/XL", "sizeable"], "size/L")

    assert calls == [
        "GET repos/o/r/issues/7/labels?per_page=100 ",
        "DELETE repos/o/r/issues/7/labels/size%2FM ",
        "DELETE repos/o/r/issues/7/labels/size%2FXL ",
        "POST repos/o/r/issues/7/labels labels[]=size/L",
    ]
    assert "::warning" not in stdout


def test_a_label_already_set_makes_no_write(tmp_path: Path) -> None:
    calls, _ = _set_label(tmp_path, ["size/S", "bug"], "size/S")

    assert calls == ["GET repos/o/r/issues/7/labels?per_page=100 "]


def test_a_pr_without_labels_gets_one(tmp_path: Path) -> None:
    calls, _ = _set_label(tmp_path, [], "size/S")

    assert calls == [
        "GET repos/o/r/issues/7/labels?per_page=100 ",
        "POST repos/o/r/issues/7/labels labels[]=size/S",
    ]


@pytest.mark.parametrize(
    ("fail", "warning", "writes"),
    [
        ("GET", "::warning::Could not read the labels of PR #7; size/L not set", 0),
        ("DELETE", "::warning::Could not remove size/M from PR #7", 2),
        ("POST", "::warning::Could not add size/L to PR #7", 2),
    ],
)
def test_an_api_error_is_a_warning(tmp_path: Path, fail: str, warning: str, writes: int) -> None:
    calls, stdout = _set_label(tmp_path, ["size/M"], "size/L", fail=fail)

    assert warning in stdout
    assert len(calls) == 1 + writes
