"""Deterministic public-boundary tests for review finding post-processing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.findings_post_processor import FindingsPostProcessor
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.publish_run_review import move_inline_to_body
from app.modules.reviews.application.review_output import (
    PublishedFinding,
    ReviewFinding,
    ReviewOutput,
    ReviewSummary,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.application.verdict import SeverityThresholdValue


def _added_file_diff_map(path: str, line_count: int) -> DiffMap:
    """One added file whose new-side lines ``1..line_count`` are anchorable."""
    lines = "\n".join(f"+line{n}" for n in range(1, line_count + 1))
    patch = f"@@ -0,0 +1,{line_count} @@\n{lines}"
    return _diff_map_from_vcs(
        VcsFile(
            path,
            "added",
            None,
            None,
            line_count,
            0,
            line_count,
            patch,
        )
    )


def _diff_map_from_vcs(*files: VcsFile) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(
            installation_external_id=0, repository_full_name="acme/app", number=1
        ),
        head_sha="c" * 40,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="",
            description=None,
            author="",
            source_branch="",
            target_branch="",
            labels=(),
            files_changed=len(files),
            lines_added=sum(item.additions for item in files),
            lines_removed=sum(item.deletions for item in files),
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="a" * 40,
    )


def _default_diff_map() -> DiffMap:
    return _added_file_diff_map("app/example.py", 14)


def _empty_diff_map() -> DiffMap:
    return _diff_map_from_vcs()


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


def process(  # type: ignore[no-untyped-def]
    *findings: dict[str, object],
    max_inline: int | None = None,
    diff_map: DiffMap | None = None,
    severity_threshold: SeverityThresholdValue = "all",
):
    return FindingsPostProcessor.from_default_patterns().process(
        output(*findings),
        diff_map=diff_map if diff_map is not None else _default_diff_map(),
        rule_names=frozenset({"Naming Consistency"}),
        repository_max_inline=max_inline,
        severity_threshold=severity_threshold,
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
        diff_map=_added_file_diff_map("app/example.py", 14),
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


def test_review_body_starts_with_verdict_risk_summary_and_checked_paths() -> None:
    result = process(
        finding(title="Blocking defect", severity="high"),
        finding(
            title="Unused import of os",
            body="The unused import should be removed.",
        ),
        finding(path="missing.py", title="Unknown critical", severity="critical"),
        finding(title="Quiet note", severity="info", confidence=0.4),
        diff_map=_diff_map_from_vcs(
            VcsFile(
                "app/example.py",
                "added",
                None,
                None,
                14,
                0,
                14,
                "@@ -0,0 +1,14 @@\n" + "\n".join(f"+line{n}" for n in range(1, 15)),
            ),
            VcsFile(
                "z/last.py",
                "added",
                None,
                None,
                12,
                0,
                12,
                "@@ -0,0 +1,12 @@\n" + "\n".join(f"+line{n}" for n in range(1, 13)),
            ),
        ),
    )

    assert result.review_body.startswith(
        "\n".join(
            (
                "## Review summary",
                "",
                "**Verdict:** blocking",
                "**Risk summary:** critical: 0, high: 1, medium: 0, low: 0, info: 1",
                "**Checked files:**",
                "- app/example.py",
                "- z/last.py",
                "",
                "**Main issue:** A required value is not validated.",
                "",
                "**Done well:** The endpoint remains small.",
                "",
                "**Effort:** small",
            )
        )
    )
    assert "## Additional findings" in result.review_body
    assert "Quiet note" in result.review_body
    assert "Unused import of os" not in result.review_body
    assert "Unknown critical" not in result.review_body
    appended = move_inline_to_body(result.review_body, (_moved_finding(),))
    assert appended.index("**Verdict:**") < appended.index("## Inline findings")
    assert "- **Moved note** (`src/app.py:3`): outside the diff" in appended


def test_checked_files_are_none_when_the_diff_has_no_paths() -> None:
    result = process(finding(severity="high"), diff_map=_empty_diff_map())

    assert result.review_body == "\n".join(
        (
            "## Review summary",
            "",
            "**Verdict:** clean",
            "**Risk summary:** critical: 0, high: 0, medium: 0, low: 0, info: 0",
            "**Checked files:**",
            "(none)",
            "",
            "**Main issue:** A required value is not validated.",
            "",
            "**Done well:** The endpoint remains small.",
            "",
            "**Effort:** small",
        )
    )


def test_character_cap_drops_only_additional_findings() -> None:
    notes = tuple(
        finding(title=f"Overflow {number}", confidence=0.4, body="x" * 1200)
        for number in range(1, 7)
    )
    capped = process(finding(title="Blocking defect", severity="high"), *notes)
    assert capped.review_body.startswith(
        "\n".join(
            (
                "## Review summary",
                "",
                "**Verdict:** blocking",
                "**Risk summary:** critical: 0, high: 1, medium: 0, low: 6, info: 0",
                "**Checked files:**",
                "- app/example.py",
            )
        )
    )
    assert len(capped.review_body) == 4000
    assert "**Effort:** small" in capped.review_body
    assert "Overflow 1" in capped.review_body
    assert "Overflow 6" not in capped.review_body

    long_path_files = tuple(
        VcsFile(
            f"pkg/file_{index:03d}_{'n' * 60}.py",
            "added",
            None,
            None,
            1,
            0,
            1,
            "@@ -0,0 +1,1 @@\n+only",
        )
        for index in range(50)
    ) + (
        VcsFile(
            "app/example.py",
            "added",
            None,
            None,
            14,
            0,
            14,
            "@@ -0,0 +1,14 @@\n" + "\n".join(f"+line{n}" for n in range(1, 15)),
        ),
    )
    long_body = process(
        finding(title="Blocking defect", severity="high"),
        finding(title="Spill note", confidence=0.4),
        diff_map=_diff_map_from_vcs(*long_path_files),
    )
    assert len(long_body.review_body) > 4000
    assert "**Verdict:** blocking" in long_body.review_body
    assert (
        long_body.review_body.index("- app/example.py")
        < long_body.review_body.index("- pkg/file_000_")
        < long_body.review_body.index("- pkg/file_049_")
    )
    assert "**Effort:** small" in long_body.review_body
    assert "## Additional findings" not in long_body.review_body
    assert "Spill note" not in long_body.review_body


def test_critical_threshold_drops_medium_low_and_info_and_turns_attention_into_clean() -> None:
    findings = (
        finding(title="Medium defect", severity="medium"),
        finding(title="Low defect", severity="low", line=13),
        finding(title="Info note", severity="info", line=14),
    )

    control = process(*findings)
    result = process(*findings, severity_threshold="critical")

    assert control.review_body.startswith("## Review summary\n\n**Verdict:** attention\n")
    assert "Below severity threshold" not in control.review_body
    assert result.inline == ()
    assert result.body_only == ()
    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        ("Medium defect", "severity_threshold"),
        ("Low defect", "severity_threshold"),
        ("Info note", "severity_threshold"),
    ]
    assert result.review_body == "\n".join(
        (
            "## Review summary",
            "",
            "**Verdict:** clean",
            "**Risk summary:** critical: 0, high: 0, medium: 0, low: 0, info: 0",
            "**Below severity threshold (critical):** 3",
            "**Checked files:**",
            "- app/example.py",
            "",
            "**Main issue:** A required value is not validated.",
            "",
            "**Done well:** The endpoint remains small.",
            "",
            "**Effort:** small",
        )
    )


@pytest.mark.parametrize(
    ("threshold", "published", "below", "line"),
    [
        ("all", ["critical", "high", "medium", "low", "info"], [], None),
        (
            "warning",
            ["critical", "high", "medium", "low"],
            ["info"],
            "**Below severity threshold (warning):** 1",
        ),
        (
            "critical",
            ["critical", "high"],
            ["medium", "low", "info"],
            "**Below severity threshold (critical):** 3",
        ),
    ],
)
def test_threshold_publishes_only_its_severity_group(
    threshold: SeverityThresholdValue, published: list[str], below: list[str], line: str | None
) -> None:
    result = process(
        *(
            finding(title=f"{severity.title()} finding", severity=severity, line=10 + index)
            for index, severity in enumerate(("critical", "high", "medium", "low", "info"))
        ),
        severity_threshold=threshold,
    )

    assert [item.finding.severity for item in result.inline] == published
    assert result.body_only == ()
    assert [(item.finding.severity, item.drop_reason) for item in result.dropped] == [
        (severity, "severity_threshold") for severity in below
    ]
    if line is None:
        assert "Below severity threshold" not in result.review_body
    else:
        assert line in result.review_body


def test_risk_summary_counts_only_findings_that_pass_the_threshold() -> None:
    result = process(
        finding(title="Blocking defect", severity="high"),
        finding(title="Minor defect", severity="low", line=13),
        finding(title="Quiet note", severity="info", line=14),
        severity_threshold="warning",
    )

    assert result.review_body.startswith(
        "\n".join(
            (
                "## Review summary",
                "",
                "**Verdict:** blocking",
                "**Risk summary:** critical: 0, high: 1, medium: 0, low: 1, info: 0",
                "**Below severity threshold (warning):** 1",
                "**Checked files:**",
            )
        )
    )


def test_inline_cap_counts_only_findings_that_pass_the_threshold() -> None:
    # The cap keeps the highest severities, so threshold-then-cap and cap-then-threshold agree
    # here: this does not pin the threshold before the cap. The order against lint, dedupe and
    # routing is pinned by the next test,
    # test_threshold_runs_after_lint_and_dedupe_and_before_path_and_confidence_routing.
    blocking = tuple(
        finding(title=f"Blocking {number}", severity="high", line=10 + number)
        for number in range(4)
    )
    minor = tuple(
        finding(title=f"Minor {number}", severity="low", line=10 + number % 5)
        for number in range(8)
    )
    # Twelve findings exceed the model contract's ten; built like the 14-finding fixture above.
    parsed = ReviewOutput.model_construct(
        findings=[ReviewFinding.model_validate(item) for item in (*blocking, *minor)],
        summary=output().summary,
    )

    result = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        diff_map=_added_file_diff_map("app/example.py", 14),
        rule_names=frozenset(),
        severity_threshold="critical",
    )

    assert [item.finding.title for item in result.inline] == [
        "Blocking 0",
        "Blocking 1",
        "Blocking 2",
        "Blocking 3",
    ]
    assert result.body_only == ()
    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        (f"Minor {number}", "severity_threshold") for number in range(8)
    ]
    assert "**Below severity threshold (critical):** 8" in result.review_body


def test_threshold_runs_after_lint_and_dedupe_and_before_path_and_confidence_routing() -> None:
    result = process(
        finding(path="other.py", title="Unknown blocking file", severity="high"),
        finding(title="Unused import of os", body="The unused import should be removed."),
        finding(title="Missing null check", line=13),
        finding(path="other.py", title="Unknown minor file"),
        finding(title="missing null-check", line=13, confidence=0.7),
        finding(title="Low confidence", confidence=0.4),
        severity_threshold="critical",
    )

    assert result.inline == ()
    assert result.body_only == ()
    assert [(item.finding.title, item.drop_reason) for item in result.dropped] == [
        ("Unknown blocking file", "unknown_path"),
        ("Unused import of os", "lint_pattern"),
        ("Missing null check", "severity_threshold"),
        ("Unknown minor file", "severity_threshold"),
        ("missing null-check", "duplicate"),
        ("Low confidence", "severity_threshold"),
    ]
    assert "**Below severity threshold (critical):** 3" in result.review_body


@pytest.mark.parametrize("threshold", [None, "all"])
def test_all_threshold_keeps_the_ten_findings_fixture_result(
    threshold: SeverityThresholdValue | None,
) -> None:
    fixture = Path(__file__).parent / "fixtures" / "review_output" / "valid" / "ten-findings.json"
    parsed = ReviewOutput.model_validate_json(fixture.read_text(encoding="utf-8"))
    processor = FindingsPostProcessor.from_default_patterns()
    diff_map = _added_file_diff_map("app/modules/billing/application/charge.py", 20)

    result = (
        processor.process(parsed, diff_map=diff_map, rule_names=frozenset())
        if threshold is None
        else processor.process(
            parsed, diff_map=diff_map, rule_names=frozenset(), severity_threshold=threshold
        )
    )

    assert [item.finding.line for item in result.inline] == list(range(10, 18))
    assert [item.finding.line for item in result.body_only] == [18, 19]
    assert result.dropped == ()
    assert result.review_body.startswith(
        "\n".join(
            (
                "## Review summary",
                "",
                "**Verdict:** attention",
                "**Risk summary:** critical: 0, high: 0, medium: 10, low: 0, info: 0",
                "**Checked files:**",
                "- app/modules/billing/application/charge.py",
            )
        )
    )
    assert "Below severity threshold" not in result.review_body


def _moved_finding() -> PublishedFinding:
    return PublishedFinding(
        path="src/app.py",
        line=3,
        start_line=None,
        severity="high",
        category="correctness",
        title="Moved note",
        body="outside the diff",
        suggestion=None,
        confidence=0.9,
        rule_name=None,
    )


def test_findings_post_processor_legacy_nulls_out_v2_rules() -> None:
    from app.modules.reviews.application.review_output import (
        ReviewFinding,
        ReviewOutput,
        ReviewSummary,
    )

    processor = FindingsPostProcessor.from_default_patterns()
    findings = [
        ReviewFinding(
            path="app/main.py",
            line=10,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 1",
            body="Body 1",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
        ),
        ReviewFinding(
            path="app/main.py",
            line=11,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 2",
            body="Body 2",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:foo",
        ),
    ]
    output = ReviewOutput(
        findings=findings,
        summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
    )
    # Legacy run: allowed_v2_rules is None
    result = processor.process(
        output,
        diff_map=_added_file_diff_map("app/main.py", 11),
        rule_names=frozenset(),
        allowed_v2_rules=None,
    )
    # Both v2 rule names must be nulled out in legacy mode!
    assert len(result.inline) == 2
    assert result.inline[0].finding.rule_name is None
    assert result.inline[1].finding.rule_name is None


def test_findings_post_processor_v2_allows_valid_and_rejects_malformed_rules() -> None:
    from app.modules.reviews.application.review_output import (
        ReviewFinding,
        ReviewOutput,
        ReviewSummary,
    )

    processor = FindingsPostProcessor.from_default_patterns()
    findings = [
        ReviewFinding(
            path="app/main.py",
            line=10,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 1",
            body="Body 1",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
        ),
        ReviewFinding(
            path="app/main.py",
            line=11,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 2",
            body="Body 2",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:valid-rule_1",
        ),
        ReviewFinding(
            path="app/main.py",
            line=12,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 3",
            body="Body 3",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:[x](http://evil.com)",
        ),
        ReviewFinding(
            path="app/main.py",
            line=13,
            start_line=None,
            severity="high",
            category="security",
            title="Finding 4",
            body="Body 4",
            suggestion=None,
            confidence=0.9,
            rule_name="service-defaults:",
        ),
    ]
    output = ReviewOutput(
        findings=findings,
        summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
    )
    # V2 run: allowed_v2_rules provided
    result = processor.process(
        output,
        diff_map=_added_file_diff_map("app/main.py", 13),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset({".review/rules.md", "AGENTS.md"}),
    )
    assert len(result.inline) == 4
    # .review/rules.md is allowed
    assert result.inline[0].finding.rule_name == ".review/rules.md"
    # valid service-defaults ID is allowed
    assert result.inline[1].finding.rule_name == "service-defaults:valid-rule_1"
    # malformed service-defaults are nulled out
    assert result.inline[2].finding.rule_name is None
    assert result.inline[3].finding.rule_name is None


def test_line_absent_in_diff_map_routes_to_body_only() -> None:
    """Publish bucketing uses DiffMap + resolve_anchor, not hunk line sets (#171)."""
    result = process(finding(title="Outside diff map", line=99))

    assert [item.finding.title for item in result.inline] == []
    assert [item.finding.title for item in result.body_only] == ["Outside diff map"]
    assert result.dropped == ()


def test_findings_post_processor_v2_preserves_attribution_for_body_only_findings() -> None:
    from app.modules.reviews.application.review_output import (
        ReviewFinding,
        ReviewOutput,
        ReviewSummary,
    )

    processor = FindingsPostProcessor.from_default_patterns()
    findings = [
        ReviewFinding(
            path="app/main.py",
            line=999,  # line not in hunk lines, so it is routed to body_only
            start_line=None,
            severity="high",
            category="security",
            title="Finding 1",
            body="Body 1",
            suggestion=None,
            confidence=0.9,
            rule_name=".review/rules.md",
        ),
        ReviewFinding(
            path="app/main.py",
            line=10,  # low confidence (< 0.8), so it is routed to body_only
            start_line=None,
            severity="high",
            category="security",
            title="Finding 2",
            body="Body 2",
            suggestion=None,
            confidence=0.5,
            rule_name="service-defaults:auth-check",
        ),
    ]
    output = ReviewOutput(
        findings=findings,
        summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
    )

    # 1. Snapshot run with allowed_v2_rules: attribution must be preserved in body_only!
    result = processor.process(
        output,
        diff_map=_added_file_diff_map("app/main.py", 10),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset({".review/rules.md"}),
    )
    assert len(result.body_only) == 2
    assert result.body_only[0].finding.rule_name == ".review/rules.md"
    assert result.body_only[1].finding.rule_name == "service-defaults:auth-check"

    # 2. Legacy run without allowed_v2_rules: attribution must be cleared in body_only!
    legacy_result = processor.process(
        output,
        diff_map=_added_file_diff_map("app/main.py", 10),
        rule_names=frozenset(),
        allowed_v2_rules=None,
    )
    assert len(legacy_result.body_only) == 2
    assert legacy_result.body_only[0].finding.rule_name is None
    assert legacy_result.body_only[1].finding.rule_name is None
