"""Application decoding of durable context bindings without provider metadata."""

from __future__ import annotations

import pytest

from app.modules.reviews.application import run_coverage
from app.modules.reviews.application.get_run_diff import DiffSnapshot


def test_context_binding_decoder_keeps_actual_old_sha_separate_from_policy_base() -> None:
    pins = run_coverage.read_context_coverage_pins(
        {"head_sha": "a" * 40},
        {
            "coverage": {
                "version": 1,
                "head_sha": "a" * 40,
                "file_list_complete": False,
                "diff_digest": "d" * 64,
            },
            "diff": {"diff_old_sha": "c" * 40, "digest": "d" * 64},
        },
        head_sha="a" * 40,
    )
    assert pins is not None
    assert pins.diff_old_sha == "c" * 40
    assert pins.file_list_complete is False


@pytest.mark.parametrize(
    ("head", "complete", "old_sha"),
    [
        ("b" * 40, True, "c" * 40),
        ("a" * 40, 1, "c" * 40),
        ("a" * 40, True, "main"),
    ],
)
def test_context_binding_decoder_rejects_stale_or_malformed_pins(
    head: str,
    complete: object,
    old_sha: str,
) -> None:
    assert (
        run_coverage.read_context_coverage_pins(
            {"head_sha": head},
            {
                "coverage": {
                    "version": 1,
                    "head_sha": head,
                    "file_list_complete": complete,
                    "diff_digest": "d" * 64,
                },
                "diff": {"diff_old_sha": old_sha, "digest": "d" * 64},
            },
            head_sha="a" * 40,
        )
        is None
    )


@pytest.mark.parametrize("old_sha", ["C" * 40, "c" * 64, "c" * 39, "g" * 40, "../" + "c" * 40])
def test_context_binding_requires_canonical_sha1_while_github_path_keeps_its_contract(
    old_sha: str,
) -> None:
    from app.common.application.git_identity import is_canonical_sha1
    from app.common.infrastructure.github_repository_path import (
        InvalidGitHubPathSegment,
        commit_sha_segment,
    )

    assert is_canonical_sha1("c" * 40) is True
    assert is_canonical_sha1(old_sha) is False
    if old_sha in {"C" * 40, "c" * 64}:
        assert commit_sha_segment(old_sha) == old_sha
    else:
        with pytest.raises(InvalidGitHubPathSegment):
            commit_sha_segment(old_sha)


@pytest.mark.parametrize("corrupt", [False, True])
def test_context_restore_hashes_durable_snapshots_once_and_rejects_mismatch(
    corrupt: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from dataclasses import replace

    from tests.test_clean_without_text_coverage_record import diff_for
    from tests.test_run_coverage import code_snapshot, coverage

    snapshots = [code_snapshot()]
    original = coverage(snapshots)
    calls = 0
    digest = run_coverage.snapshot_coverage_digest

    def counted(values: list[DiffSnapshot]) -> str:
        nonlocal calls
        calls += 1
        return digest(values)

    monkeypatch.setattr(run_coverage, "snapshot_coverage_digest", counted)
    diff = diff_for(snapshots)
    changed = [replace(snapshots[0], blob_sha="e" * 40)] if corrupt else snapshots
    result = run_coverage.restore_context_coverage(
        {"head_sha": "a" * 40},
        {"coverage": original.to_json(), "diff": {"diff_old_sha": "c" * 40, "digest": diff.digest}},
        snapshots=changed,
        diff_map=diff,
    )
    assert calls == 1
    assert result.status.value == ("partial" if corrupt else "complete")
    assert result.reason_counts == ({"classification_unavailable": 2} if corrupt else {})


def test_corrupt_context_warns_with_run_id_while_absent_context_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging
    from uuid import UUID

    from tests.test_clean_without_text_coverage_record import diff_for
    from tests.test_run_coverage import code_snapshot

    snapshots = [code_snapshot()]
    with caplog.at_level(logging.WARNING):
        result = run_coverage.restore_context_coverage(
            {"head_sha": "a" * 40},
            {"secret": "private-token/path.py"},
            snapshots=snapshots,
            diff_map=diff_for(snapshots),
            run_id=UUID(int=213),
        )
    assert result.status.value == "partial"
    assert "Invalid saved context coverage" in caplog.text
    assert "run_id=00000000-0000-0000-0000-0000000000d5" in caplog.text
    assert "private-token" not in caplog.text
    caplog.clear()
    run_coverage.restore_context_coverage(
        None,
        None,
        snapshots=snapshots,
        diff_map=diff_for(snapshots),
        run_id=UUID(int=213),
    )
    assert caplog.records == []
