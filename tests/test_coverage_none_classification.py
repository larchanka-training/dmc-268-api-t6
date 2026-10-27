"""An unsupported path or an unresolved identity is a technical skip on the snapshot path.

CVS §2.2: a complete inventory with at least one T (``too_large``, ``unsupported_change``,
``unsupported_path``, ``identity_unresolved``) and otherwise only exclusions has coverage
none (#130 F5, DD1). Only the snapshot path classifies the last two so; a legacy (≤ v3) run
keeps them incomplete, byte-identical to #283.
"""

from __future__ import annotations

import asyncio
import logging
from uuid import UUID

import pytest

from app.modules.reviews.application.coverage_schema import CoverageStatus, valid_coverage_status
from app.modules.reviews.application.diff_map import DiffMap, VcsProviderName, build_diff_map
from app.modules.reviews.application.findings_post_processor import summary_from_result
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.review_context import NoReviewableText
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
from app.modules.reviews.application.saved_review_result import read_saved_review_result
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsPullRequest
from app.modules.reviews.application.verdict import severity_counts
from tests.test_review_context_stage import Trace, Vcs
from tests.test_review_context_stage import _stage as _context_stage
from tests.test_review_path import _store
from tests.test_run_coverage import code_snapshot, empty_snapshot

_RUN = UUID(int=2)
_HEAD = "a" * 40
_PATCH = "@@ -1 +1 @@\n-old\n+new"


def unsupported_path() -> DiffSnapshot:
    """A text change under a name outside the path contract (a backslash)."""
    return DiffSnapshot(
        "src\\bad.py", _PATCH, "d" * 40, additions=1, deletions=1, changes=2, review_patch=None
    )


def unresolved_identity() -> DiffSnapshot:
    """A rename the provider sent without the previous name."""
    return DiffSnapshot("src/moved.py", None, "d" * 40, "renamed", None)


def metadata_only_rename() -> DiffSnapshot:
    """A pure rename with its exact metadata patch: ``unsupported_change``.

    Unreachable live: neither adapter sends such a patch, a pure rename has none.
    """
    return DiffSnapshot(
        "src/new.py",
        "diff --git a/src/old.py b/src/new.py\nsimilarity index 100%\n"
        "rename from src/old.py\nrename to src/new.py\n",
        "d" * 40,
        "renamed",
        "src/old.py",
    )


