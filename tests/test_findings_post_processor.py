"""Deterministic public-boundary tests for review finding post-processing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast

from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
from app.modules.reviews.application.review_output import ReviewFinding, ReviewOutput, ReviewSummary


def finding(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "path": "app/example.py",
        "line": 12,
        "start_line": None,
        "severity": "low",
        "category": "correctness",
        "title": "Missing validation",
        "body": "The request is used without checking its required value.",
        "suggestion": "validate(value)",
        "confidence": 0.9,
        "rule_name": None,
    }
    value.update(overrides)
    return value


def output(*findings: dict[str, object]) -> ReviewOutput:
    rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    ordered = sorted(
        findings,
        key=lambda item: (
            rank[cast(str, item["severity"])],
            -cast(float, item["confidence"]),
        ),
    )
    return ReviewOutput.model_validate(
        {
            "findings": list(ordered),
            "summary": {
                "problem": "A required value is not validated.",
                "done_well": "The endpoint remains small.",
                "effort": "small",
            },
        }
    )


def process(*findings: dict[str, object], max_inline: int | None = None):  # type: ignore[no-untyped-def]
    return FindingsPostProcessor.from_default_patterns().process(
        output(*findings),
        hunk_lines={"app/example.py": frozenset({10, 11, 12, 13, 14})},
        rule_names=frozenset({"Naming Consistency"}),
        repository_max_inline=max_inline,
    )


def test_lint_patterns_keep_security_exceptions_and_drop_lint_only_findings() -> None:
    result = process(
        finding(title="Unused import of os", body="The unused import should be removed."),
        finding(
            title="Unused variable holds a hardcoded password",
            body="The password remains available in process memory.",
        ),
        finding(
            title="Line too long hides an SQL injection",
            body="The query accepts untrusted input.",
            severity="critical",
        ),
    )

    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        ("Unused import of os", "lint_pattern")
    ]
    assert [item.finding.title for item in result.inline] == [
        "Line too long hides an SQL injection",
        "Unused variable holds a hardcoded password",
    ]


def test_dedup_confidence_hunks_and_cap_put_each_finding_in_one_bucket() -> None:
    duplicates = (
        finding(title="missing null-check", confidence=0.9, severity="high"),
        finding(title="Missing null check", confidence=0.7),
    )
    overflow = tuple(
        finding(title=f"Finding {number}", line=10 + number % 5, confidence=0.8)
        for number in range(5)
    )
    result = process(
        *duplicates,
        finding(title="Low confidence", confidence=0.4),
        finding(title="Outside hunk", line=99),
        finding(title="Valid range", line=12, start_line=10),
        *overflow,
        max_inline=2,
    )

    all_positions = [item.position for bucket in result.buckets for item in bucket]
    assert sorted(all_positions) == list(range(10))
    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        ("Missing null check", "duplicate")
    ]
    assert result.inline[0].finding.title == "missing null-check"
    assert len(result.inline) == 2
    body_by_title = {item.finding.title: item.finding for item in result.body_only}
    assert body_by_title["Low confidence"].suggestion == "validate(value)"
    assert body_by_title["Outside hunk"].suggestion == "validate(value)"
    inline_and_body = result.inline + result.body_only
    assert (
        next(
            item.finding for item in inline_and_body if item.finding.title == "Valid range"
        ).start_line
        == 10
    )


def test_unknown_path_is_dropped_and_bad_custom_attribution_is_repaired_to_body_only() -> None:
    result = process(
        finding(path="other.py", title="Unknown file"),
        finding(
            title="Bad attribution",
            rule_name=None,
            body=(
                "According to custom instructions in 'Missing Rule' (details): The value is unsafe."
            ),
        ),
        finding(
            title="Wrong named attribution",
            rule_name="Naming Consistency",
            body="The value is unsafe.",
        ),
    )

    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        ("Unknown file", "unknown_path")
    ]
    by_title = {item.finding.title: item.finding for item in result.body_only}
    assert by_title["Bad attribution"].body == "The value is unsafe."
    assert by_title["Bad attribution"].rule_name is None
    inline_by_title = {item.finding.title: item.finding for item in result.inline}
    assert inline_by_title["Wrong named attribution"].rule_name is None


def test_body_rendering_has_body_only_findings_and_is_limited_to_4000_characters() -> None:
    result = process(finding(title="Low confidence", confidence=0.4, body="x" * 1200))

    assert "## Additional findings" in result.review_body
    assert "Low confidence" in result.review_body
    assert len(result.review_body) <= 4000


def test_default_cap_classifies_fixture_as_ten_inline_and_four_body_only() -> None:
    fixture = Path(__file__).parent / "fixtures" / "lint_filter_default_14.json"
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    raw_findings = cast(list[dict[str, object]], payload["findings"])
    findings = [ReviewFinding.model_validate(item) for item in raw_findings]
    parsed = ReviewOutput.model_construct(
        findings=findings,
        summary=ReviewSummary.model_validate(cast(dict[str, object], payload["summary"])),
    )

    result = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        hunk_lines={"app/example.py": frozenset(range(1, 15))},
        rule_names=frozenset(),
    )

    assert len(result.inline) == 10
    assert len(result.body_only) == 4
    assert [item.finding.title for item in result.inline] == [
        f"Finding {number:02d}" for number in range(1, 11)
    ]
    assert [item.finding.title for item in result.body_only] == [
        f"Finding {number:02d}" for number in range(11, 15)
    ]
