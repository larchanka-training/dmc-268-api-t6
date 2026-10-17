"""Unit tests for ReviewOutput v2 and Attribution Validation (Issue #120)."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.context_provenance import SourceRole
from app.modules.reviews.application.policy_snapshot import (
    CallManifest,
    ManifestBlock,
)
from app.modules.reviews.application.review_output import (
    EvidenceRef,
    InvalidReviewOutput,
    RequirementRef,
    ReviewFindingV2,
    ReviewOutputV2,
    ReviewSummary,
    parse_review_output,
    parse_review_output_v2,
    validate_v2_attribution,
)


def make_valid_finding_v2(
    *,
    rule_name: str | None = ".review/rules.md",
    evidence_refs: list[EvidenceRef] | None = None,
    path: str = "app/main.py",
    line: int = 15,
    start_line: int | None = 10,
) -> ReviewFindingV2:
    if evidence_refs is None:
        evidence_refs = [EvidenceRef(block_id="file-diff-1", start_line=10, end_line=15)]
    return ReviewFindingV2(
        path=path,
        line=line,
        start_line=start_line,
        severity="high",
        category="security",
        title="SQL injection risk",
        body="Raw query concatenation without parametrization",
        suggestion="Use parameterized query",
        confidence=0.9,
        rule_name=rule_name,
        evidence_refs=evidence_refs,
    )


def test_evidence_ref_validates_range() -> None:
    ref = EvidenceRef(block_id="block-1", start_line=5, end_line=10)
    assert ref.block_id == "block-1"
    assert ref.start_line == 5
    assert ref.end_line == 10

    # Single-line range start == end is valid
    single = EvidenceRef(block_id="block-1", start_line=5, end_line=5)
    assert single.start_line == single.end_line

    # start_line > end_line is invalid
    with pytest.raises(ValidationError):
        EvidenceRef(block_id="block-1", start_line=11, end_line=10)


def test_requirement_ref_validates_range_and_check_id() -> None:
    ref = RequirementRef(
        block_id="policy-rules",
        start_line=1,
        end_line=5,
        check_id=None,
    )
    assert ref.block_id == "policy-rules"
    assert ref.check_id is None

    # Service defaults check_id
    svc_ref = RequirementRef(
        block_id="service-defaults",
        start_line=10,
        end_line=12,
        check_id="B4.1",
    )
    assert svc_ref.check_id == "B4.1"

    with pytest.raises(ValidationError):
        RequirementRef(block_id="b", start_line=10, end_line=5)


def test_review_finding_v2_validates_rule_name_values() -> None:
    # Allowed rule_name values
    make_valid_finding_v2(rule_name=".review/rules.md")
    make_valid_finding_v2(rule_name="AGENTS.md")
    make_valid_finding_v2(rule_name="service-defaults:sql-injection")
    make_valid_finding_v2(rule_name=None)

    # Disallowed rule_name values
    with pytest.raises(ValidationError):
        make_valid_finding_v2(rule_name="custom-random-rule")

    with pytest.raises(ValidationError):
        make_valid_finding_v2(rule_name="Security")

    with pytest.raises(ValidationError):
        make_valid_finding_v2(rule_name="service-defaults:")


def test_review_finding_v2_requires_evidence_refs() -> None:
    with pytest.raises(ValidationError):
        make_valid_finding_v2(evidence_refs=[])


def test_review_output_v2_orders_findings_and_accepts_summary() -> None:
    f1 = make_valid_finding_v2()
    summary = ReviewSummary(
        problem="Missing input validation creates SQL injection risk.",
        done_well="Clean architecture with repository pattern.",
        effort="small",
    )
    out = ReviewOutputV2(findings=[f1], summary=summary)
    assert len(out.findings) == 1
    assert out.summary.effort == "small"


def test_validate_v2_attribution_checks_manifest_evidence_and_policy() -> None:
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        stage="review",
        blocks=(
            ManifestBlock(
                block_id="diff-app-main",
                source_role=SourceRole.DIFF,
                source_kind="diff",
                path="app/main.py",
                start_line=1,
                end_line=30,
                shown_line_count=30,
            ),
            ManifestBlock(
                block_id="policy-rules",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="repository_rules",
                path=".review/rules.md",
                start_line=1,
                end_line=20,
                shown_line_count=20,
            ),
        ),
    )

    valid_finding = make_valid_finding_v2(
        rule_name=".review/rules.md",
        evidence_refs=[EvidenceRef(block_id="diff-app-main", start_line=10, end_line=15)],
    )
    valid, reason = validate_v2_attribution(valid_finding, manifest)
    assert valid is True
    assert reason is None

    # Evidence pointing outside shown lines
    invalid_range_finding = make_valid_finding_v2(
        evidence_refs=[EvidenceRef(block_id="diff-app-main", start_line=20, end_line=45)],
    )
    valid, reason = validate_v2_attribution(invalid_range_finding, manifest)
    assert valid is False
    assert "not shown" in str(reason)

    # Evidence pointing to nonexistent block
    invalid_block_finding = make_valid_finding_v2(
        evidence_refs=[EvidenceRef(block_id="unknown-block", start_line=1, end_line=2)],
    )
    valid, reason = validate_v2_attribution(invalid_block_finding, manifest)
    assert valid is False
    assert "not found" in str(reason)

    # Rule name is AGENTS.md but no AGENTS block in manifest
    missing_policy_finding = make_valid_finding_v2(
        rule_name="AGENTS.md",
        evidence_refs=[EvidenceRef(block_id="diff-app-main", start_line=10, end_line=15)],
    )
    valid, reason = validate_v2_attribution(missing_policy_finding, manifest)
    assert valid is False
    assert "AGENTS.md" in str(reason)


def test_review_finding_v2_line_and_title_validation() -> None:
    # start_line >= line
    with pytest.raises(ValidationError, match="start_line must be strictly earlier than line"):
        make_valid_finding_v2(start_line=15, line=15)

    with pytest.raises(ValidationError, match="start_line must be strictly earlier than line"):
        make_valid_finding_v2(start_line=20, line=15)

    # title ends with period
    with pytest.raises(ValidationError, match="title must not end with a period"):
        ReviewFindingV2(
            path="app/main.py",
            line=15,
            start_line=10,
            severity="high",
            category="security",
            title="Title with period.",
            body="Finding body",
            suggestion=None,
            confidence=0.8,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id="diff-1", start_line=10, end_line=15)],
        )

    # title multi-line
    with pytest.raises(ValidationError, match="title must be one line"):
        ReviewFindingV2(
            path="app/main.py",
            line=15,
            start_line=10,
            severity="high",
            category="security",
            title="Line 1\nLine 2",
            body="Finding body",
            suggestion=None,
            confidence=0.8,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id="diff-1", start_line=10, end_line=15)],
        )


def test_review_output_v2_ordering_validation() -> None:
    f_high = make_valid_finding_v2(path="a.py", line=10, start_line=5)
    f_low = ReviewFindingV2(
        path="b.py",
        line=20,
        start_line=None,
        severity="low",
        category="readability",
        title="Minor variable naming",
        body="Variable could be clearer",
        suggestion=None,
        confidence=0.5,
        rule_name=None,
        evidence_refs=[EvidenceRef(block_id="diff-1", start_line=20, end_line=20)],
    )
    summary = ReviewSummary(
        problem="Found issues.",
        done_well="Clean architecture.",
        effort="small",
    )
    # Correct order: high before low
    ReviewOutputV2(findings=[f_high, f_low], summary=summary)

    # Inverted order: low before high
    with pytest.raises(ValidationError, match="findings must be severity then confidence ordered"):
        ReviewOutputV2(findings=[f_low, f_high], summary=summary)


def test_validate_v2_attribution_additional_branches() -> None:
    manifest = CallManifest(
        call_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        stage="review",
        blocks=(
            ManifestBlock(
                block_id="invalid-role-block",
                source_role=SourceRole.CANDIDATE,
                source_kind="candidate_finding",
                start_line=1,
                end_line=10,
                shown_line_count=10,
            ),
            ManifestBlock(
                block_id="diff-file",
                source_role=SourceRole.DIFF,
                source_kind="file_diff",
                start_line=1,
                end_line=30,
                shown_line_count=30,
            ),
            ManifestBlock(
                block_id="agents-doc",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="project_conventions_document",
                path="AGENTS.md",
                start_line=1,
                end_line=20,
                shown_line_count=20,
            ),
        ),
    )

    # Invalid block role (not diff, source_excerpt, ast_excerpt)
    f_bad_role = make_valid_finding_v2(
        evidence_refs=[EvidenceRef(block_id="invalid-role-block", start_line=1, end_line=5)],
    )
    valid, reason = validate_v2_attribution(f_bad_role, manifest)
    assert valid is False
    assert "has invalid role" in str(reason)

    # Rule name is .review/rules.md but no repository_rules in manifest
    f_missing_rules = make_valid_finding_v2(
        rule_name=".review/rules.md",
        evidence_refs=[EvidenceRef(block_id="diff-file", start_line=5, end_line=10)],
    )
    valid, reason = validate_v2_attribution(f_missing_rules, manifest)
    assert valid is False
    assert "no repository_rules in manifest" in str(reason)

    # Rule name is AGENTS.md and manifest HAS AGENTS.md
    f_agents = make_valid_finding_v2(
        rule_name="AGENTS.md",
        evidence_refs=[EvidenceRef(block_id="diff-file", start_line=5, end_line=10)],
    )
    valid, reason = validate_v2_attribution(f_agents, manifest)
    assert valid is True
    assert reason is None

    # Rule name is None (built-in standard criteria)
    f_none_rule = make_valid_finding_v2(
        rule_name=None,
        evidence_refs=[EvidenceRef(block_id="diff-file", start_line=5, end_line=10)],
    )
    valid, reason = validate_v2_attribution(f_none_rule, manifest)
    assert valid is True
    assert reason is None


def test_parse_review_output_with_evidence_refs() -> None:
    payload = {
        "findings": [
            {
                "path": "app/main.py",
                "line": 15,
                "start_line": 10,
                "severity": "high",
                "category": "security",
                "title": "SQL injection risk",
                "body": "Raw query concatenation",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
                "evidence_refs": [{"block_id": "b-1", "start_line": 10, "end_line": 15}],
            }
        ],
        "summary": {
            "problem": "Problem found.",
            "done_well": "Done well.",
            "effort": "small",
        },
    }
    # parse_review_output (v1) strips evidence_refs and validates cleanly
    res_v1 = parse_review_output(payload)
    assert len(res_v1.findings) == 1
    assert res_v1.findings[0].path == "app/main.py"

    # parse_review_output (v1) bytes
    res_v1_bytes = parse_review_output(json.dumps(payload).encode("utf-8"))
    assert len(res_v1_bytes.findings) == 1

    # parse_review_output (v1) empty findings
    res_v1_empty = parse_review_output({"findings": [], "summary": payload["summary"]})
    assert len(res_v1_empty.findings) == 0

    # parse_review_output_v2 preserves evidence_refs
    res_v2 = parse_review_output_v2(payload)
    assert len(res_v2.findings) == 1
    assert len(res_v2.findings[0].evidence_refs) == 1

    # parse_review_output_v2 string
    res_v2_str = parse_review_output_v2(json.dumps(payload))
    assert len(res_v2_str.findings) == 1

    # parse_review_output_v2 invalid
    with pytest.raises(InvalidReviewOutput):
        parse_review_output_v2({"invalid": True})

    # parse_review_output where findings is not a list or data is not a dict
    with pytest.raises(InvalidReviewOutput):
        parse_review_output('{"summary": {}}')
    with pytest.raises(InvalidReviewOutput):
        parse_review_output("123")
