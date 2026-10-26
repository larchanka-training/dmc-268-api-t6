"""Tests for review-output.schema.v2.json and verifier-output.schema.v1.json (Issue #120)."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from pydantic import BaseModel, ValidationError

from app.modules.reviews.application.reflection_pass import (
    VerificationDecision,
    VerificationOutput,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
    ReviewOutputV2,
)

SCHEMAS_DIR = Path(__file__).resolve().parents[1] / "review" / "schemas"
V2_SCHEMA_PATH = SCHEMAS_DIR / "review-output.schema.v2.json"
VERIFIER_SCHEMA_PATH = SCHEMAS_DIR / "verifier-output.schema.v1.json"


def test_v2_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_verifier_schema_is_valid_draft_2020_12() -> None:
    schema = json.loads(VERIFIER_SCHEMA_PATH.read_text(encoding="utf-8"))
    assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    Draft202012Validator.check_schema(schema)


def test_v2_schema_properties_match_pydantic_models() -> None:
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    finding_def = schema["$defs"]["ReviewFindingV2"]
    evidence_def = schema["$defs"]["EvidenceRef"]

    finding_required = {name for name, f in ReviewFindingV2.model_fields.items() if f.is_required()}
    evidence_required = {name for name, f in EvidenceRef.model_fields.items() if f.is_required()}

    assert set(finding_def["properties"]) == set(ReviewFindingV2.model_fields)
    assert set(finding_def["required"]) == finding_required

    assert set(evidence_def["properties"]) == set(EvidenceRef.model_fields)
    assert set(evidence_def["required"]) == evidence_required


def test_verifier_schema_properties_match_pydantic_models() -> None:
    schema = json.loads(VERIFIER_SCHEMA_PATH.read_text(encoding="utf-8"))

    # Root VerificationOutput
    output_required = {
        name for name, f in VerificationOutput.model_fields.items() if f.is_required()
    }
    assert set(schema["properties"]) == set(VerificationOutput.model_fields)
    assert set(schema["required"]) == output_required

    # VerificationDecision
    decision_def = schema["$defs"]["VerificationDecision"]
    decision_required = {
        name for name, f in VerificationDecision.model_fields.items() if f.is_required()
    }
    assert set(decision_def["properties"]) == set(VerificationDecision.model_fields)
    assert set(decision_def["required"]) == decision_required

    # RequirementRef
    req_def = schema["$defs"]["RequirementRef"]
    req_required = {name for name, f in RequirementRef.model_fields.items() if f.is_required()}
    assert set(req_def["properties"]) == set(RequirementRef.model_fields)
    assert set(req_def["required"]) == req_required

    # EvidenceRef
    evidence_def = schema["$defs"]["EvidenceRef"]
    evidence_required = {name for name, f in EvidenceRef.model_fields.items() if f.is_required()}
    assert set(evidence_def["properties"]) == set(EvidenceRef.model_fields)
    assert set(evidence_def["required"]) == evidence_required


def test_v2_schema_accepts_valid_payload() -> None:
    schema = json.loads(V2_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    payload = {
        "findings": [
            {
                "path": "app/service.py",
                "line": 15,
                "start_line": 12,
                "severity": "high",
                "category": "security",
                "title": "SQL injection risk",
                "body": "User input concatenated into query string",
                "suggestion": "query(param)",
                "confidence": 0.9,
                "rule_name": ".review/rules.md",
                "evidence_refs": [{"block_id": "diff-code-1", "start_line": 12, "end_line": 15}],
            }
        ],
        "summary": {
            "problem": "SQL query uses raw string formatting.",
            "done_well": "Clean layering.",
            "effort": "small",
        },
    }
    assert list(validator.iter_errors(payload)) == []


def test_verifier_schema_accepts_valid_payload() -> None:
    schema = json.loads(VERIFIER_SCHEMA_PATH.read_text(encoding="utf-8"))
    validator = Draft202012Validator(schema)

    payload = {
        "decisions": [
            {
                "candidate_id": "7d033785-0bf9-4d63-8db3-31d7a947290e",
                "status": "supported",
                "reason": "The code violates the input sanitization requirement in rules.md.",
                "evidence_refs": [{"block_id": "verify-code-1", "start_line": 12, "end_line": 15}],
                "requirement_refs": [
                    {
                        "block_id": "verify-policy-rules",
                        "start_line": 8,
                        "end_line": 10,
                        "check_id": None,
                    }
                ],
            }
        ]
    }
    assert list(validator.iter_errors(payload)) == []

    # check_id with string is valid
    payload_str_check = json.loads(json.dumps(payload))
    payload_str_check["decisions"][0]["requirement_refs"][0]["check_id"] = "check-1"
    assert list(validator.iter_errors(payload_str_check)) == []

    # check_id cannot be empty string (minLength: 1)
    payload_empty_check = json.loads(json.dumps(payload))
    payload_empty_check["decisions"][0]["requirement_refs"][0]["check_id"] = ""
    errors_empty = list(validator.iter_errors(payload_empty_check))
    assert len(errors_empty) >= 1
    assert any(e.validator == "minLength" for e in errors_empty)

    # check_id is required: omitting check_id is rejected
    payload_missing_check = json.loads(json.dumps(payload))
    del payload_missing_check["decisions"][0]["requirement_refs"][0]["check_id"]
    errors_missing = list(validator.iter_errors(payload_missing_check))
    assert len(errors_missing) >= 1
    assert any("check_id" in e.message for e in errors_missing)


def _review_contract_payload() -> dict[str, Any]:
    """Literal wire example; expected limits are specified by the public contract."""
    return {
        "findings": [
            {
                "path": "app/service.py",
                "line": 15,
                "start_line": 12,
                "severity": "high",
                "category": "security",
                "title": "SQL injection risk",
                "body": "User input reaches a raw query",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": "service-defaults:E2",
                "evidence_refs": [{"block_id": "diff-code", "start_line": 12, "end_line": 15}],
            }
        ],
        "summary": {
            "problem": "SQL input is unsafe.",
            "done_well": "Clean layering.",
            "effort": "small",
        },
    }


def _verifier_contract_payload() -> dict[str, Any]:
    return {
        "decisions": [
            {
                "candidate_id": "candidate-1",
                "status": "insufficient_context",
                "reason": "The implementation is not visible",
                "evidence_refs": [{"block_id": "verify-code", "start_line": 12, "end_line": 15}],
                "requirement_refs": [
                    {"block_id": "verify-policy", "start_line": 8, "end_line": 10, "check_id": "E2"}
                ],
            }
        ]
    }


def _assert_contract_acceptance(contract: str, payload: dict[str, Any], *, accepted: bool) -> None:
    schema_path = V2_SCHEMA_PATH if contract == "review" else VERIFIER_SCHEMA_PATH
    validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
    model = ReviewOutputV2 if contract == "review" else VerificationOutput
    errors = list(validator.iter_errors(payload))
    assert (not errors) is accepted, [error.message for error in errors]
    if accepted:
        model.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            model.model_validate(payload)


@pytest.mark.parametrize(
    ("rule_name", "accepted"),
    [
        (None, True),
        (".review/rules.md", True),
        ("AGENTS.md", True),
        ("service-defaults:E2", True),
        ("service-defaults:FSD1", True),
        ("service-defaults:Future_ID-42", True),
        ("Security", False),
        ("service-defaults:", False),
        ("service-defaults:E2\n", False),
        ("AGENTS.md\n", False),
        (".review/rules.md\n", False),
        ("service-defaults:E2/other", False),
        ("prefixservice-defaults:E2", False),
    ],
)
def test_rule_name_schema_and_model_share_lexical_domain(
    rule_name: str | None, accepted: bool
) -> None:
    payload = _review_contract_payload()
    payload["findings"][0]["rule_name"] = rule_name
    _assert_contract_acceptance("review", payload, accepted=accepted)


# Each location and limit is literal contract data, independent of model_fields/schema lookup.
@pytest.mark.parametrize(
    ("contract", "location", "boundary"),
    [
        ("review", ("findings", 0, "path"), "p" * 1024),
        ("review", ("findings", 0, "title"), "t" * 80),
        ("review", ("findings", 0, "body"), "b" * 1200),
        ("review", ("findings", 0, "rule_name"), "service-defaults:" + "R" * 111),
        ("review", ("findings", 0, "evidence_refs", 0, "block_id"), "b" * 128),
        ("verifier", ("decisions", 0, "candidate_id"), "c" * 64),
        ("verifier", ("decisions", 0, "reason"), "r" * 600),
        ("verifier", ("decisions", 0, "evidence_refs", 0, "block_id"), "b" * 128),
        ("verifier", ("decisions", 0, "requirement_refs", 0, "block_id"), "b" * 128),
        ("verifier", ("decisions", 0, "requirement_refs", 0, "check_id"), "c" * 128),
    ],
    ids=[
        "review-path",
        "review-title",
        "review-body",
        "review-rule-name",
        "review-evidence-block",
        "verifier-candidate-id",
        "verifier-reason",
        "verifier-evidence-block",
        "verifier-requirement-block",
        "verifier-check-id",
    ],
)
@pytest.mark.parametrize("beyond_limit", [False, True], ids=["at-limit", "over-limit"])
def test_schema_and_model_string_limits(
    contract: str, location: tuple[str | int, ...], boundary: str, beyond_limit: bool
) -> None:
    payload = _review_contract_payload() if contract == "review" else _verifier_contract_payload()
    parent: Any = payload
    for key in location[:-1]:
        parent = parent[key]
    parent[location[-1]] = boundary + ("x" if beyond_limit else "")
    _assert_contract_acceptance(contract, payload, accepted=not beyond_limit)


@pytest.mark.parametrize(
    ("contract", "location"),
    [
        ("review", ()),
        ("review", ("findings", 0)),
        ("review", ("findings", 0, "evidence_refs", 0)),
        ("review", ("summary",)),
        ("verifier", ()),
        ("verifier", ("decisions", 0)),
        ("verifier", ("decisions", 0, "evidence_refs", 0)),
        ("verifier", ("decisions", 0, "requirement_refs", 0)),
    ],
    ids=[
        "review-root",
        "review-finding",
        "review-evidence",
        "review-summary",
        "verifier-root",
        "verifier-decision",
        "verifier-evidence",
        "verifier-requirement",
    ],
)
def test_schema_and_model_close_every_object(
    contract: str, location: tuple[str | int, ...]
) -> None:
    payload = _review_contract_payload() if contract == "review" else _verifier_contract_payload()
    _assert_contract_acceptance(contract, payload, accepted=True)
    target: Any = payload
    for key in location:
        target = target[key]
    target["unexpected"] = "must not be silently accepted"
    _assert_contract_acceptance(contract, payload, accepted=False)


@pytest.mark.parametrize(
    ("contract", "location", "count", "accepted"),
    [
        ("review", ("findings",), 0, True),
        ("review", ("findings",), 10, True),
        ("review", ("findings",), 11, False),
        ("review", ("findings", 0, "evidence_refs"), 0, False),
        ("review", ("findings", 0, "evidence_refs"), 1, True),
        ("review", ("findings", 0, "evidence_refs"), 8, True),
        ("review", ("findings", 0, "evidence_refs"), 9, False),
        ("verifier", ("decisions",), 0, False),
        ("verifier", ("decisions",), 1, True),
        ("verifier", ("decisions",), 10, True),
        ("verifier", ("decisions",), 11, False),
        ("verifier", ("decisions", 0, "evidence_refs"), 0, True),
        ("verifier", ("decisions", 0, "evidence_refs"), 1, True),
        ("verifier", ("decisions", 0, "evidence_refs"), 8, True),
        ("verifier", ("decisions", 0, "evidence_refs"), 9, False),
        ("verifier", ("decisions", 0, "requirement_refs"), 0, True),
        ("verifier", ("decisions", 0, "requirement_refs"), 1, True),
        ("verifier", ("decisions", 0, "requirement_refs"), 8, True),
        ("verifier", ("decisions", 0, "requirement_refs"), 9, False),
    ],
)
def test_schema_and_model_array_bounds(
    contract: str, location: tuple[str | int, ...], count: int, accepted: bool
) -> None:
    payload = _review_contract_payload() if contract == "review" else _verifier_contract_payload()
    parent: Any = payload
    for key in location[:-1]:
        parent = parent[key]
    element = parent[location[-1]][0]
    parent[location[-1]] = [deepcopy(element) for _ in range(count)]
    _assert_contract_acceptance(contract, payload, accepted=accepted)


def test_schema_leaves_cross_field_semantics_to_runtime_models() -> None:
    # JSON Schema bounds do not encode coordinate ordering or status-dependent evidence.
    review = _review_contract_payload()
    review["findings"][0]["evidence_refs"][0]["start_line"] = 16
    verifier = _verifier_contract_payload()
    verifier["decisions"][0]["status"] = "supported"
    verifier["decisions"][0]["evidence_refs"] = []
    examples: list[tuple[Path, type[BaseModel], dict[str, Any]]] = [
        (V2_SCHEMA_PATH, ReviewOutputV2, review),
        (VERIFIER_SCHEMA_PATH, VerificationOutput, verifier),
    ]
    for schema_path, model, payload in examples:
        validator = Draft202012Validator(json.loads(schema_path.read_text(encoding="utf-8")))
        assert list(validator.iter_errors(payload)) == []
        with pytest.raises(ValidationError):
            model.model_validate(payload)
