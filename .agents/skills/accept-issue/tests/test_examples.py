"""Tests for examples/: the golden cases of acceptance.py.

Offline: no network, no tokens. Every step of every case is run against
`scripts/acceptance.py` (stdout, exit code and write log byte for byte), and checked for
internal consistency, so the goldens cannot drift apart from each other: every fixture
parses, the issue body is the same in both fixtures that carry it, `rows.json` names
exactly the AC / DoD of that body, the comment in `expected.json` is the comment in
`expected-writes.jsonl`, and the case README repeats the step's command line and starts
with the SYNC line.
"""
# SYNC: mirrored in dmc-268-{ui,api}-t6/.agents/skills/accept-issue/tests/test_examples.py

from __future__ import annotations

import importlib.util
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
EXAMPLES = SKILL / "examples"
SCRIPT = SKILL / "scripts" / "acceptance.py"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


facts = _load(SKILL / "scripts" / "issue_facts.py", "accept_examples_issue_facts")

STEPS = sorted(p for p in EXAMPLES.glob("*/step-*") if p.is_dir())
IDS = [f"{p.parent.name}/{p.name}" for p in STEPS]


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def body_of(path: Path) -> str:
    body = read_json(path)["data"]["repository"]["issue"]["body"]
    assert isinstance(body, str)
    return body


def item_ids(body: str) -> list[str]:
    boxes = facts.build_checkboxes(body)
    return [f"AC{i + 1}" for i in range(boxes["ac"]["total"])] + [
        f"DoD{i + 1}" for i in range(boxes["dod"]["total"])
    ]


def test_there_are_cases_and_steps() -> None:
    assert len(STEPS) >= 18
    assert sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) == sorted(
        {p.parent.name for p in STEPS}
    )


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_every_json_file_parses(step: Path) -> None:
    for path in step.rglob("*.json"):
        assert isinstance(read_json(path), dict), path
    log = step / "expected-writes.jsonl"
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            assert isinstance(json.loads(line), dict)


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_expected_is_stable_json(step: Path) -> None:
    text = (step / "expected.json").read_text(encoding="utf-8")
    assert text == json.dumps(json.loads(text), sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    assert (step / "args.txt").read_text(encoding="utf-8").endswith("\n")


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_the_issue_body_is_the_same_in_both_fixtures(step: Path) -> None:
    for before, after in (("issue-p1", "acceptance-p1"), ("after-issue-p1", "after-acceptance-p1")):
        first, second = step / "fixtures" / f"{before}.json", step / "fixtures" / f"{after}.json"
        if first.exists() and second.exists():
            assert body_of(first) == body_of(second), step


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_rows_json_names_exactly_the_body_items(step: Path) -> None:
    rows = step / "rows.json"
    if not rows.exists():
        pytest.skip("no rows.json in this step")
    spec = read_json(rows)
    ids = item_ids(body_of(step / "fixtures" / "acceptance-p1.json"))
    # a step may be a golden for a row mismatch; then the expected output is a stop
    if read_json(step / "expected.json")["verdict"] != "stop":
        assert sorted(spec) == sorted(ids)
    for row in spec.values():
        assert row["status"] in ("done", "not_done", "moved")
        assert (row["status"] == "done") == bool(row.get("evidence"))
        assert (row["status"] == "moved") == ("to" in row)


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_expected_comment_is_the_posted_comment(step: Path) -> None:
    expected = read_json(step / "expected.json")
    log = step / "expected-writes.jsonl"
    writes = (
        [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        if log.exists()
        else []
    )
    posted = [w["body"] for w in writes if w["op"] == "comment"]
    if expected["apply"] and expected["verdict"] != "stop" and expected["comment"] is not None:
        assert posted == [expected["comment"]]
    else:
        assert posted == []
    if expected["verdict"] == "stop" or not expected["apply"]:
        assert writes == []


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_the_readme_repeats_the_command_and_starts_with_the_sync_line(step: Path) -> None:
    case = step.parent
    readme = (case / "README.md").read_text(encoding="utf-8")
    assert readme.startswith(
        f"<!-- SYNC: mirrored in dmc-268-{{ui,api}}-t6/.agents/skills/accept-issue/"
        f"examples/{case.name}/README.md -->\n\n# "
    )
    args = (step / "args.txt").read_text(encoding="utf-8").strip()
    assert f"acceptance.py {args}" in readme


# ─── running the goldens ────────────────────────────────────────────────────
def run_step(step: Path, tmp_path: Path) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run a step offline from its own directory; (process, the write log's text)."""
    guard = tmp_path / "guard-bin"
    guard.mkdir()
    gh = guard / "gh"
    gh.write_text(
        f"#!{sys.executable}\nimport sys\n"
        "sys.stderr.write('unexpected gh call\\n')\nsys.exit(97)\n",
        encoding="utf-8",
    )
    gh.chmod(0o755)
    log = tmp_path / "writes.jsonl"
    env = dict(os.environ)
    env["PATH"] = f"{guard}{os.pathsep}{env.get('PATH', '')}"
    env["ACCEPT_ISSUE_FIXTURE_DIR"] = str(step / "fixtures")
    env["ACCEPT_ISSUE_WRITE_LOG"] = str(log)
    args = shlex.split((step / "args.txt").read_text(encoding="utf-8"))
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True,
        encoding="utf-8",
        env=env,
        cwd=step,
        check=False,
    )
    return proc, log.read_text(encoding="utf-8") if log.exists() else ""


@pytest.mark.parametrize("step", STEPS, ids=IDS)
def test_step_matches_its_golden(step: Path, tmp_path: Path) -> None:
    proc, writes = run_step(step, tmp_path)
    expected = (step / "expected.json").read_text(encoding="utf-8")
    assert proc.stdout == expected
    assert proc.stderr == ""
    assert proc.returncode == (1 if json.loads(expected)["verdict"] == "stop" else 0)
    golden = step / "expected-writes.jsonl"
    assert writes == (golden.read_text(encoding="utf-8") if golden.exists() else "")
