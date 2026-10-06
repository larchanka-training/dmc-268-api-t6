"""Manual live-run workflow contracts, executed without provider or ECB network."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "llm-live-run.yml"


def test_live_run_does_not_inherit_obsolete_repository_eur_rate() -> None:
    job = WORKFLOW.read_text(encoding="utf-8").split("  live-run:\n", 1)[1]
    job_env = re.search(r"(?m)^    env:\n((?:^      [A-Z_]+: .*\n)+)", job)
    assert job_env is not None
    assert "LLM_EUR_TO_USD_RATE" not in job

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
    assert all(line.count("|") == 10 for line in summary.splitlines() if line.startswith("|"))
