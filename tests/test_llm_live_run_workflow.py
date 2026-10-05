"""Manual live-run workflow contracts, executed without provider or ECB network."""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "llm-live-run.yml"
REVIEW_SAMPLE = (REPO_ROOT / "review/examples/findings.sample.json").read_text()
CONVENTIONS_SAMPLE = json.loads((REPO_ROOT / "review/examples/conventions.sample.json").read_text())


def test_live_run_job_does_not_inherit_obsolete_rate_or_fallback_model() -> None:
    job = WORKFLOW.read_text(encoding="utf-8").split("  live-run:\n", 1)[1]
    job_env = re.search(r"(?m)^    env:\n((?:^      [^\n]+\n)+)", job)
    assert job_env is not None
    assert "LLM_EUR_TO_USD_RATE" not in job
    assert not re.search(r"(?m)^      LLM_FALLBACK_MODEL\s*:", job_env.group(1))

    for name in ("Primary with fallback", "Fallback as the primary, without a fallback"):
        step = job.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
        assert "uv run python -m app.bootstrap.llm_gateway" in step


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the GitHub runner")
def test_step_summary_shows_each_call_fx_on_success_and_paid_failure(tmp_path: Path) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split("      - name: Validate and summarize\n", 1)[1].split(
        "\n      - name: Upload results", 1
    )[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    fx_stale = {
        "source": "EXR.D.USD.EUR.SP00.A",
        "observation_date": "2026-10-05",
        "rate_usd_per_eur": "1.1204",
        "stale_cache": True,
    }
    fx_fresh = {**fx_stale, "stale_cache": False}
    primary = {
        "output": {"findings": []},
        "provider": "eurouter|spoofed\nprovider row",
        "model": (
            "mistral|spoofed\n[Open logs](https://attacker.example) "
            "![image](https://attacker.example/p.png)\nmodel row"
        ),
        "calls": [
            {"kind": "primary", "call_no": 1, "fx": fx_stale},
            {"kind": "repair", "call_no": 2},
        ],
        "tokens_in": 100,
        "tokens_out": 20,
        "cache_read_tokens": 0,
        "cost_usd": "0.150000",
        "latency_ms": 200,
    }
    fallback = {
        "error_code": "llm_invalid_output",
        "calls": [{"kind": "primary", "call_no": 1, "fx": fx_fresh}],
        "tokens_in": 100,
        "tokens_out": 20,
        "cache_read_tokens": 0,
        "cost_usd": "0.012000",
    }
    (tmp_path / "primary.json").write_text(json.dumps(primary), encoding="utf-8")
    (tmp_path / "fallback.json").write_text(json.dumps(fallback), encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv_stub = bin_dir / "uv"
    uv_stub.write_text("#!/bin/sh\nprintf 'valid|check\\n'\n", encoding="utf-8")
    uv_stub.chmod(0o755)
    summary_path = tmp_path / "summary.md"

    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={
            "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
            "GITHUB_STEP_SUMMARY": str(summary_path),
        },
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 1, result.stderr  # paid failure and repair violate D7
    summary = summary_path.read_text(encoding="utf-8")
    assert "FX quote per call" in summary
    assert "primary#1: EXR.D.USD.EUR.SP00.A 2026-10-05 1.1204 USD/EUR (stale cache)" in summary
    assert "repair#2: —" in summary
    assert "primary#1: EXR.D.USD.EUR.SP00.A 2026-10-05 1.1204 USD/EUR (fresh)" in summary
    assert "failed&#58; llm&#95;invalid&#95;output" in summary
    assert "eurouter&#124;spoofed<br>provider row" in summary
    assert "mistral&#124;spoofed<br>" in summary
    assert "&#91;Open logs&#93;&#40;https&#58;//attacker.example&#41;" in summary
    assert "&#33;&#91;image&#93;&#40;https&#58;//attacker.example/p.png&#41;" in summary
    assert "[Open logs](https://attacker.example)" not in summary
    assert "![image](https://attacker.example/p.png)" not in summary
    assert "valid&#124;check" in summary
    assert (
        len(
            [
                line
                for line in summary.splitlines()
                if line.startswith(("| primary ", "| fallback "))
            ]
        )
        == 2
    )
    assert all(line.count("|") == 11 for line in summary.splitlines() if line.startswith("|"))


def test_manual_workflow_runs_both_schemas_for_both_models() -> None:
    workflow = WORKFLOW.read_text()

    assert workflow.count("uv run python -m app.bootstrap.llm_gateway") == 4
    assert workflow.count('--task conventions "$DIFF"') == 2
    assert "for run in primary fallback primary-conventions fallback-conventions" in workflow
    assert "uv run python review/scripts/validate_findings.py" in workflow
    assert "| D7 |" in workflow


@pytest.mark.parametrize(
    ("step_name", "expected_assignment", "expected_command"),
    [
        (
            "Primary with fallback",
            'LLM_MODEL="$MODEL" LLM_FALLBACK_MODEL="$FALLBACK_MODEL" \\',
            'uv run python -m app.bootstrap.llm_gateway "$DIFF" > primary.json',
        ),
        (
            "Fallback as the primary, without a fallback",
            'LLM_MODEL="$FALLBACK_MODEL" \\',
            'uv run python -m app.bootstrap.llm_gateway "$DIFF" > fallback.json',
        ),
        (
            "Primary conventions with fallback",
            'LLM_MODEL="$MODEL" LLM_FALLBACK_MODEL="$FALLBACK_MODEL" \\',
            "uv run python -m app.bootstrap.llm_gateway --task conventions "
            '"$DIFF" > primary-conventions.json',
        ),
        (
            "Fallback conventions as the primary",
            'LLM_MODEL="$FALLBACK_MODEL" \\',
            "uv run python -m app.bootstrap.llm_gateway --task conventions "
            '"$DIFF" > fallback-conventions.json',
        ),
    ],
)
def test_manual_workflow_assigns_exact_models_for_each_run(
    step_name: str, expected_assignment: str, expected_command: str
) -> None:
    workflow = WORKFLOW.read_text(encoding="utf-8")
    step = workflow.split(f"      - name: {step_name}\n", 1)[1].split("\n      - name:", 1)[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    assignments = [
        line.strip() for line in script.splitlines() if line.strip().startswith("LLM_MODEL=")
    ]
    commands = [
        line.strip()
        for line in script.splitlines()
        if "uv run python -m app.bootstrap.llm_gateway" in line
    ]

    assert assignments == [expected_assignment]
    assert commands == [expected_command]
    if step_name in (
        "Fallback as the primary, without a fallback",
        "Fallback conventions as the primary",
    ):
        assert "LLM_FALLBACK_MODEL" not in step


def _run_workflow_summary(
    tmp_path: Path, *, invalid_conventions: bool, repair_conventions: bool = False
) -> tuple[subprocess.CompletedProcess[str], str]:
    workflow = WORKFLOW.read_text()
    step = workflow.split("      - name: Validate and summarize\n", 1)[1].split(
        "\n      - name: Upload results", 1
    )[0]
    script = textwrap.dedent(step.split("        run: |\n", 1)[1])
    validator = REPO_ROOT / "review" / "scripts" / "validate_findings.py"
    script = script.replace(
        "uv run python review/scripts/validate_findings.py",
        f"{shlex.quote(sys.executable)} {shlex.quote(str(validator))}",
    )
    review = json.loads(REVIEW_SAMPLE)
    conventions = {**CONVENTIONS_SAMPLE, "files": []} if invalid_conventions else CONVENTIONS_SAMPLE
    for name in ("primary", "fallback", "primary-conventions", "fallback-conventions"):
        calls: list[dict[str, object]] = [
            {
                "kind": "primary",
                "call_no": 1,
                "fx": {
                    "source": "EXR.D.USD.EUR.SP00.A",
                    "observation_date": "2026-10-05",
                    "rate_usd_per_eur": "1.20",
                    "stale_cache": False,
                },
            }
        ]
        payload = {
            "provider": "self-hosted",
            "model": "test-model",
            "calls": calls,
            "tokens_in": 500,
            "tokens_out": 300,
            "cache_read_tokens": 0,
            "cost_usd": "0.000000",
            "latency_ms": 1,
            "output": conventions if "conventions" in name else review,
        }
        if repair_conventions and "conventions" in name:
            calls.append({"kind": "repair", "call_no": 2})
        (tmp_path / f"{name}.json").write_text(json.dumps(payload))
    summary_path = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary_path)},
        capture_output=True,
        text=True,
        check=False,
    )
    return result, summary_path.read_text()


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the GitHub runner")
def test_manual_workflow_d7_confirms_valid_single_primary_answers(tmp_path: Path) -> None:
    result, summary = _run_workflow_summary(tmp_path, invalid_conventions=False)

    assert result.returncode == 0, result.stderr
    assert summary.count("OK ReviewOutput") == 2
    assert summary.count("OK RepoConventionsDraft") == 2
    assert summary.count("confirmed&#58; primary") == 4
    assert summary.count("EXR.D.USD.EUR.SP00.A") == 4
    assert "| FX quote per call |" in summary


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the GitHub runner")
def test_manual_workflow_d7_rejects_invalid_conventions_with_one_primary_call(
    tmp_path: Path,
) -> None:
    result, summary = _run_workflow_summary(tmp_path, invalid_conventions=True)

    assert result.returncode == 1, result.stderr
    for name in ("primary-conventions", "fallback-conventions"):
        row = next(line for line in summary.splitlines() if line.startswith(f"| {name} |"))
        assert "must have at least 1 item" in row
        assert "| not confirmed&#58;" in row
        assert "confirmed&#58; primary" not in row


@pytest.mark.skipif(shutil.which("jq") is None, reason="jq is required by the GitHub runner")
def test_manual_workflow_d7_rejects_valid_conventions_after_repair(tmp_path: Path) -> None:
    result, summary = _run_workflow_summary(
        tmp_path, invalid_conventions=False, repair_conventions=True
    )

    assert result.returncode == 1, result.stderr
    for name in ("primary-conventions", "fallback-conventions"):
        row = next(line for line in summary.splitlines() if line.startswith(f"| {name} |"))
        assert "OK RepoConventionsDraft" in row
        assert "not confirmed&#58;" in row
        assert "repair" in row
        assert "confirmed&#58; primary" not in row
