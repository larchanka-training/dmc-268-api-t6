"""The required CI job must publish the complete offline replay report."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

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
    assert workflow.index("    name: Python lint / type / test") < match.start()
    return textwrap.dedent(match.group("body"))


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
    tmp_path: Path, *, manifest: bool, fail_replay: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/bash\n"
        "set -e\n"
        'case "$3" in\n'
        '  review/scripts/validate_dataset.py) cp "$FIXTURE_CORPUS" "${@: -1}" ;;\n'
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
