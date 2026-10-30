"""Deterministic public-boundary tests for review finding post-processing."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.findings_post_processor import (
    FindingsPostProcessor,
    _format_checked_files,
    finalize_stored_review_body,
    format_body_only_overflow_footer,
    format_body_only_overflow_footer_without_url,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.publish_run_review import move_inline_to_body
from app.modules.reviews.application.review_output import (
    PublishedFinding,
    ReviewFinding,
    ReviewOutput,
    ReviewSummary,
)
from app.modules.reviews.application.run_coverage import CoverageStatus, RunCoverage
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.application.verdict import SeverityThresholdValue
from tests.test_diff_anchor import _diff_map
from tests.test_diff_anchor import _file as anchor_file


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


def _trusted_coverage(paths: frozenset[str]) -> RunCoverage:
    """Formatting fixtures provide trusted coverage separately from diff anchors."""
    return RunCoverage(
        CoverageStatus.COMPLETE if paths else CoverageStatus.NONE,
        len(paths),
        total_files=len(paths),
        file_list_complete=True,
        durable_reviewable_paths=paths,
    )


def process(  # type: ignore[no-untyped-def]
    *findings: dict[str, object],
    max_inline: int | None = None,
    diff_map: DiffMap | None = None,
    checked_paths: frozenset[str] | None = None,
    severity_threshold: SeverityThresholdValue = "all",
):
    resolved_map = diff_map if diff_map is not None else _default_diff_map()
    paths = checked_paths if checked_paths is not None else _paths_of(resolved_map)
    return FindingsPostProcessor.from_default_patterns().process(
        output(*findings),
        diff_map=resolved_map,
        checked_paths=paths,
        rule_names=frozenset({"Naming Consistency"}),
        repository_max_inline=max_inline,
        severity_threshold=severity_threshold,
        coverage=_trusted_coverage(paths),
    )


def _paths_of(diff_map: DiffMap) -> frozenset[str]:
    return frozenset(
        path
        for file in diff_map.files
        for path in (file.new_path, file.old_path)
        if path is not None
    )


def _count_listed_additional_findings(review_body: str) -> int:
    """Count whole finding bullets under the Additional findings section (test-side D-3 assert)."""
    marker = "\n## Additional findings\n"
    start = review_body.find(marker)
    if start < 0:
        return 0
    tail = review_body[start + len(marker) :]
    footer_marker = "\n\n**Ещё "
    footer_at = tail.find(footer_marker)
    if footer_at >= 0:
        tail = tail[:footer_at]
    return sum(1 for line in tail.splitlines() if line.startswith("- **"))


def _parse_body_only_overflow_footer_count(review_body: str) -> int:
    """Read ``N`` from a store-layer overflow footer (with or without a run URL)."""
    with_url = re.search(r"\n\n\*\*Ещё (\d+) наход(?:ка|ки|ок):\*\*", review_body)
    if with_url is not None:
        return int(with_url.group(1))
    without_url = re.search(r"\n\n\*\*Ещё (\d+) наход(?:ка|ки|ок)\*\*", review_body)
    return int(without_url.group(1)) if without_url is not None else 0


def _body_only_accounted_in_review_body(review_body: str) -> int:
    """Visible Additional findings bullets plus any overflow footer count (D-3)."""
    return _count_listed_additional_findings(review_body) + _parse_body_only_overflow_footer_count(
        review_body
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

    example_diff = _added_file_diff_map("app/example.py", 14)
    result = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        diff_map=example_diff,
        checked_paths=_paths_of(example_diff),
        rule_names=frozenset(),
        coverage=_trusted_coverage(_paths_of(example_diff)),
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
        checked_paths=frozenset({"app/example.py", "z/last.py"}),
    )

    assert result.review_body.startswith(
        "\n".join(
            (
                "## Review summary",
                "",
                "**Verdict:** blocking",
                "**Risk summary:** critical: 0, high: 1, medium: 0, low: 0, info: 1",
                "**Coverage:** partial — Проверка неполная: "
                "не удалось сопоставить замечание с доступным diff (1).",
                "- не удалось сопоставить замечание с доступным diff",
                "**Проверено файлов с текстовыми изменениями:** 2",
                "**Checked files:**",
                "- app/example.py",
                "- z/last.py",
                "",
                "**Main issue:** Подтверждено замечаний: 2 (correctness: 2); Проверка неполная: "
                "не удалось сопоставить замечание с доступным diff (1).",
                "",
                "**Done well:** Проанализировано файлов с текстовыми изменениями: 2; "
                "исключено файлов: 0.",
                "",
                "**Effort:** medium",
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


def test_move_inline_to_body_with_no_findings_leaves_body_unchanged() -> None:
    body = "## Review summary\n\nDone."
    assert move_inline_to_body(body, ()) == body


def test_checked_files_include_deleted_paths_when_reviewed() -> None:
    diff_map = _diff_map_from_vcs(
        VcsFile(
            "gone.py",
            "removed",
            None,
            None,
            0,
            2,
            2,
            "@@ -1,2 +0,0 @@\n-one\n-two",
        ),
        VcsFile(
            "app/example.py",
            "modified",
            None,
            None,
            1,
            1,
            2,
            "@@ -1 +1 @@\n-x\n+y",
        ),
    )
    result = process(
        finding(path="app/example.py", line=1),
        diff_map=diff_map,
        checked_paths=frozenset({"gone.py", "app/example.py"}),
    )
    assert "- app/example.py" in result.review_body
    assert "- gone.py" in result.review_body


def test_checked_files_list_only_reviewable_snapshots() -> None:
    """Paths without review_patch stay anchorable but not under Checked files (#170 D-2)."""
    diff_map = _diff_map_from_vcs(
        *(
            VcsFile(
                path,
                "added",
                None,
                None,
                14,
                0,
                14,
                "@@ -0,0 +1,14 @@\n" + "\n".join(f"+line{n}" for n in range(1, 15)),
            )
            for path in ("app/reviewed.py", "vendor/ignored.py", "docs/summary_only.md")
        )
    )
    result = process(
        finding(path="app/reviewed.py", line=12, title="Blocking defect", severity="high"),
        finding(
            path="vendor/ignored.py",
            line=12,
            title="Ignored path note",
            confidence=0.4,
        ),
        diff_map=diff_map,
        checked_paths=frozenset({"app/reviewed.py"}),
    )

    checked_section = result.review_body.split("**Checked files:**", maxsplit=1)[1].split(
        "\n\n", 1
    )[0]
    assert "- app/reviewed.py" in checked_section
    assert "- vendor/ignored.py" not in checked_section
    assert "- docs/summary_only.md" not in checked_section
    assert result.inline[0].finding.path == "app/reviewed.py"
    assert any(item.finding.title == "Ignored path note" for item in result.body_only)


def _diff_map_for_paths(*paths: str) -> DiffMap:
    return _diff_map_from_vcs(
        *(
            VcsFile(
                path,
                "added",
                None,
                None,
                1,
                0,
                1,
                "@@ -0,0 +1,1 @@\n+only",
            )
            for path in paths
        )
    )


def test_checked_files_cap_lists_all_paths_when_they_fit() -> None:
    paths = frozenset(f"app/module_{index:02d}.py" for index in range(3))
    result = process(
        finding(severity="high"),
        diff_map=_diff_map_for_paths(*sorted(paths)),
        checked_paths=paths,
    )

    checked_section = (
        result.review_body.split("**Checked files:**", maxsplit=1)[1].split("\n\n", 1)[0].strip()
    )
    assert checked_section == "\n".join(f"- {path}" for path in sorted(paths))
    assert "… и ещё" not in checked_section


def test_checked_files_cap_lists_one_path_without_overflow_line() -> None:
    paths = frozenset({"only/reviewed.py"})
    result = process(
        finding(path="only/reviewed.py", line=1, severity="high"),
        diff_map=_diff_map_for_paths("only/reviewed.py"),
        checked_paths=paths,
    )

    checked_section = (
        result.review_body.split("**Checked files:**", maxsplit=1)[1].split("\n\n", 1)[0].strip()
    )
    assert checked_section == "- only/reviewed.py"
    assert "… и ещё" not in checked_section


def test_checked_files_cap_overflow_line_counts_omitted_paths() -> None:
    long_path_count = 62

    def long_checked_path(index: int) -> str:
        prefix = f"pkg/file_{index:03d}_"
        suffix = ".py"
        padding = 58 - len(prefix) - len(suffix)
        return f"{prefix}{'n' * padding}{suffix}"

    paths = tuple(long_checked_path(index) for index in range(long_path_count))
    result = process(
        finding(severity="high"),
        finding(title="Spill note", confidence=0.4),
        diff_map=_diff_map_for_paths(*paths),
        checked_paths=frozenset(paths),
    )

    checked_section = (
        result.review_body.split("**Checked files:**", maxsplit=1)[1].split("\n\n", 1)[0].strip()
    )
    listed = [line for line in checked_section.splitlines() if line.startswith("- ")]
    overflow = [line for line in listed if "… и ещё" in line]
    assert overflow
    shown_paths = [line.removeprefix("- ") for line in listed if "… и ещё" not in line]
    assert shown_paths == sorted(paths)[: len(shown_paths)]
    assert int(overflow[0].split("… и ещё ", 1)[1].split(" ", 1)[0]) == len(paths) - len(
        shown_paths
    )


def test_checked_files_are_none_when_the_diff_has_no_paths() -> None:
    result = process(
        finding(severity="high"),
        diff_map=_empty_diff_map(),
        checked_paths=frozenset(),
    )

    assert result.review_body == "\n".join(
        (
            "## Review summary",
            "",
            "**Verdict:** проверка неполная",
            "**Risk summary:** critical: 0, high: 0, medium: 0, low: 0, info: 0",
            "**Coverage:** partial — Проверка неполная: "
            "не удалось сопоставить замечание с доступным diff (1).",
            "- не удалось сопоставить замечание с доступным diff",
            "**Проверено файлов с текстовыми изменениями:** 0",
            "**Checked files:**",
            "(none)",
            "",
            "**Main issue:** Подтверждённых замечаний нет; Проверка неполная: "
            "не удалось сопоставить замечание с доступным diff (1).",
            "",
            "**Done well:** Проанализировано файлов с текстовыми изменениями: 0; "
            "исключено файлов: 0.",
            "",
            "**Effort:** none",
        )
    )


def test_overflow_phrases_agree_with_russian_cardinals() -> None:
    assert format_body_only_overflow_footer_without_url(1).endswith("**Ещё 1 находка**")
    assert format_body_only_overflow_footer(3, "https://ui.test/runs/x").endswith(
        "**Ещё 3 находки:** https://ui.test/runs/x"
    )
    assert format_body_only_overflow_footer_without_url(11).endswith("**Ещё 11 находок**")
    assert format_body_only_overflow_footer(21, "https://ui.test/runs/x").endswith(
        "**Ещё 21 находка:** https://ui.test/runs/x"
    )
    paths = ("a.py", "b.py", "c.py", "d.py")
    assert _format_checked_files(paths, 1).endswith("… и ещё 3 файла")
    assert _format_checked_files(paths[:2], 1).endswith("… и ещё 1 файл")
    assert _format_checked_files(("a.py",) * 6, 1).endswith("… и ещё 5 файлов")


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
    assert len(capped.review_body) <= 4000
    assert capped.omitted_body_only_count == 4
    assert "**Effort:** medium" in capped.review_body
    assert "Overflow 1" in capped.review_body
    assert "Overflow 6" not in capped.review_body
    assert all(
        capped.review_body.count(f"Overflow {number}") == (1 if number <= 2 else 0)
        for number in range(1, 7)
    )

    # #170 / D-1: 60 long checked paths (~58 chars each) plus two body-only findings must
    # stay within the 4000-char budget. Checked files truncate with ``… и ещё N файл/файла/файлов``
    # (not every path as its own bullet). Body-only findings must appear under Additional findings
    # (overflow footer URL is store/publish — covered in Task 4, not here).
    long_path_count = 60

    def long_checked_path(index: int) -> str:
        prefix = f"pkg/file_{index:03d}_"
        suffix = ".py"
        padding = 58 - len(prefix) - len(suffix)
        assert padding > 0
        return f"{prefix}{'n' * padding}{suffix}"

    long_path_names = tuple(long_checked_path(index) for index in range(long_path_count))
    long_path_files = tuple(
        VcsFile(
            path,
            "added",
            None,
            None,
            1,
            0,
            1,
            "@@ -0,0 +1,1 @@\n+only",
        )
        for path in long_path_names
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
        finding(title="Spill note one", confidence=0.4),
        finding(title="Spill note two", confidence=0.39),
        diff_map=_diff_map_from_vcs(*long_path_files),
        checked_paths=frozenset(long_path_names) | {"app/example.py"},
    )
    body = long_body.review_body
    assert len(body) <= 4000
    assert "**Verdict:** blocking" in body
    assert "- app/example.py" in body
    assert body.index("- app/example.py") < body.index(f"- {long_checked_path(0)}")
    assert "**Effort:** medium" in body
    listed_long_paths = sum(
        1 for index in range(long_path_count) if f"- {long_checked_path(index)}" in body
    )
    assert listed_long_paths < long_path_count
    assert re.search(r"… и ещё \d+ файл(?:а|ов)?", body) is not None
    assert "## Additional findings" in body
    assert "Spill note one" in body
    assert "Spill note two" in body
    assert long_body.omitted_body_only_count == 0


def test_packer_lists_only_whole_additional_finding_bullets() -> None:
    notes = tuple(
        finding(title=f"Overflow {number}", confidence=0.4, body="x" * 1200)
        for number in range(1, 7)
    )
    capped = process(finding(title="Blocking defect", severity="high"), *notes)
    section = capped.review_body.split("## Additional findings", 1)[1]
    bullets = [line for line in section.splitlines() if line.startswith("- **")]
    assert bullets
    for line in bullets:
        assert line.endswith("x" * 1200)


def test_finalize_stored_review_body_appends_overflow_footer_within_budget() -> None:
    notes = tuple(
        finding(title=f"Overflow {number}", confidence=0.4, body="x" * 1200)
        for number in range(1, 7)
    )
    parsed = output(finding(title="Blocking defect", severity="high"), *notes)
    processed = process(finding(title="Blocking defect", severity="high"), *notes)
    run_url = "https://ui.test/runs/11111111-1111-1111-1111-111111111111"
    finalized = finalize_stored_review_body(
        parsed,
        processed,
        checked_paths=frozenset({"app/example.py"}),
        severity_threshold="all",
        below_threshold=0,
        run_url=run_url,
    )
    assert finalized.omitted_body_only_count == processed.omitted_body_only_count
    assert finalized.review_body.endswith(run_url)
    assert "**Ещё 4 находки:**" in finalized.review_body
    assert len(finalized.review_body) <= 4000
    assert _body_only_accounted_in_review_body(finalized.review_body) == 6


def test_finalize_stored_review_body_appends_overflow_count_without_url_when_run_url_missing() -> (
    None
):
    notes = tuple(
        finding(title=f"Overflow {number}", confidence=0.4, body="x" * 1200)
        for number in range(1, 7)
    )
    parsed = output(finding(title="Blocking defect", severity="high"), *notes)
    processed = process(finding(title="Blocking defect", severity="high"), *notes)
    finalized = finalize_stored_review_body(
        parsed,
        processed,
        checked_paths=frozenset({"app/example.py"}),
        severity_threshold="all",
        below_threshold=0,
        run_url=None,
    )
    assert finalized.omitted_body_only_count == processed.omitted_body_only_count
    assert finalized.review_body.endswith("**Ещё 4 находки**")
    assert "http" not in finalized.review_body.split("**Ещё")[-1]
    assert len(finalized.review_body) <= 4000
    assert _body_only_accounted_in_review_body(finalized.review_body) == 6


@pytest.mark.parametrize(
    ("footer", "expected"),
    [
        ("\n\n**Ещё 1 находка:** https://ui.test/runs/x", 1),
        ("\n\n**Ещё 3 находки**", 3),
        ("\n\n**Ещё 5 находок:** https://ui.test/runs/x", 5),
    ],
)
def test_body_only_accounted_parses_overflow_footer_with_and_without_url(
    footer: str, expected: int
) -> None:
    body = "## Review summary\n\n**Verdict:** attention\n\n## Additional findings\n\n- **One**\n"
    assert _body_only_accounted_in_review_body(body + footer) == 1 + expected


def test_finalize_stored_review_body_degrades_long_summary_instead_of_asserting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # processed must come from the same parsed output: the long summary is what forces the
    # degraded branch (prose cannot shrink below the footer).
    parsed = ReviewOutput.model_validate(
        {
            "findings": [
                finding(title="Blocking defect", severity="high"),
                *(
                    finding(title=f"Overflow {number}", confidence=0.4, body="x" * 1200)
                    for number in range(1, 7)
                ),
            ],
            "summary": {
                "problem": f"{'p' * 3498}.",
                "done_well": f"{'d' * 498}.",
                "effort": "small",
            },
        }
    )
    # #213 normally builds bounded backend prose. Exercise the packer's robustness
    # for an unusually long backend summary, rather than republishing raw model prose.
    monkeypatch.setattr(
        "app.modules.reviews.application.findings_post_processor.summary_from_result",
        lambda *_args, **_kwargs: parsed.summary,
    )
    processed = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        diff_map=_default_diff_map(),
        checked_paths=frozenset({"app/example.py"}),
        rule_names=frozenset({"Naming Consistency"}),
        coverage=_trusted_coverage(frozenset({"app/example.py"})),
    )
    run_url = "https://ui.test/runs/11111111-1111-1111-1111-111111111111"
    finalized = finalize_stored_review_body(
        parsed,
        processed,
        checked_paths=frozenset({"app/example.py"}),
        severity_threshold="all",
        below_threshold=0,
        run_url=run_url,
    )
    assert finalized.review_body_budget_degraded is True
    assert len(finalized.review_body) <= 4000
    assert finalized.review_body.endswith(run_url)
    assert finalized.omitted_body_only_count == len(processed.body_only)


