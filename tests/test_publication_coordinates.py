"""GitLab publication coordinate serialization (CONTEXT §9.5, issue #171)."""

from __future__ import annotations

from app.modules.reviews.application.diff_map import DiffMap
from app.modules.reviews.application.publication_coordinates import (
    enrich_gitlab_publication_finding,
    gitlab_text_position,
)
from app.modules.reviews.application.review_output import PublishedFinding
from tests.test_diff_map import _build, _file

_SPEC_BASE = "1111111111111111111111111111111111111111"
_SPEC_START = "2222222222222222222222222222222222222222"
_SPEC_HEAD = "3333333333333333333333333333333333333333"
_VERSION = {
    "base_commit_sha": _SPEC_BASE,
    "start_commit_sha": _SPEC_START,
    "head_commit_sha": _SPEC_HEAD,
}

_SECTION_NINE_FIVE_PATCH = (
    "@@ -10,3 +10,4 @@\n before()\n-old_value()\n+new_value()\n+validate()\n after()"
)
_RANGE_LINE_CODE_SHA = "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8"


def _section_nine_five_diff_map() -> DiffMap:
    return _build(
        _file(
            filename="a",
            patch=_SECTION_NINE_FIVE_PATCH,
            additions=2,
            deletions=1,
        )
    )


def test_gitlab_deleted_row_serializes_old_line_only() -> None:
    """CONTEXT §9.5 removed row old11 — fixture-only; v2 does not publish LEFT (#171)."""
    finding = PublishedFinding(
        path="a",
        line=11,
        start_line=None,
        severity="high",
        category="correctness",
        title="Removed API",
        body="Удаление нарушает показанный контракт вызывающего кода.",
        suggestion=None,
        confidence=0.9,
        rule_name=None,
        gitlab_old_line=11,
        gitlab_old_line_only=True,
    )

    assert gitlab_text_position(_VERSION, finding) == {
        "position_type": "text",
        "base_sha": _SPEC_BASE,
        "start_sha": _SPEC_START,
        "head_sha": _SPEC_HEAD,
        "old_path": "a",
        "new_path": "a",
        "old_line": 11,
    }


def test_enrich_gitlab_finding_from_diff_map_matches_section_nine_five() -> None:
    """D-1: DiffMap fills GitLab position fields without manual gitlab_* on finding (#171)."""
    diff_map = _section_nine_five_diff_map()

    context = enrich_gitlab_publication_finding(
        diff_map,
        PublishedFinding(
            path="a",
            line=13,
            start_line=None,
            severity="medium",
            category="correctness",
            title="Trailing context",
            body="Comment on unchanged context after the edit hunk.",
            suggestion=None,
            confidence=0.85,
            rule_name=None,
        ),
    )
    assert gitlab_text_position(_VERSION, context) == {
        "position_type": "text",
        "base_sha": _SPEC_BASE,
        "start_sha": _SPEC_START,
        "head_sha": _SPEC_HEAD,
        "old_path": "a",
        "new_path": "a",
        "old_line": 12,
        "new_line": 13,
    }

    ranged = enrich_gitlab_publication_finding(
        diff_map,
        PublishedFinding(
            path="a",
            line=12,
            start_line=11,
            severity="high",
            category="correctness",
            title="Validation runs after change",
            body="Move the check before the mutation.",
            suggestion="validate()\nnew_value()",
            confidence=0.9,
            rule_name=None,
        ),
    )
    assert gitlab_text_position(_VERSION, ranged) == {
        "position_type": "text",
        "base_sha": _SPEC_BASE,
        "start_sha": _SPEC_START,
        "head_sha": _SPEC_HEAD,
        "old_path": "a",
        "new_path": "a",
        "new_line": 12,
        "line_range": {
            "start": {
                "line_code": f"{_RANGE_LINE_CODE_SHA}_12_11",
                "type": "new",
                "new_line": 11,
            },
            "end": {
                "line_code": f"{_RANGE_LINE_CODE_SHA}_12_12",
                "type": "new",
                "new_line": 12,
            },
        },
    }


def test_enrich_gitlab_finding_sets_rename_old_path_from_diff_map() -> None:
    diff_map = _build(
        _file(
            filename="new/a.py",
            previous_filename="old/a.py",
            status="renamed",
            patch="@@ -1,3 +1,3 @@\n unchanged\n-old\n+new\n tail",
            additions=1,
            deletions=1,
        )
    )
    enriched = enrich_gitlab_publication_finding(
        diff_map,
        PublishedFinding(
            path="new/a.py",
            line=2,
            start_line=None,
            severity="medium",
            category="correctness",
            title="Rename path",
            body="Comment after rename.",
            suggestion=None,
            confidence=0.8,
            rule_name=None,
        ),
    )
    position = gitlab_text_position(_VERSION, enriched)
    assert position["old_path"] == "old/a.py"
    assert position["new_path"] == "new/a.py"
    assert position["new_line"] == 2
