"""The authored graph-RAG corpus of #146 (``test-prs-dataset/rag``) meets its ADR 0001 §12 shape.

The per-case integrity checks run inside the validator's ``--final --profile rag``; this file
pins the corpus-wide numbers so a case dropped or re-kinded fails a unit test, not only CI.
The CI step "RAG sub-corpus" runs the same two checks; the duplication is deliberate, a fast
local signal before a push.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from review.scripts import build_rag_snapshot, validate_dataset

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "test-prs-dataset" / "rag"
CASES = CORPUS / "cases"


def _records() -> list[dict[str, Any]]:
    return [
        json.loads((case / "case.json").read_text(encoding="utf-8"))
        for case in sorted(CASES.iterdir())
        if case.is_dir()
    ]


def test_the_corpus_has_the_adr_distribution() -> None:
    records = _records()

    assert len(records) == 25
    assert Counter(record["rag"]["kind"] for record in records) == {
        "reverse_edge": 10,
        "control": 6,
        "depth2": 4,
        "injection": 3,
        "clean": 2,
    }
    assert Counter(record["language"] for record in records) == {"python": 16, "typescript": 9}


def test_the_corpus_has_a_critical_truth_and_three_export_defects() -> None:
    records = _records()
    severities = Counter(
        finding["severity"] for record in records for finding in record["expected_findings"]
    )
    exports = [
        record["id"]
        for record in records
        if record["rag"].get("defect_form") in validate_dataset.RAG_EXPORT_FORMS
    ]

    assert severities["critical"] >= 1
    assert exports == ["RAG-05", "RAG-09", "RAG-10"]


def test_every_index_is_current(capsys: pytest.CaptureFixture[str]) -> None:
    assert build_rag_snapshot.main(["--check", str(CASES)]) == 0, capsys.readouterr().err


def test_the_validator_accepts_the_corpus_in_final_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    argv = ["validate_dataset.py", "--root", str(CORPUS), "--final", "--profile", "rag"]
    monkeypatch.setattr(sys, "argv", argv)

    assert validate_dataset.main() == 0, capsys.readouterr().out
    assert "cases: 25; valid: 25" in capsys.readouterr().out
