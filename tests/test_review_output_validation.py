"""Review output v2 coordinate and attribution validation."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from app.modules.reviews.application.review_output import (
    InvalidReviewOutput,
    ReviewOutput,
    parse_review_output,
)


def test_validate_v2_coordinates_all_branches() -> None:
    from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_coordinates,
    )

    def make_finding(
        path: str = "app/main.py",
        line: int = 10,
        start_line: int | None = None,
    ) -> ReviewFindingV2:
        return ReviewFindingV2(
            path=path,
            line=line,
            start_line=start_line,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=[
                EvidenceRef(block_id="b1", start_line=start_line or line, end_line=line)
            ],
        )

    # 1. unknown_path
    f = make_finding(path="unknown.py", line=10)
    assert validate_v2_coordinates(f, (), original_files=()) == (False, "unknown_path")

    # 2. File not truncated
    lines = (DiffLine(10, "added", "x=1"), DiffLine(12, "added", "x=2"))
    file_not_truncated = ChangedFile("app/main.py", "modified", lines)

    # Line not in shown
    assert validate_v2_coordinates(
        make_finding(line=15), (file_not_truncated,), original_files=(file_not_truncated,)
    ) == (
        False,
        "line_absent",
    )
    # Start line not in shown
    assert validate_v2_coordinates(
        make_finding(line=12, start_line=11),
        (file_not_truncated,),
        original_files=(file_not_truncated,),
    ) == (
        False,
        "line_absent",
    )

    # 3. File was truncated (total_lines = 100, shown lines 1..10)
    lines_orig_100 = tuple(DiffLine(i, "added", f"x_{i}") for i in range(1, 101))
    file_orig_100 = ChangedFile("app/main.py", "modified", lines_orig_100)
    lines_trunc = tuple(DiffLine(i, "added", f"x_{i}") for i in range(1, 11))
    file_truncated = ChangedFile("app/main.py", "modified", lines_trunc, total_lines=100)

    # Line > total_lines
    assert validate_v2_coordinates(
        make_finding(line=105), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        False,
        "line_absent",
    )
    # Line <= total_lines but not in shown (anchor_not_visible)
    assert validate_v2_coordinates(
        make_finding(line=50), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        False,
        "anchor_not_visible",
    )
    # Truncated file with lines 5..10: line 2 before hunk start is line_absent when not in original
    file_trunc_5_10 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5, 11)),
        total_lines=100,
    )
    file_orig_5_100 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5, 101)),
    )
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=2), (file_trunc_5_10,), original_files=(file_orig_5_100,)
    ) == (
        False,
        "line_absent",
    )
    # Start line <= total_lines and in shown (success)
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=8), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        True,
        None,
    )

    # 3b. With pre-fit original_files provided
    orig_file_5000 = ChangedFile(
        "app/main.py",
        "modified",
        (
            *tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5400)),
            DiffLine(5401, "added", "gap_line"),
            DiffLine(6000, "added", "second_hunk"),
        ),
    )
    shown_file_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5057)),
        total_lines=402,
    )
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=5000),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (True, None)
    assert validate_v2_coordinates(
        make_finding(line=5300),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=5060, start_line=5050),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=5005),
        (),
        original_files=(orig_file_5000,),
    ) == (False, "anchor_not_visible")
    assert validate_v2_coordinates(
        make_finding(line=3),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "line_absent")
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=3),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "line_absent")
    assert validate_v2_coordinates(
        make_finding(line=5401, start_line=5399),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "cross_hunk_range")
    assert validate_v2_coordinates(
        make_finding(line=6000, start_line=5005),
        (shown_file_5000,),
        original_files=(orig_file_5000,),
    ) == (False, "cross_hunk_range")

    # 4. Non-1-based truncated hunk (e.g. lines 5000..5056 shown out of total_lines=400)
    orig_file_full_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5400)),
    )
    file_trunc_5000 = ChangedFile(
        "app/main.py",
        "modified",
        tuple(DiffLine(i, "added", f"x_{i}") for i in range(5000, 5057)),
        total_lines=400,
    )
    assert validate_v2_coordinates(
        make_finding(line=5005), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (True, None)
    assert validate_v2_coordinates(
        make_finding(line=5300), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "anchor_not_visible",
    )
    assert validate_v2_coordinates(
        make_finding(line=3), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5600), (file_trunc_5000,), original_files=(orig_file_full_5000,)
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5005, start_line=3),
        (file_trunc_5000,),
        original_files=(orig_file_full_5000,),
    ) == (
        False,
        "line_absent",
    )
    assert validate_v2_coordinates(
        make_finding(line=5056, start_line=5005),
        (file_trunc_5000,),
        original_files=(orig_file_full_5000,),
    ) == (True, None)

    # 5. RIGHT candidate on a removed-only line number via original_files -> line_absent
    file_removed_and_added = ChangedFile(
        "app/main.py",
        "modified",
        (DiffLine(10, "removed", "old_code"), DiffLine(20, "added", "new_code")),
    )
    file_shown_added_only = ChangedFile(
        "app/main.py",
        "modified",
        (DiffLine(20, "added", "new_code"),),
    )
    # Line 10 exists only as removed line -> line_absent
    assert validate_v2_coordinates(
        make_finding(line=10), (file_shown_added_only,), original_files=(file_removed_and_added,)
    ) == (
        False,
        "line_absent",
    )

    # 6. Multiline range checks: cross_hunk_range
    # Range spans two hunks (break > 1: line 1 and line 3): cross_hunk_range
    file_gap = ChangedFile(
        "app/main.py", "modified", (DiffLine(1, "added", "a"), DiffLine(3, "added", "c"))
    )
    assert validate_v2_coordinates(
        make_finding(line=3, start_line=1), (file_gap,), original_files=(file_gap,)
    ) == (
        False,
        "cross_hunk_range",
    )

    # Range spans two distinct hunks (10..12 and 100..102): cross_hunk_range
    file_two_hunks = ChangedFile(
        "app/main.py",
        "modified",
        (
            DiffLine(10, "added", "a"),
            DiffLine(11, "added", "b"),
            DiffLine(12, "added", "c"),
            DiffLine(100, "added", "x"),
            DiffLine(101, "added", "y"),
            DiffLine(102, "added", "z"),
        ),
    )
    assert validate_v2_coordinates(
        make_finding(line=101, start_line=10), (file_two_hunks,), original_files=(file_two_hunks,)
    ) == (
        False,
        "cross_hunk_range",
    )

    # 7. All valid
    assert validate_v2_coordinates(
        make_finding(line=10, start_line=8), (file_truncated,), original_files=(file_orig_100,)
    ) == (
        True,
        None,
    )


def test_validate_v2_coordinates_drops_an_anchor_on_a_proven_ignored_file_first() -> None:
    from app.modules.reviews.application.prompt_builder import ChangedFile, DiffLine
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_coordinates,
    )

    def make_finding(path: str) -> ReviewFindingV2:
        return ReviewFindingV2(
            path=path,
            line=10,
            start_line=None,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            evidence_refs=[EvidenceRef(block_id="b1", start_line=10, end_line=10)],
        )

    shown = ChangedFile("app/main.py", "modified", (DiffLine(10, "added", "x=1"),))
    ignored = frozenset({"generated/api.py"})

    # CVS §6.2 pass 2: the stage proved the name, so the check precedes the lookup.
    assert validate_v2_coordinates(
        make_finding("generated/api.py"), (shown,), original_files=(shown,), ignored_paths=ignored
    ) == (False, "ignored_file")
    # It also precedes the existence of the line.
    masked = ChangedFile("generated/api.py", "modified", (DiffLine(1, "added", "y=1"),))
    assert validate_v2_coordinates(
        make_finding("generated/api.py"),
        (shown,),
        original_files=(shown, masked),
        ignored_paths=ignored,
    ) == (False, "ignored_file")
    # Another name, even under the same mask, proves nothing.
    assert validate_v2_coordinates(
        make_finding("generated/invented.py"),
        (shown,),
        original_files=(shown,),
        ignored_paths=ignored,
    ) == (False, "unknown_path")
    assert validate_v2_coordinates(
        make_finding("app/main.py"), (shown,), original_files=(shown,), ignored_paths=ignored
    ) == (True, None)


def test_validate_v2_attribution_all_branches() -> None:
    from app.modules.reviews.application.policy_snapshot import (
        CallManifest,
        ManifestBlock,
        SourceRole,
    )
    from app.modules.reviews.application.prompt_builder import PullRequestMeta
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        ReviewFindingV2,
        validate_v2_attribution,
    )

    pr_meta = PullRequestMeta(
        title="PR",
        description=None,
        author="alice",
        source_branch="feature",
        target_branch="main",
        labels=(),
        files_changed=1,
        lines_added=10,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha="head123",
        diff_old_sha="old123",
    )

    def make_finding(
        evidence_refs: list[EvidenceRef],
        rule_name: str | None = None,
    ) -> ReviewFindingV2:
        return ReviewFindingV2(
            path="app/main.py",
            line=10,
            start_line=None,
            severity="high",
            category="correctness",
            title="Title",
            body="Body",
            suggestion=None,
            confidence=0.9,
            rule_name=rule_name,
            evidence_refs=evidence_refs,
        )

    # Block not found
    manifest_empty = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(),
    )
    f = make_finding([EvidenceRef(block_id="missing", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f, manifest_empty, pr_meta=pr_meta)
    assert not ok and "evidence_not_shown" in str(err)

    # Block has invalid role
    block_invalid_role = ManifestBlock(
        block_id="b_invalid",
        source_role=SourceRole.POLICY_DOCUMENT,
        source_kind="repository_rules",
    )
    manifest_inv = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(block_invalid_role,),
    )
    f = make_finding([EvidenceRef(block_id="b_invalid", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f, manifest_inv, pr_meta=pr_meta)
    assert not ok and "evidence_source_disallowed" in str(err)

    # LEFT revision match vs mismatch
    left_block_ok = ManifestBlock(
        block_id="left_ok",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="LEFT",
        revision="old123",
        shown_ranges=((1, 10),),
    )
    left_block_mismatch = ManifestBlock(
        block_id="left_mismatch",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="LEFT",
        revision="wrong_old",
        shown_ranges=((1, 10),),
    )
    right_block_mismatch = ManifestBlock(
        block_id="right_mismatch",
        source_role=SourceRole.DIFF,
        source_kind="source_code",
        side="RIGHT",
        revision="wrong_head",
        shown_ranges=((1, 10),),
    )
    manifest_revs = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(left_block_ok, left_block_mismatch, right_block_mismatch),
    )
    # LEFT mismatch
    f_left_bad = make_finding([EvidenceRef(block_id="left_mismatch", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_left_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "revision_mismatch" in str(err)

    # RIGHT mismatch
    f_right_bad = make_finding([EvidenceRef(block_id="right_mismatch", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_right_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "revision_mismatch" in str(err)

    # LEFT match succeeds
    f_left_ok = make_finding([EvidenceRef(block_id="left_ok", start_line=1, end_line=1)])
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=pr_meta)
    assert ok and err is None

    # Range not shown
    f_range_bad = make_finding([EvidenceRef(block_id="left_ok", start_line=20, end_line=25)])
    ok, err = validate_v2_attribution(f_range_bad, manifest_revs, pr_meta=pr_meta)
    assert not ok and "evidence_not_shown" in str(err)

    # Rule attribution checks when policy/defaults blocks are absent vs present
    for bad_rule in (".review/rules.md", "AGENTS.md", "service-defaults:N1"):
        f_rule_bad = make_finding(
            [EvidenceRef(block_id="left_ok", start_line=1, end_line=1)],
            rule_name=bad_rule,
        )
        ok, err = validate_v2_attribution(f_rule_bad, manifest_revs, pr_meta=pr_meta)
        assert not ok and "evidence_source_disallowed" in str(err)

    manifest_with_all_policies = CallManifest(
        call_id=UUID("00000000-0000-0000-0000-000000000001"),
        stage="review",
        recovery_kind="none",
        blocks=(
            left_block_ok,
            ManifestBlock(
                block_id="policy-rules",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="repository_rules",
                start_line=1,
                end_line=1,
                shown_line_count=1,
                shown_ranges=((1, 1),),
            ),
            ManifestBlock(
                block_id="policy-agents",
                source_role=SourceRole.POLICY_DOCUMENT,
                source_kind="project_conventions_document",
                start_line=1,
                end_line=1,
                shown_line_count=1,
                shown_ranges=((1, 1),),
            ),
            ManifestBlock(
                block_id="service-defaults",
                source_role=SourceRole.SERVICE_DEFAULTS,
                source_kind="service_defaults",
            ),
        ),
    )
    for ok_rule in (".review/rules.md", "AGENTS.md", "service-defaults:N1"):
        f_rule_ok = make_finding(
            [EvidenceRef(block_id="left_ok", start_line=1, end_line=1)],
            rule_name=ok_rule,
        )
        ok, err = validate_v2_attribution(f_rule_ok, manifest_with_all_policies, pr_meta=pr_meta)
        assert ok and err is None

    # LEFT block with diff_old_sha=None fails closed with revision_mismatch
    pr_meta_no_old = PullRequestMeta(
        title="PR",
        description=None,
        author="alice",
        source_branch="feature",
        target_branch="main",
        labels=(),
        files_changed=1,
        lines_added=10,
        lines_removed=0,
        is_draft=False,
        is_fork=False,
        head_sha="head123",
        diff_old_sha=None,
    )
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=pr_meta_no_old)
    assert not ok and "revision_mismatch" in str(err)

    # pr_meta=None skips revision check
    ok, err = validate_v2_attribution(f_left_ok, manifest_revs, pr_meta=None)
    assert ok and err is None


def test_review_output_v2_and_answers_edge_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.modules.reviews.infrastructure.llm.answers as answers_mod
    from app.modules.reviews.application.review_output import (
        EvidenceRef,
        RequirementRef,
        ReviewFindingV2,
        ReviewOutputV2,
        ReviewSummary,
        parse_review_output_v2,
    )
    from app.modules.reviews.infrastructure.llm.answers import (
        InvalidAnswer,
        _bounded,
        parse_review_answer,
        validate_review_answer,
    )

    with pytest.raises(ValidationError, match="start_line must be <= end_line"):
        EvidenceRef(block_id="b1", start_line=5, end_line=3)

    req_ok = RequirementRef(block_id="b1", start_line=1, end_line=2, check_id="N1")
    assert req_ok.check_id == "N1"
    with pytest.raises(ValidationError, match="start_line must be <= end_line"):
        RequirementRef(block_id="b1", start_line=5, end_line=3, check_id=None)

    base_kw: dict[str, Any] = {
        "path": "app/main.py",
        "line": 10,
        "start_line": None,
        "severity": "high",
        "category": "correctness",
        "title": "Valid title",
        "body": "Body",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
        "evidence_refs": [EvidenceRef(block_id="b1", start_line=10, end_line=10)],
    }
    with pytest.raises(ValidationError, match="start_line must be strictly earlier than line"):
        ReviewFindingV2(**{**base_kw, "start_line": 10})
    with pytest.raises(ValidationError, match="title must not end with a period"):
        ReviewFindingV2(**{**base_kw, "title": "Ends with period."})
    with pytest.raises(ValidationError, match="title must be one line"):
        ReviewFindingV2(**{**base_kw, "title": "Line 1\nLine 2"})

    f_high = ReviewFindingV2(**base_kw)
    f_low = ReviewFindingV2(**{**base_kw, "severity": "low", "confidence": 0.5})
    out_v2 = ReviewOutputV2(
        findings=[f_high, f_low],
        summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
    )
    assert len(out_v2.findings) == 2
    with pytest.raises(ValidationError, match="findings must be severity then confidence ordered"):
        ReviewOutputV2(
            findings=[f_low, f_high],
            summary=ReviewSummary(problem="Problem.", done_well="Done well.", effort="small"),
        )

    # parse_review_output strips evidence_refs from v2 dict findings
    raw_with_v2 = {
        "findings": [
            {
                **{k: v for k, v in base_kw.items() if k != "evidence_refs"},
                "evidence_refs": [{"block_id": "b1", "start_line": 10, "end_line": 10}],
            }
        ],
        "summary": {"problem": "Problem.", "done_well": "Done well.", "effort": "small"},
    }
    parsed_v1 = parse_review_output(raw_with_v2)
    assert len(parsed_v1.findings) == 1
    with pytest.raises(InvalidReviewOutput):
        parse_review_output("[]")

    # parse_review_output_v2 str, dict, and error
    assert len(parse_review_output_v2(raw_with_v2).findings) == 1
    assert len(parse_review_output_v2(json.dumps(raw_with_v2)).findings) == 1
    with pytest.raises(InvalidReviewOutput):
        parse_review_output_v2("{bad")

    # answers.py edge branches
    raw_valid_v1 = json.dumps(
        {
            "findings": [],
            "summary": {"problem": "None.", "done_well": "Good.", "effort": "none"},
        }
    )
    assert parse_review_answer(raw_valid_v1).findings == []
    assert "…" in _bounded("x" * 500)

    def _raise_invalid_review_output(_obj: object) -> ReviewOutput:
        try:
            ReviewSummary.model_validate({"problem": "", "done_well": "d", "effort": "none"})
        except ValidationError as ve:
            raise InvalidReviewOutput("bad") from ve
        raise AssertionError("unreachable")

    monkeypatch.setattr(answers_mod, "parse_review_output", _raise_invalid_review_output)
    with pytest.raises(InvalidAnswer):
        validate_review_answer(raw_valid_v1)