def test_body_only_accounted_matches_listed_bullets_plus_footer_n() -> None:
    body = "\n".join(
        (
            "## Review summary",
            "",
            "**Verdict:** attention",
            "",
            "## Additional findings",
            "",
            "- **First**",
            "- **Second**",
            "",
            "**Ещё 5 находок:** https://ui.test/runs/x",
        )
    )
    assert _body_only_accounted_in_review_body(body) == 7


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
            "**Main issue:** Подтверждённых замечаний нет.",
            "",
            "**Done well:** Проанализировано файлов с текстовыми изменениями: 1; "
            "исключено файлов: 0.",
            "",
            "**Effort:** none",
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

    example_diff = _added_file_diff_map("app/example.py", 14)
    result = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        diff_map=example_diff,
        checked_paths=_paths_of(example_diff),
        rule_names=frozenset(),
        severity_threshold="critical",
        coverage=_trusted_coverage(_paths_of(example_diff)),
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

    paths = _paths_of(diff_map)
    result = (
        processor.process(
            parsed,
            diff_map=diff_map,
            checked_paths=paths,
            rule_names=frozenset(),
            coverage=_trusted_coverage(paths),
        )
        if threshold is None
        else processor.process(
            parsed,
            diff_map=diff_map,
            checked_paths=paths,
            rule_names=frozenset(),
            severity_threshold=threshold,
            coverage=_trusted_coverage(paths),
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
    main_diff = _added_file_diff_map("app/main.py", 11)
    result = processor.process(
        output,
        diff_map=main_diff,
        checked_paths=_paths_of(main_diff),
        rule_names=frozenset(),
        allowed_v2_rules=None,
        coverage=_trusted_coverage(_paths_of(main_diff)),
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
    main_diff = _added_file_diff_map("app/main.py", 13)
    result = processor.process(
        output,
        diff_map=main_diff,
        checked_paths=_paths_of(main_diff),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset({".review/rules.md", "AGENTS.md"}),
        coverage=_trusted_coverage(_paths_of(main_diff)),
    )
    assert len(result.inline) == 4
    # .review/rules.md is allowed
    assert result.inline[0].finding.rule_name == ".review/rules.md"
    # valid service-defaults ID is allowed
    assert result.inline[1].finding.rule_name == "service-defaults:valid-rule_1"
    # malformed service-defaults are nulled out
    assert result.inline[2].finding.rule_name is None
    assert result.inline[3].finding.rule_name is None


@pytest.mark.parametrize(
    ("rule_name", "expected_rule_name"),
    [
        ("service-defaults:E2", "service-defaults:E2"),
        ("service-defaults:FSD1", "service-defaults:FSD1"),
        ("service-defaults:e2", "service-defaults:e2"),
        ("service-defaults:unknown-rule_1", "service-defaults:unknown-rule_1"),
        ("service-defaults:E2\n", None),
        ("service-defaults:FSD1\n", None),
        ("service-defaults:E2\r\n", None),
        ("service-defaults:E2 ", None),
        ("service-defaults: E2", None),
        ("service-defaults:É2", None),
        ("service-defaults:", None),
    ],
)
def test_postprocessor_preserves_findings_and_repairs_only_invalid_service_default_ids(
    rule_name: str, expected_rule_name: str | None
) -> None:
    parsed = output(finding(rule_name=rule_name))

    result = FindingsPostProcessor.from_default_patterns().process(
        parsed,
        diff_map=_default_diff_map(),
        checked_paths=_paths_of(_default_diff_map()),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset(),
    )

    assert len(result.inline) == 1
    assert result.body_only == ()
    assert result.dropped == ()
    assert result.inline[0].position == 0
    assert result.inline[0].finding == parsed.findings[0].model_copy(
        update={"rule_name": expected_rule_name}
    )


def test_collapsed_range_drops_multiline_suggestion() -> None:
    """Cross-hunk range narrowed to one line must not keep an applicable suggestion (#171)."""
    diff_map = _diff_map(anchor_file("src/app.py"))
    result = FindingsPostProcessor.from_default_patterns().process(
        ReviewOutput(
            findings=[
                ReviewFinding(
                    path="src/app.py",
                    line=20,
                    start_line=6,
                    severity="high",
                    category="correctness",
                    title="Spanning hunk",
                    body="Range crosses hunks.",
                    suggestion="x\ny",
                    confidence=0.9,
                    rule_name=None,
                )
            ],
            summary=ReviewSummary(
                problem="Cross-hunk range is invalid.",
                done_well="Tests cover the edge.",
                effort="none",
            ),
        ),
        diff_map=diff_map,
        checked_paths=_paths_of(diff_map),
        rule_names=frozenset(),
        coverage=_trusted_coverage(_paths_of(diff_map)),
    )

    assert len(result.inline) == 1
    assert result.inline[0].finding.start_line is None
    assert result.inline[0].finding.line == 20
    assert result.inline[0].finding.suggestion is None


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
    main_diff = _added_file_diff_map("app/main.py", 10)
    result = processor.process(
        output,
        diff_map=main_diff,
        checked_paths=_paths_of(main_diff),
        rule_names=frozenset(),
        allowed_v2_rules=frozenset({".review/rules.md"}),
        coverage=_trusted_coverage(_paths_of(main_diff)),
    )
    assert len(result.body_only) == 2
    assert result.body_only[0].finding.rule_name == ".review/rules.md"
    assert result.body_only[1].finding.rule_name == "service-defaults:auth-check"

    # 2. Legacy run without allowed_v2_rules: attribution must be cleared in body_only!
    legacy_result = processor.process(
        output,
        diff_map=main_diff,
        checked_paths=_paths_of(main_diff),
        rule_names=frozenset(),
        allowed_v2_rules=None,
        coverage=_trusted_coverage(_paths_of(main_diff)),
    )
    assert len(legacy_result.body_only) == 2
    assert legacy_result.body_only[0].finding.rule_name is None
    assert legacy_result.body_only[1].finding.rule_name is None
