"""RFS §11 cases of the rules.md sub-corpus (#399): the premises the validator cannot see.

``validate_dataset.py --profile rules`` checks anchors, arms and quotes. It does not know which
scenario a case stands for, so an edit of a fixture could void the case with the validator green.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any

import pytest

from tests.test_eval_live import VALID, FakeTransport, answer, record_arm, rules_fixture_root

ROOT = Path(__file__).resolve().parent.parent
CASES = ROOT / "test-prs-dataset" / "rules" / "cases"
CATALOG = ROOT / "docs" / "test-plan" / "rules-md-scenarios-163.md"


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


def test_level_four_neighbours_reach_the_prompt_and_no_document_allows_them(
    tmp_path: Path,
) -> None:
    transport = FakeTransport([answer("primary-model", VALID)])
    record_arm(rules_fixture_root(tmp_path, ("RUL-10",)), transport, "none")
    (request,) = transport.requests
    prompt = html.unescape(request.messages[1].content)

    patch = (CASES / "RUL-10" / "diff.patch").read_text("utf-8").splitlines()
    neighbours = [line for line in patch if line.startswith(" ") and "conn.fetch(f" in line]
    assert len(neighbours) == 3
    for neighbour in neighbours:
        assert neighbour.strip() in prompt

    metadata = _metadata("RUL-10")
    assert {
        (item["path"], item["category"])
        for item in metadata["expected_findings"]
        if "arms" not in item
    } == {("app/reports/queries.py", "security"), ("app/reports/queries.py", "correctness")}
    (anchor,) = metadata["must_not_flag"]
    assert anchor["path"].startswith("scripts/backfill/")
    assert "app/" not in _document("RUL-10", ".review/rules.md")
    assert not re.search(r"SQL|f-string", _document("RUL-10", "AGENTS.md"))


def test_rules_exception_contradicts_agents_and_the_rest_stays() -> None:
    agents = _document("RUL-11", "AGENTS.md")
    assert "test setup shared by two tests goes into a factory" in agents
    assert "Report a duplicated block as a readability problem" in agents

    metadata = _metadata("RUL-11")
    (anchor,) = metadata["must_not_flag"]
    assert "/__tests__/" in anchor["path"]
    findings = metadata["expected_findings"]
    assert [
        (item["path"], item["line"], item["category"])
        for item in findings
        if item.get("arms") == ["none"]
    ] == [(anchor["path"], anchor["line"], "readability")]
    (outside_tests,) = [item for item in findings if "arms" not in item]
    assert "/__tests__/" not in outside_tests["path"]
    assert outside_tests["category"] == "readability"
    (other_rule,) = [item for item in findings if item.get("origin") == "rule"]
    assert other_rule["path"] == anchor["path"]
    assert other_rule["category"] == "correctness"


@pytest.mark.parametrize(
    ("row", "case_id"),
    [
        ("Rules имеет только Style", "RUL-09"),
        ("Конвенция уровня 4 допускает небезопасный паттерн", "RUL-10"),
        ("Rules разрешает дублирование test setup", "RUL-11"),
    ],
)
def test_catalog_row_names_its_case(row: str, case_id: str) -> None:
    lines = CATALOG.read_text("utf-8").splitlines()
    (found,) = [line for line in lines if line.startswith(f"| {row}")]
    assert case_id in found
    assert "не покрыто" not in found
