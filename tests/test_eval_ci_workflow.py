"""The Python CI jobs: the unit job publishes the complete offline replay report, and the
required check is red unless the unit job and every integration shard passed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from collections.abc import Callable
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/ci-cd.yml"


def _replay_step() -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    match = re.search(
        r"(?m)^      - name: Gold corpus and recorded replay\n"
        r"        shell: bash\n        run: \|\n"
        r"(?P<body>(?:^          .*\n|^\n)*)",
        workflow,
    )
    assert match is not None
    # The replay runs in the unit job; the required check fails when that job does.
    assert match.group(0) in _python_job()
    return textwrap.dedent(match.group("body"))


def _python_job(job_id: str = "python-unit") -> str:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    start = workflow.index(f"\n  {job_id}:\n") + 1
    next_job = re.compile(r"(?m)^  \S").search(workflow, start + 1)
    return workflow[start : next_job.start() if next_job else len(workflow)]


def _job_step(job: str, name: str) -> str:
    """The whole step mapping, keys after ``run`` included."""
    header = f"      - name: {name}\n"
    assert job.count(header) == 1, f"step {name!r} must exist exactly once"
    start = job.index(header)
    end = job.find("\n      - name: ", start + len(header))
    return job[start : end if end != -1 else len(job)]


def _report() -> dict[str, Any]:
    return {
        "case_count": 2,
        "valid_response_count": 1,
        "validity": 0.5,
        "provenance": {
            "model_id": "model-test",
            "prompt_path": "review/prompts/review.system.v2.md",
            "prompt_version": "v2",
            "prompt_sha": "a" * 64,
            "static_digest": "sha256-v1:" + "b" * 64,
            "corpus_digest": "sha256-v1:" + "c" * 64,
        },
        "micro": {"tp": 2, "fp": 1, "fn": 1, "precision": 2 / 3, "recall": 2 / 3},
        "critical": {"matched": 1, "total": 2, "recall": 0.5},
        "per_category": {
            "security": {"tp": 1, "fp": 0, "fn": 1, "precision": 1.0, "recall": 0.5},
            "performance": {"tp": 0, "fp": 0, "fn": 0, "precision": None, "recall": None},
        },
        "verdict": {"agreed": 1, "total": 2, "agreement": 0.5},
        "severity_mismatches": [{"case_id": "SEC-01"}],
        "warnings": ["Static input digest mismatch"],
    }


def _run_step(
    tmp_path: Path, *, manifest: bool, fail_replay: bool = False, fail_rules: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    # The script is the first review/scripts/*.py (or ``-``) argument, wherever uv flags put it.
    shim.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        'script=""\n'
        'for arg in "$@"; do\n'
        '  case "$arg" in review/scripts/*.py|-) script="$arg"; break ;; esac\n'
        "done\n"
        'case "$script" in\n'
        "  review/scripts/validate_dataset.py)\n"
        '    if [[ " $* " == *" --profile rules "* ]]; then\n'
        '      echo "$*" >> "$RULES_LOG"\n'
        '      if [[ "${FAIL_RULES:-}" == 1 ]]; then exit 33; fi\n'
        "    else\n"
        '      cp "$FIXTURE_CORPUS" "${@: -1}"\n'
        "    fi ;;\n"
        "  review/scripts/eval_replay.py)\n"
        "    if [[ ! -f test-prs-dataset/responses/manifest.json ]]; then exit 1; fi\n"
        '    if [[ "${FAIL_REPLAY:-}" == 1 ]]; then exit 27; fi\n'
        '    cp "$FIXTURE_REPLAY" "${@: -1}" ;;\n'
        '  -) exec "$PYTHON_EXE" - ;;\n'
        "  *) exit 41 ;;\n"
        "esac\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    corpus = tmp_path / "corpus.json"
    corpus.write_text(json.dumps({"valid_case_count": 24, "case_count": 24, "passed": True}))
    replay = tmp_path / "replay.json"
    replay.write_text(json.dumps(_report()), encoding="utf-8")
    if manifest:
        manifest_path = tmp_path / "test-prs-dataset/responses/manifest.json"
        manifest_path.parent.mkdir(parents=True)
        manifest_path.write_text("{}", encoding="utf-8")
    summary = tmp_path / "summary.md"
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHON_EXE": sys.executable,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_STEP_SUMMARY": str(summary),
        "FIXTURE_CORPUS": str(corpus),
        "FIXTURE_REPLAY": str(replay),
        "FAIL_REPLAY": "1" if fail_replay else "0",
        "FAIL_RULES": "1" if fail_rules else "0",
        "RULES_LOG": str(tmp_path / "rules-validation.log"),
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _replay_step()],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, summary.read_text(encoding="utf-8") if summary.exists() else ""


def test_required_ci_summary_uses_complete_replay_report(tmp_path: Path) -> None:
    result, summary = _run_step(tmp_path, manifest=True)

    expected = (
        "\n".join(
            [
                "## Gold benchmark",
                "",
                "Corpus: 24/24 valid; distribution passed.",
                "",
                "Recorded replay:",
                "",
                "```text",
                "Cases: 2",
                "Model: model-test",
                "Prompt: review/prompts/review.system.v2.md (version v2)",
                "Prompt SHA: " + "a" * 64,
                "Static digest: sha256-v1:" + "b" * 64,
                "Corpus digest: sha256-v1:" + "c" * 64,
                "Validity: 50.0% (1/2)",
                "Micro: TP=2 FP=1 FN=1 Precision=66.7% Recall=66.7%",
                "Critical Recall: 50.0% (1/2)",
                "Verdict agreement: 50.0% (1/2)",
                "Per category:",
                "  security: TP=1 FP=0 FN=1 Precision=100.0% Recall=50.0%",
                "  performance: TP=0 FP=0 FN=0 Precision=undefined Recall=undefined",
                "Severity mismatches: 1",
                "Warning: Static input digest mismatch",
                "```",
                "",
            ]
        )
        + "\n"
    )
    assert result.returncode == 0, result.stderr
    assert summary == expected
    assert "::warning::Static input digest mismatch" in result.stdout


def test_required_ci_validates_the_rules_corpus_with_its_profile(tmp_path: Path) -> None:
    result, _ = _run_step(tmp_path, manifest=True)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "rules-validation.log").read_text(encoding="utf-8") == (
        "run --locked python review/scripts/validate_dataset.py "
        "--root test-prs-dataset/rules --final --profile rules\n"
    )


def test_required_ci_rules_corpus_failure_fails_the_step(tmp_path: Path) -> None:
    result, summary = _run_step(tmp_path, manifest=True, fail_rules=True)

    assert result.returncode == 33
    assert summary == ""


def test_required_ci_replay_failure_fails_the_step(tmp_path: Path) -> None:
    result, summary = _run_step(tmp_path, manifest=True, fail_replay=True)

    assert result.returncode == 27
    assert "Recorded replay failed; no report was produced." in summary


def test_required_ci_without_manifest_fails_with_summary(tmp_path: Path) -> None:
    result, summary = _run_step(tmp_path, manifest=False)

    assert result.returncode != 0
    assert summary == (
        "## Gold benchmark\n\n"
        "Corpus: 24/24 valid; distribution passed.\n\n"
        "Recorded replay failed: baseline manifest is missing.\n"
    )


def test_required_ci_replay_failure_is_not_softened_by_step_or_job_keys() -> None:
    job = _python_job()
    step = _job_step(job, "Gold corpus and recorded replay")

    # A failed replay must fail the required check: a key placed anywhere in the step mapping,
    # before or after its run body, or on the job must not turn it green or skip it.
    continue_on_error = re.compile(r"(?m)^ +continue-on-error:")
    assert not continue_on_error.search(step), "replay step must not continue on error"
    assert not re.search(r"(?m)^        if:", step), "replay step must always run"


@pytest.mark.parametrize("job_id", ["python-unit", "python-integration"])
def test_python_test_jobs_are_not_softened_by_job_or_step_keys(job_id: str) -> None:
    job = _python_job(job_id)

    # Strict on purpose: any step that continues on error, or a condition that skips the job,
    # softens the required check that needs it.
    assert not re.search(r"(?m)^ +continue-on-error:", job), (
        f"neither {job_id} nor any of its steps may continue on error"
    )
    assert not re.search(r"(?m)^    if:", job), f"{job_id} must always run"


def test_required_check_fails_unless_every_python_job_succeeded() -> None:
    job = _python_job("python-lint-type-test")

    # A required check whose job is skipped counts as passed, so the aggregator runs after a
    # failed or cancelled job and is red unless every job it needs succeeded.
    assert "    name: Python lint / type / test\n" in job
    assert re.search(r"(?m)^    if: always\(\)$", job)
    assert "      - python-unit\n" in job
    assert "      - python-integration\n" in job
    assert "NEEDS: ${{ toJSON(needs) }}" in job
    assert 'if result != "success"' in job
    assert not re.search(r"(?m)^ +continue-on-error:", job)


def test_shard_count_is_pinned_across_the_python_jobs() -> None:
    integration = _python_job("python-integration")
    matrix = re.search(r"(?m)^        shard: \[([\d, ]+)\]$", integration)
    name = re.search(
        r"(?m)^    name: Python integration \(\$\{\{ matrix\.shard \}\}/(\d+)\)$", integration
    )
    splits = re.findall(r"--splits (\d+)", integration)
    assert matrix is not None and name is not None and len(splits) == 1

    shards = [int(shard) for shard in matrix.group(1).split(",")]
    assert shards == list(range(1, len(shards) + 1))
    assert int(splits[0]) == int(name.group(1)) == _expected_shards() == len(shards)


def test_shards_keep_the_durations_file_inside_the_checkout() -> None:
    step = _job_step(_python_job("python-integration"), "Integration tests")
    commands = "\n".join(line for line in step.splitlines() if not line.lstrip().startswith("#"))

    # An existing --durations-path outside the checkout moves pytest's rootdir away from
    # pyproject.toml; --strict-markers turns a lost config into a collection error.
    assert "--durations-path" not in commands
    assert "--strict-markers" in commands


def _expected_shards() -> int:
    """The shard count the aggregating job checks."""
    match = re.search(r'(?m)^          SHARDS: "(\d+)"$', _python_job("python-lint-type-test"))
    assert match is not None
    return int(match.group(1))


def _aggregator_step(name: str) -> str:
    """The bash body of a step of the aggregating job, heredoc included, as the runner gets it."""
    step = _job_step(_python_job("python-lint-type-test"), name)
    match = re.search(r"(?m)^        run: \|\n(?P<body>(?:^          .*\n|^\n)*)", step)
    assert match is not None
    return textwrap.dedent(match.group("body"))


def _run_aggregator_step(
    tmp_path: Path, name: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "python3"
    shim.write_text('#!/bin/bash\nexec "$PYTHON_EXE" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _aggregator_step(name)],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHON_EXE": sys.executable,
            **env,
        },
        capture_output=True,
        text=True,
        check=False,
    )


# Shard -> testcases as (nodeid, outcome); durations.json of a shard lists the same nodeids.
Shards = dict[int, list[tuple[str, str]]]
# Breaks a green shard set in place; returns the ::error:: text the check must print.
Mutation = Callable[[list[str], Shards], str]


def _green_shards() -> tuple[list[str], Shards]:
    count = _expected_shards()
    nodeids = [f"tests/test_shard_{n}.py::test_case[{n}]" for n in range(2 * count)]
    shards = {
        shard: [(nodeid, "passed") for nodeid in nodeids[2 * shard - 2 : 2 * shard]]
        for shard in range(1, count + 1)
    }
    return nodeids, shards


def _write_shard(directory: Path, cases: list[tuple[str, str]], durations: list[str]) -> None:
    directory.mkdir()
    suite = ElementTree.Element("testsuite", name="pytest", tests=str(len(cases)))
    for nodeid, outcome in cases:
        path, test = nodeid.split("::")
        case = ElementTree.SubElement(
            suite, "testcase", classname=path.removesuffix(".py").replace("/", "."), name=test
        )
        if outcome in ("skipped", "xfail"):
            kind = "pytest.skip" if outcome == "skipped" else "pytest.xfail"
            ElementTree.SubElement(case, "skipped", type=kind, message=outcome)
        elif outcome in ("failure", "error"):
            ElementTree.SubElement(case, outcome, message=outcome)
    root = ElementTree.Element("testsuites")
    root.append(suite)
    ElementTree.ElementTree(root).write(directory / "junit.xml", encoding="utf-8")
    (directory / "durations.json").write_text(
        json.dumps({nodeid: 0.5 for nodeid in durations}), encoding="utf-8"
    )


def _run_suite_check(
    tmp_path: Path,
    collected: list[str],
    shards: Shards,
    durations: dict[int, list[str]] | None = None,
) -> tuple[subprocess.CompletedProcess[str], Path]:
    results = tmp_path / "python-tests"
    results.mkdir()
    (results / "integration-tests.txt").write_text(
        "".join(f"{nodeid}\n" for nodeid in collected), encoding="utf-8"
    )
    for shard, cases in shards.items():
        listed = (durations or {}).get(shard, [nodeid for nodeid, _ in cases])
        _write_shard(results / f"integration-{shard}", cases, listed)
    summary = tmp_path / "summary.md"
    env = {"RESULTS": str(results), "SHARDS": str(_expected_shards())}
    env["GITHUB_STEP_SUMMARY"] = str(summary)
    return _run_aggregator_step(tmp_path, "Integration suite ran once (zero skips)", env), results


def test_suite_check_merges_the_durations_of_green_shards(tmp_path: Path) -> None:
    nodeids, shards = _green_shards()
    shards[1][0] = (shards[1][0][0], "xfail")

    result, results = _run_suite_check(tmp_path, nodeids, shards)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout
    merged = (results / ".test_durations").read_text(encoding="utf-8")
    assert merged.endswith("}\n")
    assert sorted(json.loads(merged)) == sorted(nodeids)
    assert f"{len(nodeids)} collected, {len(nodeids)} ran, 0 errors" in (
        tmp_path / "summary.md"
    ).read_text(encoding="utf-8")


def _lose_last(collected: list[str], shards: Shards) -> str:
    nodeid, _ = shards[max(shards)].pop()
    return f"Integration test did not run: {nodeid}"


def _not_collected(collected: list[str], shards: Shards) -> str:
    nodeid = collected.pop(0)
    return f"Integration test ran but was not collected: {nodeid}"


def _duplicate(collected: list[str], shards: Shards) -> str:
    shards[2].append(shards[1][0])
    return f"Integration test ran in 2 shards: {collected[0]}"


def _outcome(shard: int, outcome: str, error: str) -> Mutation:
    def mutate(collected: list[str], shards: Shards) -> str:
        nodeid, _ = shards[shard][0]
        shards[shard][0] = (nodeid, outcome)
        path, test = nodeid.split("::")
        return f"Shard {shard}: integration test {error}: {path[:-3].replace('/', '.')}::{test}"

    return mutate


def _missing_shard(collected: list[str], shards: Shards) -> str:
    last = max(shards)
    del shards[last]
    return f"Shard {last} uploaded no durations.json and junit.xml"


def _empty_collection(collected: list[str], shards: Shards) -> str:
    collected.clear()
    return "No integration tests were collected"


@pytest.mark.parametrize(
    "mutate",
    [
        _lose_last,
        _not_collected,
        _duplicate,
        _outcome(1, "skipped", "skipped"),
        _outcome(2, "failure", "failed"),
        _outcome(2, "error", "failed"),
        _missing_shard,
        _empty_collection,
    ],
    ids=["lost", "not-collected", "duplicate", "skipped", "failure", "error", "no-shard", "empty"],
)
def test_suite_check_fails_on_a_broken_shard_set(tmp_path: Path, mutate: Mutation) -> None:
    nodeids, shards = _green_shards()
    expected = mutate(nodeids, shards)

    result, results = _run_suite_check(tmp_path, nodeids, shards)

    assert result.returncode != 0
    assert f"::error::{expected}\n" in result.stdout
    assert not (results / ".test_durations").exists()


def test_suite_check_fails_when_junit_and_durations_disagree(tmp_path: Path) -> None:
    nodeids, shards = _green_shards()
    durations = {2: [nodeid for nodeid, _ in shards[2]]}
    shards[2].pop()

    result, _ = _run_suite_check(tmp_path, nodeids, shards, durations)

    assert result.returncode != 0
    assert "::error::Shard 2: junit.xml lists 1 tests, durations.json 2\n" in result.stdout


def _run_needs_check(tmp_path: Path, results: dict[str, str]) -> subprocess.CompletedProcess[str]:
    needs = {job: {"result": result, "outputs": {}} for job, result in results.items()}
    return _run_aggregator_step(
        tmp_path, "Require the Python jobs to succeed", {"NEEDS": json.dumps(needs)}
    )


def test_needs_check_passes_when_every_python_job_succeeded(tmp_path: Path) -> None:
    result = _run_needs_check(tmp_path, {"python-unit": "success", "python-integration": "success"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "::error::" not in result.stdout


@pytest.mark.parametrize("outcome", ["failure", "cancelled", "skipped"])
@pytest.mark.parametrize("job", ["python-unit", "python-integration"])
def test_needs_check_fails_unless_every_python_job_succeeded(
    tmp_path: Path, job: str, outcome: str
) -> None:
    results = {"python-unit": "success", "python-integration": "success", job: outcome}

    result = _run_needs_check(tmp_path, results)

    assert result.returncode != 0
    assert f"::error::Job {job} finished with result '{outcome}', not 'success'\n" in result.stdout
