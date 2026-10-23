"""GitHub pull-request review gateway helpers (#171)."""

from __future__ import annotations

from uuid import UUID

from app.modules.reviews.application.review_output import PublishedFinding, comment_body
from app.modules.reviews.infrastructure.github_review_publication import _posted_inline_finding_ids

_ID_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
_ID_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


def _finding(
    *,
    finding_id: UUID,
    title: str,
    line: int = 40,
    start_line: int | None = None,
) -> PublishedFinding:
    return PublishedFinding(
        path="src/app.py",
        line=line,
        start_line=start_line,
        severity="high",
        category="correctness",
        title=title,
        body="Details.",
        suggestion=None,
        confidence=0.9,
        rule_name=None,
        finding_id=finding_id,
    )


def test_posted_inline_finding_ids_matches_comment_body_on_shared_line() -> None:
    """Two findings on one line: only the comment whose body matches counts as inline."""
    first = _finding(finding_id=_ID_A, title="Missing null check")
    second = _finding(finding_id=_ID_B, title="Unbounded loop")
    comments = [
        {
            "id": 901,
            "path": "src/app.py",
            "line": 40,
            "body": comment_body(second.title, second.body, second.suggestion),
        }
    ]
    posted = _posted_inline_finding_ids(comments, (first, second))
    assert posted == frozenset({_ID_B})


def test_posted_inline_finding_ids_rejects_range_comment_for_single_line_finding() -> None:
    finding = _finding(finding_id=_ID_A, title="Narrow note", line=10, start_line=None)
    comments = [
        {
            "id": 902,
            "path": "src/app.py",
            "line": 10,
            "start_line": 8,
            "body": comment_body(finding.title, finding.body, finding.suggestion),
        }
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset()
