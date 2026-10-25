"""Saved null, complete and incomplete results have one read projection."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    check_run_view,
)
from app.modules.reviews.application.get_run import GetRunDetail
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.list_pulls import run_verdict
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from tests.test_clean_without_text_result import finalize
from tests.test_run_coverage import code_snapshot, coverage, empty_snapshot
from tests.test_run_detail import Detail, review, run_item


@pytest.mark.parametrize(
    ("other", "expected"),
    [
        (empty_snapshot(), "clean"),
        (replace(empty_snapshot(), omission_reason="missing_patch"), None),
    ],
)
def test_shared_reader_preserves_explicit_null_and_complete_saved_verdict(
    other: DiffSnapshot,
    expected: str | None,
) -> None:
    repository, _ = finalize(coverage([code_snapshot(), other]))
    result = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert result is not None
    assert result.verdict == expected
    assert result.summary == repository.response["summary"]


def test_succeeded_check_run_with_null_verdict_never_falls_back_to_clean() -> None:
    target = CheckRunTarget(17, "octo/repo", "a" * 40, UUID(int=213))
    view = check_run_view(CheckRunReport(target, "succeeded", 1, verdict=None))
    assert view.title == "AI-ревью: проверка неполная"


@pytest.mark.parametrize("field", ["head_sha", "coverage_status", "verdict", "summary"])
def test_shared_reader_rejects_stale_or_malformed_saved_result(field: str) -> None:
    repository, _ = finalize(coverage([code_snapshot(), empty_snapshot()]))
    value = repository.response.copy()
    if field == "head_sha":
        value["coverage"] = value["coverage"] | {"head_sha": "b" * 40}
    elif field == "coverage_status":
        value["coverage"] = value["coverage"] | {"status": "unknown"}
    elif field == "verdict":
        value["verdict"] = "blocking"
    else:
        value["summary"] = {"problem": "raw model diagnostic"}
    assert read_saved_review_result(value, head_sha="a" * 40, severities=()) is None


def test_detail_does_not_promote_a_saved_null_verdict_from_an_empty_finding_set() -> None:
    repository, _ = finalize(coverage([code_snapshot(), DiffSnapshot("missing.py", None)]))
    result = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert result is not None
    stored = replace(review(), summary=result.summary, saved_result=result)
    detail = asyncio.run(GetRunDetail(Detail(run_item(), stored)).execute(UUID(int=213)))
    assert detail is not None and detail.verdict is None
    assert "Проверка неполная" in (detail.review.summary or {})["problem"]


@pytest.mark.parametrize(
    "corruption", ["reviewed", "exclusions", "global", "inventory", "item_type"]
)
def test_saved_partial_result_rejects_impossible_counts_and_malformed_items(
    corruption: str,
) -> None:
    repository, _ = finalize(coverage([code_snapshot(), DiffSnapshot("missing.py", None)]))
    value = deepcopy(repository.response)
    raw = value["coverage"]
    if corruption == "reviewed":
        raw["reviewed_files"] = 2
    elif corruption == "exclusions":
        raw["exclusion_counts"] = {"empty_file": 2}
    elif corruption == "global":
        # A classification diagnostic contributes one global reason, not three.
        raw["reason_counts"] = {"classification_unavailable": 3, "missing_patch": 1}
    elif corruption == "inventory":
        raw["file_list_complete"] = False
        raw["reason_counts"] = {"diff_inventory_incomplete": 2, "missing_patch": 1}
    else:
        raw["reasons"][0]["change_type"] = []
    assert read_saved_review_result(value, head_sha="a" * 40, severities=()) is None


@pytest.mark.parametrize(("severity", "expected"), [("high", "blocking"), ("low", "attention")])
def test_saved_partial_supported_risk_agrees_in_detail_list_and_check_run(
    severity: str,
    expected: str,
) -> None:
    repository, _ = finalize(
        coverage([code_snapshot(), DiffSnapshot("missing.py", None)]),
        [
            {
                "path": "src/app.py",
                "line": 1,
                "start_line": None,
                "severity": severity,
                "category": "security",
                "title": "Unchecked authorization",
                "body": "A remote caller can bypass the permission check.",
                "suggestion": None,
                "confidence": 0.9,
                "rule_name": None,
            }
        ],
    )
    result = read_saved_review_result(
        repository.response, head_sha="a" * 40, severities=(severity,)
    )
    assert result is not None and result.verdict == expected
    stored = replace(review(severity), summary=result.summary, saved_result=result)
    detail = asyncio.run(GetRunDetail(Detail(run_item(), stored)).execute(UUID(int=213)))
    assert detail is not None and detail.verdict == expected
    assert run_verdict("succeeded", False, (severity,), result) == expected
    target = CheckRunTarget(17, "octo/repo", "a" * 40, UUID(int=213))
    view = check_run_view(
        CheckRunReport(
            target,
            "succeeded",
            1,
            verdict=result.verdict,
            coverage_status=result.coverage.status.value,
            saved_summary=result.summary,
        )
    )
    assert view.title == f"AI-ревью: {expected}"
    assert "Проверка неполная" in view.summary
    assert run_verdict("running", False, (severity,), result) is None
    assert run_verdict("succeeded", True, (severity,), result) is None


def test_saved_none_coverage_has_neutral_check_run_without_clean_label() -> None:
    repository, _ = finalize(
        coverage([DiffSnapshot("large.txt", None, omission_reason="too_large")])
    )
    result = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert result is not None and result.verdict is None
    target = CheckRunTarget(17, "octo/repo", "a" * 40, UUID(int=213))
    view = check_run_view(
        CheckRunReport(
            target,
            "succeeded",
            1,
            verdict=result.verdict,
            coverage_status=result.coverage.status.value,
            saved_summary=result.summary,
        )
    )
    assert view.title == "AI-ревью: нет доступных для анализа изменений"
    assert view.conclusion == "neutral"
    assert "Превышен лимит размера, содержимое не проверено" in view.summary


def test_corrupt_saved_result_warns_with_run_id_without_serialized_data(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        assert (
            read_saved_review_result(
                {"verdict": "clean", "coverage": {"secret": "private-token/path.py"}},
                head_sha="a" * 40,
                severities=(),
                run_id=UUID(int=213),
            )
            is None
        )
    assert "Invalid saved review result" in caplog.text
    assert "run_id=00000000-0000-0000-0000-0000000000d5" in caplog.text
    assert "private-token" not in caplog.text
    caplog.clear()
    assert (
        read_saved_review_result(None, head_sha="a" * 40, severities=(), run_id=UUID(int=213))
        is None
    )
    assert caplog.records == []


@pytest.mark.parametrize("legacy", [None, {}, {"verdict": "clean"}, {"verdict": None}])
def test_historical_result_without_coverage_marker_is_quiet(
    legacy: object,
    caplog: pytest.LogCaptureFixture,
) -> None:
    import logging

    with caplog.at_level(logging.WARNING):
        assert (
            read_saved_review_result(legacy, head_sha="a" * 40, severities=(), run_id=UUID(int=213))
            is None
        )
    assert caplog.records == []
