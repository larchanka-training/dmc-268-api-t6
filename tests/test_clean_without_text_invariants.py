"""Literal serialized contracts reject contradictory complete classifications."""

from __future__ import annotations

from copy import deepcopy
from uuid import UUID

from app.modules.reviews.application.diff_map import VcsProviderName
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.run_coverage import restore_run_coverage
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from tests.test_clean_without_text_result import finalize
from tests.test_run_coverage import code_snapshot, coverage

_EXPECTED_COVERAGE = {
    "version": 1,
    "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "diff_digest": "87a3b7a375b738e79d41224ec53bf920efc13b03d77874e33299370502d5944d",
    "snapshot_digest": "56f161c0fa3eb738bcec6a2f31dc41c9bdf65be9374d97e19824b5120e9cdc1d",
    "file_list_complete": True,
    "total_files": 2,
    "reviewed_files": 1,
    "status": "partial",
    "exclusion_counts": {},
    "reason_counts": {"too_large": 1},
    "exclusions": [],
    "reasons": [{"path": "huge.txt", "reason": "too_large", "change_type": "modified"}],
}
_EXPECTED_SUMMARY = {
    "problem": (
        "Подтверждённых замечаний нет; Проверка неполная: "
        "Превышен лимит размера, содержимое не проверено (1)."
    ),
    "done_well": "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0.",
    "effort": "none",
}


def test_literal_finalized_coverage_and_summary_contract() -> None:
    snapshots = [code_snapshot(), DiffSnapshot("huge.txt", None, omission_reason="too_large")]
    repository, _ = finalize(coverage(snapshots))
    assert repository.response["coverage"] == _EXPECTED_COVERAGE
    assert repository.response["summary"] == _EXPECTED_SUMMARY
    result = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert result is not None
    assert result.coverage.to_json() == _EXPECTED_COVERAGE
    assert result.summary == _EXPECTED_SUMMARY


def test_saved_result_rejects_literal_complete_with_retained_too_large_reason() -> None:
    raw = deepcopy(_EXPECTED_COVERAGE)
    raw["status"] = "complete"
    value = {
        "coverage": raw,
        "verdict": "clean",
        "summary": _EXPECTED_SUMMARY,
        "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    }
    assert read_saved_review_result(value, head_sha="a" * 40, severities=()) is None


def test_context_restore_rejects_literal_complete_with_retained_too_large_reason() -> None:
    snapshots = [code_snapshot(), DiffSnapshot("huge.txt", None, omission_reason="too_large")]
    diff = diff_map_from_snapshots(
        repository_id=UUID(int=1),
        code_change_number=7,
        head_sha="a" * 40,
        base_sha="b" * 40,
        diff_old_sha="c" * 40,
        snapshots=snapshots,
        provider=VcsProviderName.GITHUB,
    )
    raw = deepcopy(_EXPECTED_COVERAGE)
    raw["status"] = "complete"
    assert restore_run_coverage(raw, snapshots=snapshots, diff_map=diff) is None


def test_saved_complete_rejects_global_technical_reason_even_when_file_counts_balance() -> None:
    value = {
        "coverage": {
            "version": 1,
            "head_sha": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "diff_digest": "87a3b7a375b738e79d41224ec53bf920efc13b03d77874e33299370502d5944d",
            "snapshot_digest": "56f161c0fa3eb738bcec6a2f31dc41c9bdf65be9374d97e19824b5120e9cdc1d",
            "file_list_complete": True,
            "total_files": 2,
            "reviewed_files": 2,
            "status": "complete",
            "exclusion_counts": {},
            "exclusions": [],
            "reason_counts": {"unknown_path": 1},
            "reasons": [{"path": None, "reason": "unknown_path", "change_type": None}],
        },
        "verdict": "clean",
        "summary": {
            "problem": "Подтверждённых замечаний нет.",
            "done_well": (
                "Проанализировано файлов с текстовыми изменениями: 2; исключено файлов: 0."
            ),
            "effort": "none",
        },
        "severity_counts": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
    }
    assert read_saved_review_result(value, head_sha="a" * 40, severities=()) is None
