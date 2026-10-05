"""The manual live-run job passes currency configuration to both gateway calls."""

from __future__ import annotations

import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "llm-live-run.yml"


def test_live_run_inherits_repository_eur_rate_for_both_gateway_calls() -> None:
    job = WORKFLOW.read_text(encoding="utf-8").split("  live-run:\n", 1)[1]
    job_env = re.search(r"(?m)^    env:\n((?:^      [A-Z_]+: .*\n)+)", job)
    assert job_env is not None
    assert "      LLM_EUR_TO_USD_RATE: ${{ vars.LLM_EUR_TO_USD_RATE }}\n" in job_env.group(1)

    for name in ("Primary with fallback", "Fallback as the primary, without a fallback"):
        step = job.split(f"      - name: {name}\n", 1)[1].split("\n      - name:", 1)[0]
        assert "uv run python -m app.bootstrap.llm_gateway" in step
        assert "LLM_EUR_TO_USD_RATE:" not in step  # job env is inherited without a shadowing value
