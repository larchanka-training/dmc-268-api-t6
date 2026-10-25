"""Literal author-facing retained causes, qualified from backend facts (#213)."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import UUID

import pytest

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    check_run_view,
)
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.run_coverage import (
    CoverageStatus,
    RunCoverage,
    unavailable_run_coverage,
)
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsFile,
)
from tests.installation_onboarding_fakes import provider_checkpoint
from tests.test_clean_without_text import EmptyAdditionProvider
from tests.test_clean_without_text_result import finalize
from tests.test_run_coverage import code_snapshot, coverage, empty_snapshot


@pytest.mark.parametrize(
    ("snapshot", "literal"),
    [
        (
            DiffSnapshot(
                "new.py",
                "diff --git a/old.py b/new.py\nsimilarity index 100%\n"
                "rename from old.py\nrename to new.py",
                status="renamed",
                previous_filename="old.py",
            ),
            "Семантика переноса файла (включая импорты) не анализируется",
        ),
        (
            DiffSnapshot("new.py", None, status="renamed", previous_filename="old.py"),
            "Отсутствующий patch не подтверждает неизменность содержимого переноса",
        ),
        (
            DiffSnapshot(
                "run.sh",
                "diff --git a/run.sh b/run.sh\nold mode 100644\nnew mode 100755",
                status="changed",
            ),
            "Изменения прав доступа/типа файла не анализируются",
        ),
        (
            DiffSnapshot("run.sh", None, status="changed"),
            "Отсутствующий patch не подтверждает изменение только прав доступа/типа файла",
        ),
        (
            DiffSnapshot("gone.png", None, status="removed", omission_reason="missing_patch"),
            "Удалённое содержимое недоступно для подтверждения binary",
        ),
        (
            DiffSnapshot(
                "large.py", None, additions=25001, changes=25001, omission_reason="too_large"
            ),
            "Превышен лимит размера, содержимое не проверено",
        ),
        (
            replace(empty_snapshot(), omission_reason="missing_patch"),
            "Нет подтверждения обычного пустого файла",
        ),
    ],
)
def test_seven_retained_causes_have_exact_summary_body_and_check_run_text(
    snapshot: DiffSnapshot,
    literal: str,
) -> None:
    repository, _ = finalize(coverage([code_snapshot(), snapshot]))
    expected = "Подтверждённых замечаний нет; Проверка неполная: " + literal + " (1)."
    assert repository.response["summary"] == {
        "problem": expected,
        "done_well": "Проанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0.",
        "effort": "none",
    }
    assert expected in repository.body
    result = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert result is not None
    view = check_run_view(
        CheckRunReport(
            CheckRunTarget(17, "octo/repo", "a" * 40, UUID(int=213)),
            "succeeded",
            1,
            verdict=result.verdict,
            coverage_status=result.coverage.status.value,
            saved_summary=result.summary,
        )
    )
    assert view.summary == (
        "critical: 0, high: 0, medium: 0, low: 0, info: 0\ninline: 0, в теле ревью: 0\n"
        + expected
        + "\nПроанализировано файлов с текстовыми изменениями: 1; исключено файлов: 0."
    )


@pytest.mark.parametrize(
    "snapshot",
    [
        DiffSnapshot("added.py", None, status="added"),
        replace(
            empty_snapshot(),
            omission_reason="missing_patch",
            patch="diff --git a/pkg/__init__.py b/pkg/__init__.py\nnew file mode 100644",
        ),
        replace(empty_snapshot(), omission_reason="missing_patch", changes=1),
        replace(empty_snapshot(), omission_reason="missing_patch", blob_sha="d" * 40),
    ],
)
def test_unqualified_added_missing_patch_stays_generic(snapshot: DiffSnapshot) -> None:
    repository, _ = finalize(coverage([code_snapshot(), snapshot]))
    assert repository.response["summary"]["problem"] == (
        "Подтверждённых замечаний нет; Проверка неполная: Отсутствует подтверждённый patch (1)."
    )


def test_text_changed_line_limit_does_not_claim_blob_bytes_or_binary() -> None:
    provider = EmptyAdditionProvider(
        (VcsFile("large.py", "modified", "d" * 40, None, 25001, 0, 25001, None),)
    )
    fetched = asyncio.run(
        FetchVcsReviewInput(provider, checkpoint=provider_checkpoint).execute(
            PullRequestLocator(17, "octo/repo", 7), "a" * 40
        )
    )
    assert fetched.omissions[0].reason.value == "too_large"
    snapshot = DiffSnapshot(
        "large.py",
        None,
        additions=25001,
        changes=25001,
        omission_reason=fetched.omissions[0].reason.value,
    )
    repository, _ = finalize(coverage([code_snapshot(), snapshot]))
    assert repository.response["summary"]["problem"] == (
        "Подтверждённых замечаний нет; Проверка неполная: "
        "Превышен лимит размера, содержимое не проверено (1)."
    )
    assert provider.blob_requests == []


def test_unknown_policy_fallback_never_displays_possibly_withheld_paths() -> None:
    result = unavailable_run_coverage(
        [DiffSnapshot("private/secret.py", None, omission_reason="missing_patch")], "a" * 40
    )
    assert result.to_json()["reasons"] == [
        {"path": None, "reason": "classification_unavailable", "change_type": None},
        {"path": None, "reason": "missing_patch", "change_type": "modified"},
    ]
    assert "private" not in result.body_explanation()


def test_qualified_empty_reason_after_twenty_diagnostics_keeps_canonical_text() -> None:
    from app.modules.reviews.application.get_run_diff import DiffSnapshot
    from app.modules.reviews.application.run_coverage import restore_run_coverage
    from tests.test_clean_without_text_coverage_record import diff_for
    from tests.test_clean_without_text_result import finalize
    from tests.test_run_coverage import code_snapshot, coverage
    from tests.vcs_facts import EMPTY_BLOB_SHA

    ordinary = [
        DiffSnapshot(f"a{i:02d}.py", None, "d" * 40, omission_reason="missing_patch")
        for i in range(20)
    ]
    empty = DiffSnapshot(
        "z_empty.py", None, EMPTY_BLOB_SHA, "added", omission_reason="missing_patch"
    )
    snapshots = [code_snapshot(), *ordinary, empty]
    projected = coverage(snapshots)
    raw = projected.to_json()
    assert len(raw["reasons"]) == 20
    assert "z_empty.py" not in str(raw["reasons"])
    expected = (
        "Проверка неполная: Нет подтверждения обычного пустого файла (1); "
        "Отсутствует подтверждённый patch (20)."
    )
    assert projected.summary_problem() == expected
    expected_body = (
        "- ` a00.py `: Отсутствует подтверждённый patch\n"
        "- ` a01.py `: Отсутствует подтверждённый patch\n"
        "- ` a02.py `: Отсутствует подтверждённый patch\n"
        "- ` a03.py `: Отсутствует подтверждённый patch\n"
        "- ` a04.py `: Отсутствует подтверждённый patch\n"
        "- Ещё причин: 16"
    )
    assert projected.body_explanation() == expected_body
    restored = restore_run_coverage(raw, snapshots=snapshots, diff_map=diff_for(snapshots))
    assert restored is not None
    assert restored.summary_problem() == expected
    assert restored.body_explanation() == expected_body
    repository, _ = finalize(restored)
    saved = read_saved_review_result(repository.response, head_sha="a" * 40, severities=())
    assert saved is not None
    assert saved.coverage.summary_problem() == expected
    assert repository.response["summary"]["problem"] == "Подтверждённых замечаний нет; " + expected
    assert len(repository.response["coverage"]["reasons"]) == 20


def test_persisted_empty_reason_variant_cannot_exceed_trusted_snapshot_capacity() -> None:
    from app.modules.reviews.application.get_run_diff import DiffSnapshot
    from app.modules.reviews.application.run_coverage import restore_run_coverage
    from tests.test_clean_without_text_coverage_record import diff_for
    from tests.test_run_coverage import code_snapshot, coverage
    from tests.vcs_facts import EMPTY_BLOB_SHA

    snapshots = [
        code_snapshot(),
        *[
            DiffSnapshot(f"a{i:02d}.py", None, "d" * 40, omission_reason="missing_patch")
            for i in range(20)
        ],
        DiffSnapshot("z_empty.py", None, EMPTY_BLOB_SHA, "added", omission_reason="missing_patch"),
    ]
    raw = coverage(snapshots).to_json()
    raw["reason_variant_counts"] = {"missing_patch:empty_addition": 21}
    assert restore_run_coverage(raw, snapshots=snapshots, diff_map=diff_for(snapshots)) is None


@pytest.mark.parametrize(
    "variants",
    [
        {"missing_patch:empty_addition": True},
        {"missing_patch:empty_addition": 22},
        {"missing_patch:unknown": 1},
        {"missing_patch:empty_addition": 0},
        {"missing_patch:empty_addition": -1},
    ],
)
def test_saved_reason_variant_schema_rejects_invalid_aggregates(variants: object) -> None:
    from tests.test_clean_without_text_result import finalize
    from tests.test_run_coverage import code_snapshot, coverage, empty_snapshot

    repository, _ = finalize(coverage([code_snapshot(), empty_snapshot()]))
    repository.response["coverage"]["reason_variant_counts"] = variants
    assert read_saved_review_result(repository.response, head_sha="a" * 40, severities=()) is None


def test_legacy_aggregate_does_not_infer_empty_qualification_from_added_status() -> None:
    from app.modules.reviews.application.get_run_diff import DiffSnapshot
    from app.modules.reviews.application.run_coverage import restore_run_coverage
    from tests.test_clean_without_text_coverage_record import diff_for
    from tests.test_run_coverage import code_snapshot, coverage
    from tests.vcs_facts import EMPTY_BLOB_SHA

    snapshots = (
        [code_snapshot()]
        + [
            DiffSnapshot(f"a{i:02d}.py", None, "d" * 40, omission_reason="missing_patch")
            for i in range(20)
        ]
        + [
            DiffSnapshot(
                "z_empty.py", None, EMPTY_BLOB_SHA, "added", omission_reason="missing_patch"
            )
        ]
    )
    raw = coverage(snapshots).to_json()
    del raw["reason_variant_counts"]
    restored = restore_run_coverage(raw, snapshots=snapshots, diff_map=diff_for(snapshots))
    assert restored is not None
    assert restored.summary_problem() == "Проверка неполная: Отсутствует подтверждённый patch (21)."


def test_ignored_empty_paths_do_not_enter_semantic_aggregates_or_diagnostics() -> None:
    from app.modules.reviews.application.get_run_diff import DiffSnapshot
    from tests.test_run_coverage import code_snapshot, coverage
    from tests.vcs_facts import EMPTY_BLOB_SHA

    snapshots = (
        [code_snapshot()]
        + [
            DiffSnapshot(f"a{i:02d}.py", None, "d" * 40, omission_reason="missing_patch")
            for i in range(21)
        ]
        + [
            DiffSnapshot(
                "private/secret.py", None, EMPTY_BLOB_SHA, "added", omission_reason="missing_patch"
            )
        ]
    )
    result = coverage(snapshots, rules="```review-ignore\nprivate/**\n```\n")
    assert result.reason_variant_counts == {}
    assert "private" not in str(result.to_json())
    assert "secret" not in str(result.to_json())
    assert result.summary_problem() == "Проверка неполная: Отсутствует подтверждённый patch (21)."


def test_saved_reason_variants_cannot_reclassify_visible_generic_diagnostics() -> None:
    from app.modules.reviews.application.get_run_diff import DiffSnapshot
    from tests.test_clean_without_text_result import finalize
    from tests.test_run_coverage import code_snapshot, coverage
    from tests.vcs_facts import EMPTY_BLOB_SHA

    snapshots = (
        [code_snapshot()]
        + [
            DiffSnapshot(f"a{i:02d}.py", None, "d" * 40, omission_reason="missing_patch")
            for i in range(20)
        ]
        + [
            DiffSnapshot(
                "z_empty.py", None, EMPTY_BLOB_SHA, "added", omission_reason="missing_patch"
            )
        ]
    )
    repository, _ = finalize(coverage(snapshots))
    repository.response["coverage"]["reason_variant_counts"] = {"missing_patch:empty_addition": 21}
    assert read_saved_review_result(repository.response, head_sha="a" * 40, severities=()) is None


def test_no_coverage_summary_uses_the_exact_unchecked_text_label() -> None:
    result = RunCoverage(CoverageStatus.NONE, 0)
    assert result.summary_problem() == "Текстовые изменения не проверены."
    repository, _ = finalize(result)
    assert repository.response["summary"] == {
        "problem": "Подтверждённых замечаний нет; Текстовые изменения не проверены.",
        "done_well": "Проанализировано файлов с текстовыми изменениями: 0; исключено файлов: 0.",
        "effort": "none",
    }
