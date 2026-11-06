"""RFS §11 cases of the rules.md sub-corpus (#399): the premises the validator cannot see.

``validate_dataset.py --profile rules`` checks anchors, arms and quotes. It does not know which
scenario a case stands for, so an edit of a fixture could void the case with the validator green.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

CASES = Path(__file__).resolve().parent.parent / "test-prs-dataset" / "rules" / "cases"


def _metadata(case_id: str) -> dict[str, Any]:
    metadata: dict[str, Any] = json.loads((CASES / case_id / "case.json").read_text("utf-8"))
    return metadata


def _document(case_id: str, path: str) -> str:
    return (CASES / case_id / "base" / path).read_text("utf-8")


def test_style_only_rules_leave_the_other_categories_in_every_arm() -> None:
    assert re.findall(r"(?m)^# (.+)$", _document("RUL-09", ".review/rules.md")) == ["Style"]
    findings = _metadata("RUL-09")["expected_findings"]
    assert {item["category"] for item in findings if "arms" not in item} == {
        "security",
        "correctness",
    }
    assert {item["category"] for item in findings if item.get("origin") == "rule"} == {
        "readability"
    }
