"""Exercise manual eval shell steps without credentials or provider calls."""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github/workflows/eval-live.yml"


def _step(name: str) -> str:
    text = WORKFLOW.read_text()
    block = text.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
    match = re.search(r"(?m)^        run: \|\n(?P<body>(?:^          .*\n|^\n)*)", block)
    assert match is not None
    return textwrap.dedent(match.group("body"))


def _run(name: str, root: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-euo", "pipefail", "-c", _step(name)],
        cwd=root,
        env={**os.environ, **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_prepare_live_corpus_preserves_committed_responses(tmp_path: Path) -> None:
    dataset = tmp_path / "test-prs-dataset"
    for directory in ("cases/SEC-01", "schema", "responses"):
        (dataset / directory).mkdir(parents=True)
    (dataset / "cases/SEC-01/diff.patch").write_bytes(b"patch\n")
    (dataset / "schema/case.schema.json").write_text("{}")
    response = dataset / "responses/SEC-01.json"
    response.write_bytes(b"original raw answer")
    (dataset / "responses/manifest.json").write_text("{}")
    runner = tmp_path / "runner"
    runner.mkdir()

    result = _run("Prepare isolated corpus", tmp_path, {"RUNNER_TEMP": str(runner)})

    assert result.returncode == 0, result.stderr
    assert response.read_bytes() == b"original raw answer"
    assert (runner / "eval-corpus/cases/SEC-01/diff.patch").read_bytes() == b"patch\n"
    assert (runner / "eval-corpus/schema/case.schema.json").read_text() == "{}"
    assert not (runner / "eval-corpus/responses").exists()


def test_live_failure_is_not_hidden_and_diagnostics_remain(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$@" > "$RUNNER_TEMP/args"\n'
        'printf "{}" > "$RUNNER_TEMP/eval-report.json"\n'
        "exit 27\n"
    )
    shim.chmod(0o755)
    result = _run(
        "Evaluate live corpus",
        tmp_path,
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "RUNNER_TEMP": str(tmp_path),
            "LLM_API_KEYS": "test-key-not-a-real-credential",
        },
    )

    assert result.returncode == 27
    assert (tmp_path / "eval-report.json").read_text() == "{}"
    assert (tmp_path / "args").read_text().splitlines() == [
        "run",
        "python",
        "review/scripts/eval_live.py",
        "--root",
        str(tmp_path / "eval-corpus"),
        "--report-json",
        str(tmp_path / "eval-report.json"),
        "--redacted-responses",
        str(tmp_path / "eval-response-metadata"),
    ]
    assert "test-key-not-a-real-credential" not in result.stdout + result.stderr


def test_missing_key_fails_before_provider_call(tmp_path: Path) -> None:
    result = _run(
        "Evaluate live corpus",
        tmp_path,
        {"RUNNER_TEMP": str(tmp_path), "LLM_API_KEYS": ""},
    )
    assert result.returncode != 0
    assert "AI_DMC268_T6" in result.stderr
    assert not (tmp_path / "eval-report.json").exists()


def test_summary_uses_replay_metrics_without_model_diagnostics(tmp_path: Path) -> None:
    import json
    import sys

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "uv"
    shim.write_text('#!/bin/bash\nexec "$PYTHON_EXE" -\n')
    shim.chmod(0o755)
    report = {
        "case_count": 24,
        "valid_response_count": 16,
        "validity": 16 / 24,
        "provenance": {
            "model_id": "model<test>",
            "prompt_path": "review/prompts/review.system.v2.md",
            "prompt_version": "v2",
            "prompt_sha": "a" * 64,
            "static_digest": "sha256-v1:" + "b" * 64,
            "corpus_digest": "sha256-v1:" + "c" * 64,
        },
        "micro": {"tp": 3, "fp": 5, "fn": 16, "precision": 0.375, "recall": 3 / 19},
        "critical": {"matched": 2, "total": 5, "recall": 0.4},
        "per_category": {"security": {"tp": 1, "fp": 0, "fn": 3, "precision": 1.0, "recall": 0.25}},
        "verdict": {"agreed": 7, "total": 24, "agreement": 7 / 24},
        "severity_mismatches": [],
        "warnings": ["Recorded capture is not publishable"],
        "responses": [{"validator_output": "RAW MODEL TEXT MUST NOT APPEAR"}],
    }
    (tmp_path / "eval-report.json").write_text(json.dumps(report))
    summary = tmp_path / "summary.md"
    result = _run(
        "Summarize evaluation",
        tmp_path,
        {
            "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
            "PYTHON_EXE": sys.executable,
            "PYTHONPATH": str(REPO_ROOT),
            "RUNNER_TEMP": str(tmp_path),
            "GITHUB_STEP_SUMMARY": str(summary),
        },
    )
    assert result.returncode == 0, result.stderr
    content = summary.read_text()
    assert "Model: model&lt;test&gt;" in content
    assert "Validity: 66.7% (16/24)" in content
    assert "Precision=37.5% Recall=15.8%" in content
    assert "security: TP=1 FP=0 FN=3 Precision=100.0% Recall=25.0%" in content
    assert "Verdict agreement: 29.2% (7/24)" in content
    assert "Warning: Recorded capture is not publishable" in content
    assert "RAW MODEL TEXT MUST NOT APPEAR" not in content
