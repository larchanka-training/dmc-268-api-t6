"""Only current, internally consistent saved coverage may authorize clean."""

from __future__ import annotations

import asyncio
from uuid import UUID

import pytest

from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.run_coverage import restore_run_coverage, snapshot_vcs_file
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from tests.installation_onboarding_fakes import provider_checkpoint
from tests.test_review_context_stage import Vcs
from tests.test_run_coverage import code_snapshot, coverage, empty_snapshot


def diff_for(files: list[DiffSnapshot]) -> DiffMap:
    pr = asyncio.run(
        Vcs(files_changed=len(files)).get_pull_request(
            PullRequestLocator(17, "octo/repo", 7), checkpoint=provider_checkpoint
        )
    )
    return build_diff_map(
        pr,
        tuple(snapshot_vcs_file(item) for item in files),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="c" * 40,
    )


def test_saved_complete_coverage_restores_only_with_matching_head_diff_and_durable_snapshot() -> (
    None
):
    files = [code_snapshot(), empty_snapshot()]
    saved = coverage(files).to_json()
    result = restore_run_coverage(saved, snapshots=files, diff_map=diff_for(files))
    assert result is not None
    assert result.to_json() == saved


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", 2),
        ("version", True),
        ("head_sha", "e" * 40),
        ("snapshot_digest", "0" * 64),
        ("diff_digest", "0" * 64),
        ("status", "unknown"),
        ("total_files", 3),
        ("reviewed_files", True),
        ("reviewed_files", 2),
        ("file_list_complete", False),
        ("reason_counts", {"missing_patch": 1}),
        ("exclusion_counts", {"empty_file": -1}),
        ("exclusions", [{"path": "../escape.py", "reason": "empty_file", "change_type": "added"}]),
        ("exclusions", [{"path": "pkg/__init__.py", "reason": "empty_file", "change_type": {}}]),
        ("exclusions", [{"path": "pkg/__init__.py", "reason": "empty_file", "change_type": []}]),
        ("exclusion_counts", {"unknown": 1}),
    ],
)
def test_stale_or_malformed_classification_is_rejected(field: str, value: object) -> None:
    files = [code_snapshot(), empty_snapshot()]
    saved = coverage(files).to_json() | {field: value}
    assert restore_run_coverage(saved, snapshots=files, diff_map=diff_for(files)) is None


def test_reloaded_compact_partial_coverage_retains_counts_larger_than_display_list() -> None:
    files = [code_snapshot(), *[DiffSnapshot(f"missing/{index}.py", None) for index in range(25)]]
    saved = coverage(files).to_json()
    result = restore_run_coverage(saved, snapshots=files, diff_map=diff_for(files))
    assert result is not None
    assert result.to_json()["reason_counts"] == {"missing_patch": 25}
    assert result.to_json() == saved


@pytest.mark.parametrize("allocation", ["reviewed", "empty_exclusion"])
def test_saved_counts_cannot_reclassify_a_missing_patch_as_reviewed_or_proven_empty(
    allocation: str,
) -> None:
    files = [code_snapshot(), DiffSnapshot("missing.py", None, omission_reason="missing_patch")]
    saved = coverage(files).to_json() | {"status": "complete", "reason_counts": {}, "reasons": []}
    if allocation == "reviewed":
        saved["reviewed_files"] = 2
    else:
        saved["exclusion_counts"] = {"empty_file": 1}
        saved["exclusions"] = [
            {"path": "missing.py", "reason": "empty_file", "change_type": "modified"}
        ]
    assert restore_run_coverage(saved, snapshots=files, diff_map=diff_for(files)) is None


def test_restore_wrapper_warns_for_corrupt_explicit_coverage_without_logging_payload(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    files = [code_snapshot(), empty_snapshot()]
    raw = coverage(files).to_json() | {"version": 2, "secret": "private-token/path.py"}
    with caplog.at_level(logging.WARNING):
        assert (
            restore_run_coverage(
                raw, snapshots=files, diff_map=diff_for(files), run_id=UUID(int=213)
            )
            is None
        )
    assert [record.getMessage() for record in caplog.records] == [
        "Invalid saved run coverage run_id=00000000-0000-0000-0000-0000000000d5"
    ]
    assert "private-token" not in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert (
            restore_run_coverage(
                None, snapshots=files, diff_map=diff_for(files), run_id=UUID(int=213)
            )
            is None
        )
    assert caplog.records == []
