"""GitHub pull-request review gateway helpers (#171)."""

from __future__ import annotations

import asyncio
import json
from typing import Any
from uuid import UUID

import httpx

from app.modules.reviews.application.publish_run_review import (
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubPullRequestReviewGateway,
    _posted_inline_finding_ids,
)
from tests.installation_onboarding_fakes import provider_checkpoint

_HEAD = "a" * 40
_HASH = "f" * 64
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
            "body": "**Unbounded loop**\n\nDetails.",
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
            "body": "**Narrow note**\n\nDetails.",
        }
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset()


def test_posted_inline_finding_ids_requires_the_same_path() -> None:
    finding = _finding(finding_id=_ID_A, title="Wrong file check")
    comments = [
        {"id": 903, "path": "src/other.py", "line": 40, "body": "**Wrong file check**\n\nDetails."}
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset()


def test_posted_inline_finding_ids_requires_the_same_line() -> None:
    finding = _finding(finding_id=_ID_A, title="Wrong line check", line=41)
    comments = [
        {"id": 904, "path": "src/app.py", "line": 40, "body": "**Wrong line check**\n\nDetails."}
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset()


def test_posted_inline_finding_ids_matches_a_ranged_comment_by_start_line() -> None:
    finding = _finding(finding_id=_ID_A, title="Range check", line=10, start_line=8)
    comments = [
        {
            "id": 905,
            "path": "src/app.py",
            "line": 10,
            "start_line": 8,
            "body": "**Range check**\n\nDetails.",
        }
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset({_ID_A})


def test_posted_inline_finding_ids_rejects_single_line_comment_for_ranged_finding() -> None:
    finding = _finding(finding_id=_ID_A, title="Range check", line=10, start_line=8)
    comments = [
        {"id": 906, "path": "src/app.py", "line": 10, "body": "**Range check**\n\nDetails."}
    ]
    assert _posted_inline_finding_ids(comments, (finding,)) == frozenset()


def test_posted_inline_finding_ids_uses_each_comment_at_most_once() -> None:
    """The same body on one line must not be credited to two findings (used check)."""
    first = _finding(finding_id=_ID_A, title="Duplicate text")
    second = _finding(finding_id=_ID_B, title="Duplicate text")
    comments = [
        {"id": 907, "path": "src/app.py", "line": 40, "body": "**Duplicate text**\n\nDetails."}
    ]
    assert _posted_inline_finding_ids(comments, (first, second)) == frozenset({_ID_A})


class _Tokens:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        return "token"


_SUGGESTION_BODY = "**Guard order**\n\nCheck is inverted.\n\n```suggestion\nx = 1\n```"


def test_redelivery_matches_inline_by_the_body_the_first_post_sent() -> None:
    """POST → GET echo on the found-review path: what was sent is what is matched."""
    finding = PublishedFinding(
        path="src/app.py",
        line=3,
        start_line=2,
        severity="high",
        category="correctness",
        title="Guard order",
        body="Check is inverted.",
        suggestion="x = 1",
        confidence=0.9,
        rule_name=None,
        finding_id=_ID_A,
    )
    posted_comments: list[dict[str, Any]] = []
    marker = f"<!-- ai-review findings_hash={_HASH} -->"

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": _HEAD}})
        if request.method == "GET" and path.endswith("/pulls/7/reviews"):
            reviews = [{"id": 55, "body": f"summary body\n\n{marker}"}] if posted_comments else []
            return httpx.Response(200, json=reviews)
        if request.method == "POST" and path.endswith("/reviews"):
            posted_comments.extend(json.loads(request.content)["comments"])
            return httpx.Response(200, json={"id": 55})
        if request.method == "GET" and path.endswith("/reviews/55/comments"):
            return httpx.Response(
                200,
                json=[{**item, "id": 700 + index} for index, item in enumerate(posted_comments)],
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(
        17, "octo/repo", 7, _HEAD, "COMMENT", "summary body", (finding,), _HASH
    )

    fresh = asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint))
    assert fresh == SubmittedReview(55, (700,), posted_inline_finding_ids=frozenset({_ID_A}))
    assert posted_comments == [
        {
            "path": "src/app.py",
            "line": 3,
            "side": "RIGHT",
            "start_line": 2,
            "body": _SUGGESTION_BODY,
        }
    ]

    found = asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint))
    assert found == SubmittedReview(55, (700,), posted_inline_finding_ids=frozenset({_ID_A}))


def test_fresh_post_treats_every_inline_finding_as_posted() -> None:
    """201 on the create is atomic: bodies and coordinates are not compared again."""
    finding = _finding(finding_id=_ID_A, title="Wrong branch", line=40, start_line=39)

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": _HEAD}})
        if request.method == "POST" and path.endswith("/reviews"):
            return httpx.Response(200, json={"id": 77})
        if request.method == "GET" and path.endswith("/reviews/77/comments"):
            # Drifted rendering (\r\n) and a wrong range: nothing byte-matches.
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 908,
                        "path": "src/app.py",
                        "line": 40,
                        "start_line": 39,
                        "body": "**Wrong branch**\r\n\r\nDetails.",
                    }
                ],
            )
        raise AssertionError(f"unexpected {request.method} {path}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(
        17, "octo/repo", 7, _HEAD, "COMMENT", "summary body", (finding,), _HASH
    )

    assert asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint)) == (
        SubmittedReview(77, (908,), posted_inline_finding_ids=frozenset({_ID_A}))
    )
