"""Parity of the ReviewOutput JSON Schema, the Pydantic model and validate_findings.py (#20)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from app.modules.reviews.application.review_output import (
    Category,
    Effort,
    ReviewFinding,
    ReviewOutput,
    ReviewSummary,
    Severity,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "review" / "schemas" / "review-output.schema.json"
VALIDATOR_SCRIPT = REPO_ROOT / "review" / "scripts" / "validate_findings.py"
SAMPLE_PATH = REPO_ROOT / "review" / "examples" / "findings.sample.json"
CORPUS_DIR = Path(__file__).parent / "fixtures" / "review_output"

SCHEMA: dict[str, Any] = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
SCHEMA_VALIDATOR = Draft202012Validator(SCHEMA)

VALID_FILES = sorted((CORPUS_DIR / "valid").glob("*.json"))
INVALID_FILES = sorted((CORPUS_DIR / "invalid").glob("*.json"))

# Verdict triples, in order: JSON Schema, Pydantic ReviewOutput, validate_findings.py.
ACCEPTED_BY_ALL = (True, True, True)
REJECTED_BY_ALL = (False, False, False)

# Rules JSON Schema cannot express (see the schema's $comment): the schema accepts,
# the Pydantic model and the script reject.
SEMANTIC_ONLY = {
    "start-line-equals-line.json",
    "title-trailing-period.json",
    "title-two-lines.json",
    "wrong-order.json",
    "problem-two-sentences.json",
    "done-well-no-sentence.json",
    "done-well-three-sentences.json",
}

# The body prefix <-> rule_name binding is enforced by the script only: at runtime the
# post-processor repairs it rather than rejecting the answer (review/postprocess/lint-filter.md).
SCRIPT_ONLY = {"rule-name-without-prefix.json"}

# Known difference (brief gate trap 3): JSON Schema's `integer` admits 1.0 for `line` and
# `start_line`, the strict Pydantic model does not. The script's semantic layer keeps the strict
# check, so only the schema accepts.
KNOWN_DIFFERENCES = {"line-float.json", "start-line-float.json"}

# Without a `findings` key the script cannot tell the output kind and exits 2 instead of 1.
UNKNOWN_KIND = {"missing-findings.json"}


def _expected(path: Path) -> tuple[bool, bool, bool]:
    if path.parent.name == "valid":
        return ACCEPTED_BY_ALL
    if path.name in SEMANTIC_ONLY:
        return (True, False, False)
    if path.name in SCRIPT_ONLY:
        return (True, True, False)
    if path.name in KNOWN_DIFFERENCES:
        return (True, False, False)
    return REJECTED_BY_ALL


def _model_accepts(raw: str) -> bool:
    try:
        ReviewOutput.model_validate_json(raw)
    except ValidationError:
        return False
    return True


def _script_accepts(path: Path) -> bool:
    result = subprocess.run(
        [sys.executable, str(VALIDATOR_SCRIPT), str(path)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    allowed_codes = {2} if path.name in UNKNOWN_KIND else {0, 1}
    assert result.returncode in allowed_codes, result.stdout + result.stderr
    return result.returncode == 0


def test_corpus_declares_only_existing_cases() -> None:
    invalid_names = {path.name for path in INVALID_FILES}
    declared = SEMANTIC_ONLY | SCRIPT_ONLY | KNOWN_DIFFERENCES | UNKNOWN_KIND

    assert VALID_FILES
    assert declared <= invalid_names


@pytest.mark.parametrize(
    "path", VALID_FILES + INVALID_FILES, ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_schema_model_and_script_verdicts(path: Path) -> None:
    raw = path.read_text(encoding="utf-8")

    verdicts = (
        SCHEMA_VALIDATOR.is_valid(json.loads(raw)),
        _model_accepts(raw),
        _script_accepts(path),
    )

    assert verdicts == _expected(path)


def test_schema_is_a_valid_draft_2020_12_schema() -> None:
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(SCHEMA)


def test_schema_keys_equal_the_model_fields() -> None:
    finding = SCHEMA["$defs"]["ReviewFinding"]
    summary = SCHEMA["$defs"]["ReviewSummary"]

    for node, model in (
        (SCHEMA, ReviewOutput),
        (finding, ReviewFinding),
        (summary, ReviewSummary),
    ):
        assert set(node["properties"]) == set(model.model_fields)
        assert set(node["required"]) == set(model.model_fields)
        assert node["additionalProperties"] is False


def test_schema_enums_equal_the_model_literals() -> None:
    finding = SCHEMA["$defs"]["ReviewFinding"]["properties"]
    summary = SCHEMA["$defs"]["ReviewSummary"]["properties"]

    assert finding["severity"]["enum"] == list(get_args(Severity.__value__))
    assert finding["category"]["enum"] == list(get_args(Category.__value__))
    assert summary["effort"]["enum"] == list(get_args(Effort.__value__))


def test_schema_accepts_the_findings_sample() -> None:
    sample = json.loads(SAMPLE_PATH.read_text(encoding="utf-8"))

    assert list(SCHEMA_VALIDATOR.iter_errors(sample)) == []
