"""The offline RAG build of the eval asks the same gate as the review whether retrieved blocks
hide their comments (#498): a recording on v7 sends no comment of unchanged code, one on v5
sends them as the captures recorded them."""

from __future__ import annotations

import asyncio
from pathlib import Path

from review.scripts.eval_rag_context import build_arm_input

# RAG-21 retrieves its unchanged caller, whose lines 11-13 are a comment addressed to
# automated reviewers (test-prs-dataset/rag/cases/RAG-21/README.md, "The bait").
_RAG_21 = Path(__file__).resolve().parents[1] / "test-prs-dataset" / "rag" / "cases" / "RAG-21"
_CALLER = "fleetlog/planner.py"
_BAIT_LINES = (11, 12, 13)


def _caller_lines(version: str) -> dict[int, str]:
    arm = asyncio.run(build_arm_input(_RAG_21, "B", review_model="m", version=version))
    (block,) = (item for item in arm.context.retrieved if item.source.path == _CALLER)
    numbers = [n for part in block.source.ranges for n in range(part.start_line, part.end_line + 1)]
    return dict(zip(numbers, block.text.split("\n"), strict=True))


def test_v7_sends_markers_in_place_of_the_bait() -> None:
    lines = _caller_lines("v7")

    assert [lines[n] for n in _BAIT_LINES] == ["    # [comment removed]"] * 3


def test_the_recorded_v5_prompt_still_sends_the_comments() -> None:
    lines = _caller_lines("v5")

    assert all(lines[n].startswith("    # ") for n in _BAIT_LINES)
    assert not any("[comment removed]" in line for line in lines.values())
