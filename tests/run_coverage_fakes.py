"""Shared coverage inputs for review-result tests."""

from __future__ import annotations

from uuid import UUID

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