def _diff(files: list[DiffSnapshot], *, expected: int | None = None) -> DiffMap:
    pr = VcsPullRequest(
        PullRequestLocator(17, "octo/repo", 7),
        _HEAD,
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
    return build_diff_map(
        pr,
        tuple(snapshot_vcs_file(item) for item in files),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha="c" * 40,
    )


def project(
    files: list[DiffSnapshot],
    *,
    snapshot_path: bool,
    expected: int | None = None,
    rules: str = "",
) -> RunCoverage:
    diff = _diff(files, expected=expected)
    ignore = apply_ignore(IgnoreMatcher(parse_review_ignore(rules)), diff)
    return project_run_coverage(files, diff, ignore=ignore, snapshot_path=snapshot_path)


def _reasons(result: RunCoverage) -> list[tuple[str | None, str]]:
    return [(item.path, item.reason) for item in result.reasons]


def test_an_unsupported_path_only_change_is_none_only_on_the_snapshot_path() -> None:
    snapshot_path = project([unsupported_path()], snapshot_path=True)
    legacy = project([unsupported_path()], snapshot_path=False)

    assert snapshot_path.status is CoverageStatus.NONE
    assert snapshot_path.reviewed_files == 0
    assert _reasons(snapshot_path) == [(None, "unsupported_path")]
    # The default is the legacy classification, as before #130 F5.
    assert legacy.status is CoverageStatus.PARTIAL
    assert _reasons(legacy) == [(None, "unsupported_path")]
    default_diff = _diff([unsupported_path()])
    assert project_run_coverage([unsupported_path()], default_diff).status is CoverageStatus.PARTIAL


@pytest.mark.parametrize(
    ("version", "status"),
    [("v2", "partial"), ("v3", "partial"), ("v4", "none")],
    ids=["v2", "v3", "v4"],
)
def test_the_stage_classifies_an_unsupported_path_by_the_path_of_the_run(
    version: str, status: str
) -> None:
    trace = Trace()
    stage = _context_stage(
        trace, store=_store(version), snapshots=[unsupported_path()], vcs=Vcs(files_changed=1)
    )

    if version == "v4":
        # The snapshot path then ends the attempt without a model call (the route, T4).
        with pytest.raises(NoReviewableText):
            asyncio.run(stage.run(_RUN))
    else:
        assert asyncio.run(stage.run(_RUN)) is not None

    [coverage] = [
        response["coverage"] for tool, _, response in trace.steps if tool == "context.build"
    ]
    assert (stage.snapshot_for_llm is not None) is (version == "v4")
    assert coverage["status"] == status
    assert coverage["reason_counts"] == {"unsupported_path": 1}
    assert coverage["reviewed_files"] == 0


def test_an_unresolved_identity_only_change_is_none_on_the_snapshot_path() -> None:
    result = project([unresolved_identity()], snapshot_path=True)

    assert result.status is CoverageStatus.NONE
    assert _reasons(result) == [(None, "identity_unresolved")]
    assert project([unresolved_identity()], snapshot_path=False).status is CoverageStatus.PARTIAL


@pytest.mark.parametrize(
    ("other", "rules", "exclusion"),
    [
        (code_snapshot(), "```review-ignore\n**/*\n```\n", "ignored"),
        (DiffSnapshot("assets/logo.png", None, omission_reason="binary"), "", "binary"),
    ],
    ids=["masked", "binary"],
)
def test_an_unsupported_path_beside_exclusions_only_is_technical_none(
    other: DiffSnapshot, rules: str, exclusion: str
) -> None:
    result = project([unsupported_path(), other], snapshot_path=True, rules=rules)

    assert result.status is CoverageStatus.NONE
    assert _reasons(result) == [(None, "unsupported_path")]
    assert [item.reason for item in result.exclusions] == [exclusion]
    assert result.all_excluded is False
    assert result.technical_only is True


@pytest.mark.parametrize(
    ("other", "expected", "reason_counts"),
    [
        (
            DiffSnapshot("other/missing.py", None, "e" * 40),
            None,
            {"unsupported_path": 1, "missing_patch": 1},
        ),
        (code_snapshot(), None, {"unsupported_path": 1}),
        (None, 2, {"diff_inventory_incomplete": 1, "unsupported_path": 1}),
    ],
    ids=["missing-patch", "reviewed-code", "incomplete-inventory"],
)
def test_an_unsupported_path_beside_an_incomplete_or_reviewed_file_stays_partial(
    other: DiffSnapshot | None, expected: int | None, reason_counts: dict[str, int]
) -> None:
    files = [unsupported_path()] if other is None else [unsupported_path(), other]

    result = project(files, snapshot_path=True, expected=expected)

    assert result.status is CoverageStatus.PARTIAL
    assert result.reason_counts == reason_counts
    assert result.technical_only is False


def _saved(result: RunCoverage) -> dict[str, object]:
    """The ``review.postprocess`` response of a run without findings."""
    return {
        "verdict": None,
        "coverage": result.to_json(),
        "summary": summary_from_result((), (), result).model_dump(),
        "severity_counts": severity_counts(()),
    }


@pytest.mark.parametrize(
    "files",
    [
        [unsupported_path()],
        [unresolved_identity()],
        [unsupported_path(), empty_snapshot()],
        [metadata_only_rename()],
    ],
    ids=["unsupported-path", "identity", "with-exclusion", "unsupported-change"],
)
def test_a_saved_technical_none_restores_at_both_persistence_boundaries(
    files: list[DiffSnapshot], caplog: pytest.LogCaptureFixture
) -> None:
    saved = project(files, snapshot_path=True)
    assert saved.status is CoverageStatus.NONE
    diff = _diff(files)

    with caplog.at_level(logging.WARNING):
        restored = restore_context_coverage(
            {"head_sha": _HEAD},
            {
                "coverage": saved.to_json(),
                "diff": {"diff_old_sha": "c" * 40, "digest": diff.digest},
            },
            snapshots=files,
            diff_map=diff,
            run_id=_RUN,
        )
        result = read_saved_review_result(_saved(saved), head_sha=_HEAD, severities=(), run_id=_RUN)

    assert restored.to_json() == saved.to_json()
    assert result is not None
    assert result.verdict is None
    assert result.coverage.status is CoverageStatus.NONE
    assert result.coverage.to_json() == saved.to_json()
    assert caplog.records == []


def _valid_none(reasons: dict[str, int]) -> bool:
    return valid_coverage_status(
        CoverageStatus.NONE,
        reviewed=0,
        total=1,
        complete=True,
        reasons=reasons,
        exclusions={},
    )


def test_the_validator_accepts_none_only_with_technical_reasons() -> None:
    assert _valid_none({"unsupported_path": 1}) is True
    assert _valid_none({"identity_unresolved": 1}) is True
    assert _valid_none({"too_large": 1}) is True
    assert _valid_none({"unsupported_change": 1}) is True
    assert _valid_none({"missing_patch": 1}) is False
    assert _valid_none({"summary_only": 1}) is False


@pytest.mark.parametrize(
    ("files", "status"),
    [
        ([empty_snapshot()], "none"),
        ([code_snapshot(), DiffSnapshot("other/missing.py", None)], "partial"),
        ([code_snapshot()], "complete"),
    ],
    ids=["exclusions-only", "partial", "complete"],
)
def test_only_a_none_with_a_technical_reason_is_technical_only(
    files: list[DiffSnapshot], status: str
) -> None:
    result = project(files, snapshot_path=True)

    assert result.status.value == status
    assert result.technical_only is False
