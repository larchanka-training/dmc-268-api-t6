"""The offline RAG build of the eval asks the same gate as the review whether retrieved blocks
hide their comments (#498): a recording on v7 sends no comment of unchanged code, one on v5
sends them as the captures recorded them."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.modules.reviews.application import rag_index
from review.scripts.eval_rag_context import build_arm_input

# RAG-21 retrieves its unchanged caller, whose lines 11-13 are a comment addressed to
# automated reviewers (test-prs-dataset/rag/cases/RAG-21/README.md, "The bait").
_CASES = Path(__file__).resolve().parents[1] / "test-prs-dataset" / "rag" / "cases"
_RAG_21 = _CASES / "RAG-21"
# RAG-25 puts its bait in the docstring of its unchanged caller: v7 hides comments, not strings.
_RAG_25 = _CASES / "RAG-25"
_CALLER = "fleetlog/planner.py"
_BAIT_LINES = (11, 12, 13)


def _block_lines(case: Path, path: str, version: str) -> dict[int, str]:
    arm = asyncio.run(build_arm_input(case, "B", review_model="m", version=version))
    (block,) = (item for item in arm.context.retrieved if item.source.path == path)
    numbers = [n for part in block.source.ranges for n in range(part.start_line, part.end_line + 1)]
    return dict(zip(numbers, block.text.split("\n"), strict=True))


def _caller_lines(version: str) -> dict[int, str]:
    return _block_lines(_RAG_21, _CALLER, version)


def test_v7_sends_markers_in_place_of_the_bait() -> None:
    lines = _caller_lines("v7")

    assert [lines[n] for n in _BAIT_LINES] == ["    # [comment removed]"] * 3


def test_v7_still_sends_the_docstring_bait_of_rag_25() -> None:
    record = json.loads((_RAG_25 / "case.json").read_text(encoding="utf-8"))
    bait = record["rag"]["injection"]
    lines = _block_lines(_RAG_25, bait["bait_path"], "v7")

    assert rag_index.hides_retrieved_comments("review.system", "v7")
    assert any(bait["bait_text"] in line for line in lines.values())
    assert not any("[comment removed]" in line for line in lines.values())


def test_the_recorded_v5_prompt_still_sends_the_comments() -> None:
    lines = _caller_lines("v5")

    assert all(lines[n].startswith("    # ") for n in _BAIT_LINES)
    assert not any("[comment removed]" in line for line in lines.values())


def test_the_build_asks_the_review_gate_not_the_version(monkeypatch: pytest.MonkeyPatch) -> None:
    # With the gate's set swapped to v5, v5 hides the bait and v7 sends it: the build follows
    # the set, as the review does, and no version check of its own.
    monkeypatch.setattr(
        rag_index, "COMMENT_FREE_PROMPT_VERSIONS", frozenset({("review.system", "v5")})
    )

    assert [_caller_lines("v5")[n] for n in _BAIT_LINES] == ["    # [comment removed]"] * 3
    assert not any("[comment removed]" in line for line in _caller_lines("v7").values())
