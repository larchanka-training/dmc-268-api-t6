"""Immutable legacy-v3 prompt contracts, before gateway normalization (#178)."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

import pytest

from app.modules.reviews.infrastructure.llm.answers import parse_review_answer
from review.scripts.validate_findings import validate_review_output

REPO_ROOT = Path(__file__).resolve().parents[1]
PROMPTS = REPO_ROOT / "review/prompts"
V3_PATH = PROMPTS / "review.system.v3.md"
V3_GOLDEN = REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.v3.txt"
V2_SHA256 = "9177e358c23d1a7cd8987cbf667cf8b705c2afb27eaf8b2237480e4531431d75"


def test_v3_asset_has_exact_full_golden_and_preserves_v2() -> None:
    content = V3_PATH.read_bytes()
    assert content.startswith(b"---\nkey: review.system\nversion: 3\n")
    assert content == V3_GOLDEN.read_bytes()
    v2 = (PROMPTS / "review.system.v2.md").read_bytes()
    assert sha256(v2).hexdigest() == V2_SHA256
    assert (REPO_ROOT / "tests/fixtures/prompt_builder/sample.system.txt").read_bytes() == v2


def _examples() -> list[tuple[str, dict[str, Any]]]:
    examples = V3_PATH.read_text(encoding="utf-8").split("\n## 11. Examples\n", 1)[1]
    inputs = re.findall(r"```xml\n(.*?)\n```", examples, re.DOTALL)
    answers = re.findall(r"```json\n(.*?)\n```", examples, re.DOTALL)
    assert len(inputs) == len(answers) == 4
    return [(xml, json.loads(answer)) for xml, answer in zip(inputs, answers, strict=True)]


def test_every_complete_v3_answer_is_strictly_valid_without_gateway_normalization() -> None:
    answers = re.findall(r"```json\n(.*?)\n```", V3_PATH.read_text(), re.DOTALL)
    assert len(answers) == 5  # Output shape and four worked answers.
    for answer in answers:
        data = json.loads(answer)
        assert validate_review_output(data) == []
        assert parse_review_answer(answer).model_dump() == data


def test_v3_worked_examples_show_the_literal_attribution_and_anchor_contracts() -> None:
    examples = _examples()
    assert examples[0][1]["findings"][0]["start_line"] == 17
    assert examples[0][1]["findings"][0]["line"] == 18
    assert examples[1][1]["findings"][0]["start_line"] is None
    assert examples[1][1]["findings"][0]["rule_name"] is None
    assert examples[2][1]["findings"] == []
    findings = examples[3][1]["findings"]
    assert [(item["severity"], item["confidence"]) for item in findings] == [
        ("critical", 0.95),
        ("high", 0.98),
        ("high", 0.90),
    ]
    assert findings[1]["rule_name"] == "External Call Failures"
    assert findings[1]["body"].startswith(
        "According to custom instructions in 'External Call Failures' "
        "(surface failed external calls): "
    )
    assert findings[1]["start_line"] is None
    assert findings[0]["rule_name"] is None
    assert findings[2]["rule_name"] is None
    assert not findings[0]["body"].startswith("According to custom instructions")
    assert not findings[2]["body"].startswith("According to custom instructions")
    envelope = ElementTree.fromstring(f"<envelope>{examples[3][0]}</envelope>")
    rule = envelope.find("./custom_instructions/rule")
    assert rule is not None
    assert rule.attrib == {
        "name": "External Call Failures",
        "include": "src/services/archive/**/*.ts",
        "exclude": "**/*.test.ts",
    }
    assert findings[1]["path"] == "src/services/archive/send.ts"
    assert rule.text is not None and "surface failed external calls" in rule.text


@pytest.mark.parametrize(
    ("defect", "expected"),
    [
        (
            "missing_prefix",
            "findings[1].body: rule_name is set, body must start with "
            "\"According to custom instructions in 'External Call Failures' (\"",
        ),
        (
            "wrong_prefix",
            "findings[1].body: rule_name is set, body must start with "
            "\"According to custom instructions in 'External Call Failures' (\"",
        ),
        ("equal_range", "findings[2].start_line: must be < line"),
        ("reversed_range", "findings[2].start_line: must be < line"),
        ("severity_order", "findings: must be ordered by severity, then by confidence descending"),
        (
            "confidence_order",
            "findings: must be ordered by severity, then by confidence descending",
        ),
    ],
)
def test_strict_v3_contract_rejects_bad_variants_before_normalization(
    defect: str, expected: str
) -> None:
    data = deepcopy(_examples()[3][1])
    findings = data["findings"]
    if defect == "missing_prefix":
        findings[1]["body"] = "The failed external call is silently ignored."
    elif defect == "wrong_prefix":
        findings[1]["body"] = (
            "According to custom instructions in 'Different Rule' (surface failures): "
            "The failed external call is silently ignored."
        )
    elif defect == "equal_range":
        findings[2]["start_line"] = 6
    elif defect == "reversed_range":
        findings[2]["start_line"] = 7
    elif defect == "severity_order":
        findings[0], findings[1] = findings[1], findings[0]
    elif defect == "confidence_order":
        findings[1], findings[2] = findings[2], findings[1]
    assert validate_review_output(data) == [expected]


def test_v3_example_anchors_and_suggestions_match_shown_files_without_corpus_reuse() -> None:
    sources = [REPO_ROOT / "review/examples/sample.diff"]
    sources += [path for path in (REPO_ROOT / "test-prs-dataset").rglob("*") if path.is_file()]
    corpus = "\n".join(path.read_text(encoding="utf-8", errors="ignore") for path in sources)
    for xml, answer in _examples():
        envelope = ElementTree.fromstring(f"<envelope>{xml}</envelope>")
        files = {file.attrib["path"]: file for file in envelope.findall("./changed_files/file")}
        for path in files:
            assert path not in corpus
        for finding in answer["findings"]:
            lines = {
                int(line.attrib["n"])
                for line in files[finding["path"]].findall("line")
                if line.attrib["type"] in {"added", "context"}
            }
            assert finding["line"] in lines
            assert finding["start_line"] is None or finding["start_line"] in lines
            suggestion = finding["suggestion"]
            if suggestion is not None:
                assert not re.search(r"^(\+|-|@@)", suggestion, re.MULTILINE)
                assert "```" not in suggestion
                assert len(suggestion.splitlines()) == (
                    finding["line"] - (finding["start_line"] or finding["line"]) + 1
                )
