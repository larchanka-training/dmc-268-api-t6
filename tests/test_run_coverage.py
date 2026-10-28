"""Bounded run coverage follows durable evidence and effective policy decisions."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_coverage import (
    RunCoverage,
    project_run_coverage,
    restore_context_coverage,
    snapshot_vcs_file,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from tests.vcs_facts import EMPTY_BLOB_SHA as _EMPTY

_PATCH = "@@ -1 +1 @@\n-old\n+new"


def code_snapshot() -> DiffSnapshot:
    return DiffSnapshot(
        "src/app.py", _PATCH, "d" * 40, additions=1, deletions=1, changes=2, review_patch=_PATCH
    )


def empty_snapshot() -> DiffSnapshot:
    return DiffSnapshot("pkg/__init__.py", None, _EMPTY, "added", omission_reason="empty_file")


def coverage_inputs(
    files: list[DiffSnapshot], *, expected: int | None = None, rules: str = ""
) -> tuple[RunCoverage, DiffMap]:
    pr = VcsPullRequest(
        PullRequestLocator(17, "octo/repo", 7),
        "a" * 40,
        "b" * 40,
        PullRequestMeta(
            "PR",
            None,
            "octo",
            "feature",
            "main",
            (),
            len(files) if expected is None else expected,
            1,
            1,
            False,
            False,
        ),
    )
    diff = build_diff_map(
        pr,
        tuple(snapshot_vcs_file(item) for item in files),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="c" * 40,
    )
    ignore = apply_ignore(IgnoreMatcher(parse_review_ignore(rules)), diff)
    return project_run_coverage(files, diff, ignore=ignore), diff


def coverage(
    files: list[DiffSnapshot], *, expected: int | None = None, rules: str = ""
) -> RunCoverage:
    result, _ = coverage_inputs(files, expected=expected, rules=rules)
    return result


def test_reviewed_code_with_proven_empty_addition_has_complete_coverage() -> None:
    result = coverage([code_snapshot(), empty_snapshot()])

    assert result.status.value == "complete"
    assert result.reviewed_files == 1
    assert [(item.path, item.reason) for item in result.exclusions] == [
        ("pkg/__init__.py", "empty_file")
    ]
    assert result.reasons == ()


@pytest.mark.parametrize(
    ("snapshot", "reason"),
    [
        (
            DiffSnapshot("pkg/__init__.py", None, _EMPTY, "added", omission_reason="missing_patch"),
            "missing_patch",
        ),
        (DiffSnapshot("src/moved.py", None, "d" * 40, "renamed", "src/old.py"), "missing_patch"),
        (DiffSnapshot("src/run.sh", None, "d" * 40, "changed"), "missing_patch"),
        (
            DiffSnapshot(
                "assets/gone.png",
                None,
                None,
                "removed",
                deletions=1,
                changes=1,
                omission_reason="missing_patch",
            ),
            "missing_patch",
        ),
        (DiffSnapshot("assets/huge.png", None, "d" * 40, omission_reason="too_large"), "too_large"),
        (replace(code_snapshot(), additions=2, changes=3), "patch_truncated"),
        (replace(code_snapshot(), patch="@@ -1 +1 @@\n?invalid"), "patch_malformed"),
        (replace(code_snapshot(), summary_only=True), "summary_only"),
        (
            replace(code_snapshot(), review_patch="@@ -1 +1 @@\n-old\n+partial"),
            "review_input_incomplete",
        ),
        (replace(empty_snapshot(), additions=1), "missing_patch"),
    ],
)
def test_mixed_review_retains_named_coverage_blockers(snapshot: DiffSnapshot, reason: str) -> None:
    result = coverage([code_snapshot(), replace(snapshot, filename="other/" + snapshot.filename)])
    assert result.status.value == "partial"
    assert [item.reason for item in result.reasons] == [reason] * (
        2 if snapshot.summary_only else 1
    )
    if snapshot.summary_only:
        assert result.reviewed_files == 0


def test_effective_custom_ignore_precedes_missing_patch_and_tracks_both_rename_names() -> None:
    moved = DiffSnapshot(
        "src/moved.py", None, "d" * 40, "renamed", "private/old.py", omission_reason="missing_patch"
    )
    result = coverage([code_snapshot(), moved], rules="```review-ignore\nprivate/**\n```\n")
    assert result.status.value == "complete"
    assert [(item.path, item.reason) for item in result.exclusions] == [("src/moved.py", "ignored")]


def test_unresolved_identity_is_never_hidden_by_an_empty_or_binary_marker() -> None:
    empty = replace(empty_snapshot(), filename="../escape.py")
    binary = DiffSnapshot("assets/renamed.png", None, status="renamed", omission_reason="binary")
    result = coverage([code_snapshot(), empty, binary], rules="```review-ignore\n**\n```\n")
    assert result.status.value == "partial"
    assert [item.reason for item in result.reasons] == ["unsupported_path", "identity_unresolved"]
    assert all(item.path is None for item in result.reasons)


def test_incomplete_inventory_never_proves_an_excluded_only_change() -> None:
    result = coverage([empty_snapshot()], expected=2)
    assert result.status.value == "partial"
    assert [item.reason for item in result.reasons] == ["diff_inventory_incomplete"]


def test_binary_generated_and_empty_only_have_no_reviewed_text() -> None:
    result = coverage(
        [
            empty_snapshot(),
            DiffSnapshot("assets/a.png", None, omission_reason="binary"),
            DiffSnapshot("generated/custom.py", None, omission_reason="generated"),
        ]
    )
    assert result.status.value == "none"
    assert result.reviewed_files == 0
    assert [item.reason for item in result.exclusions] == ["empty_file", "binary", "generated"]


def test_proven_metadata_only_rename_is_technical_while_absence_is_incomplete() -> None:
    moved = DiffSnapshot(
        "new.py",
        "diff --git a/old.py b/new.py\n"
        "similarity index 100%\nrename from old.py\nrename to new.py\n",
        status="renamed",
        previous_filename="old.py",
    )
    result = coverage([moved])
    assert result.status.value == "none"
    assert [item.reason for item in result.reasons] == ["unsupported_change"]
    assert coverage([replace(moved, patch=None)]).status.value == "partial"


def test_proven_mode_only_change_and_too_large_are_technical_no_text_routes() -> None:
    mode = DiffSnapshot(
        "run.sh",
        "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755\n",
        status="changed",
    )
    result = coverage([mode, DiffSnapshot("large.txt", None, omission_reason="too_large")])
    assert result.status.value == "none"
    assert [item.reason for item in result.reasons] == ["unsupported_change", "too_large"]


def test_independent_nul_binary_proof_precedes_size_without_guessing_from_suffix() -> None:
    binary = DiffSnapshot("assets/no_extension", "\x00", changes=20001, omission_reason="too_large")
    result = coverage([code_snapshot(), binary])
    assert result.status.value == "complete"
    assert [item.reason for item in result.exclusions] == ["binary"]


def test_compact_coverage_bounds_paths_but_retains_exact_counts() -> None:
    missing = [DiffSnapshot(f"missing/{number}.py", None) for number in range(25)]
    result = coverage([code_snapshot(), *missing]).to_json()
    assert len(result["reasons"]) == 20
    assert result["reason_counts"] == {"missing_patch": 25}
    assert result["total_files"] == 26


def test_flagged_rows_restore_coverage_recorded_before_the_summary_only_mark() -> None:
    raw = code_snapshot()
    stored = replace(
        raw,
        review_patch=raw.patch,
        patch=(
            f"diff --git a/{raw.filename} b/{raw.filename}\n"
            f"--- a/{raw.filename}\n"
            f"+++ b/{raw.filename}\n"
            f"{raw.patch}"
        ),
    )
    files = [stored, empty_snapshot()]
    saved, diff = coverage_inputs(files, expected=3)
    flagged = [replace(item, summary_only=True, review_patch=None) for item in files]

    restored = restore_context_coverage(
        {"head_sha": "a" * 40},
        {
            "status": "summary_only",
            "reason": "diff_inventory_incomplete",
            "coverage": saved.to_json(),
            "diff": {"digest": diff.digest, "diff_old_sha": "c" * 40},
        },
        snapshots=flagged,
        diff_map=diff,
    )

    assert restored is not None
    assert restored.reason_counts == {"diff_inventory_incomplete": 1}
    assert restored.reviewed_files == 1
    assert restored.exclusion_counts == {"empty_file": 1}


def test_durable_classification_digest_changes_without_altering_raw_diff_identity() -> None:
    proven = coverage([code_snapshot(), empty_snapshot()])
    unproven = coverage(
        [code_snapshot(), replace(empty_snapshot(), omission_reason="missing_patch")]
    )
    assert proven.diff_digest == unproven.diff_digest
    assert proven.snapshot_digest != unproven.snapshot_digest
    assert proven.status.value == "complete"
    assert unproven.status.value == "partial"
