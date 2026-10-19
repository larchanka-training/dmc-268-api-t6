"""Tests for review-output.schema.v2.json and verifier-output.schema.v1.json (Issue #120)."""

from __future__ import annotations

import json
from pathlib import Path

from jsonschema import Draft202012Validator

from app.modules.reviews.application.reflection_pass import (
    VerificationDecision,
    VerificationOutput,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    RequirementRef,
    ReviewFindingV2,
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
