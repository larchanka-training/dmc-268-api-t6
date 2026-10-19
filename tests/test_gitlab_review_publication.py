"""GitLab merge-request discussions posted idempotently by findings hash (#97)."""

from __future__ import annotations

import asyncio
import importlib
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.modules.reviews.application.publish_run_review import (
    PublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.infrastructure.gitlab_review_publication import (
    GitLabMergeRequestDiscussionGateway,
)

HEAD = "a" * 40
OTHER_HEAD = "b" * 40
BASE = "1" * 40
START = "2" * 40
OTHER_BASE = "c" * 40
OTHER_START = "d" * 40
HASH = "f" * 64
BODY = "## Review summary\n\n**Verdict:** blocking"
PROJECT_ID = 42
VERSIONS = "/api/v4/projects/42/merge_requests/7/versions"
DISCUSSIONS = "/api/v4/projects/42/merge_requests/7/discussions"


def _inline_marker(path: str, line: int, finding_id: str) -> str:
    return f"<!-- ai-review findings_hash={HASH} inline={path}:{line} finding={finding_id} -->"


# finding= — first 16 hex of SHA-256(comment_body(...)); literals lock the contract in tests.
INLINE_MARKER_APP = _inline_marker("src/app.py", 3, "6888906fd671f420")
INLINE_MARKER_OTHER = _inline_marker("src/other.py", 9, "3761ff714cafc119")
INLINE_MARKER_TWIN = _inline_marker("src/app.py", 3, "38fff52fc9d1ab4f")
SUMMARY_MARKER = f"<!-- ai-review findings_hash={HASH} summary -->"
INLINE_BODY_APP = f"**Wrong branch**\n\nThe branch is inverted.\n\n{INLINE_MARKER_APP}"
INLINE_BODY_OTHER = (
    "**Narrow check**\n\nThe guard is too wide.\n\n```suggestion\nreturn early\n```\n\n"
    f"{INLINE_MARKER_OTHER}"
)
INLINE_BODY_TWIN = f"**Other defect**\n\nA second problem on the same line.\n\n{INLINE_MARKER_TWIN}"
SUMMARY_BODY = f"{BODY}\n\n{SUMMARY_MARKER}"
POSITION_APP = {
    "position_type": "text",
    "base_sha": BASE,
    "start_sha": START,
    "head_sha": HEAD,
    "old_path": "src/app.py",
    "new_path": "src/app.py",
    "new_line": 3,
}
POSITION_OTHER = {
    "position_type": "text",
    "base_sha": BASE,
    "start_sha": START,
    "head_sha": HEAD,
    "old_path": "src/other.py",
    "new_path": "src/other.py",
    "new_line": 9,
}
# CONTEXT §9.5 fixture patch: context row after hunks is old12/new13 on path `a`.
CONTEXT_PATH = "a"
CONTEXT_OLD_LINE = 12
CONTEXT_NEW_LINE = 13
POSITION_CONTEXT = {
    "position_type": "text",
    "base_sha": BASE,
    "start_sha": START,
    "head_sha": HEAD,
    "old_path": CONTEXT_PATH,
    "new_path": CONTEXT_PATH,
    "old_line": CONTEXT_OLD_LINE,
    "new_line": CONTEXT_NEW_LINE,
}
CONTEXT_TITLE = "Trailing context"
CONTEXT_BODY = "Comment on unchanged context after the edit hunk."
CONTEXT_MARKER = _inline_marker(CONTEXT_PATH, CONTEXT_NEW_LINE, "63801b1f58480743")
CONTEXT_INLINE_BODY = f"**{CONTEXT_TITLE}**\n\n{CONTEXT_BODY}\n\n{CONTEXT_MARKER}"

FINDING_APP = PublishedFinding(
    path="src/app.py",
    line=3,
    start_line=None,
    severity="high",
    category="correctness",
    title="Wrong branch",
    body="The branch is inverted.",
    suggestion=None,
    confidence=0.9,
    rule_name=None,
)
FINDING_OTHER = PublishedFinding(
    path="src/other.py",
    line=9,
    start_line=None,
    severity="medium",
    category="correctness",
    title="Narrow check",
    body="The guard is too wide.",
    suggestion="return early",
    confidence=0.8,
    rule_name=None,
)
FINDING_TWIN = PublishedFinding(
    path="src/app.py",
    line=3,
    start_line=None,
    severity="medium",
    category="correctness",
    title="Other defect",
    body="A second problem on the same line.",
    suggestion=None,
    confidence=0.7,
    rule_name=None,
)
FINDING_CONTEXT = PublishedFinding(
    path=CONTEXT_PATH,
    line=CONTEXT_NEW_LINE,
    start_line=None,
    severity="medium",
    category="correctness",
    title=CONTEXT_TITLE,
    body=CONTEXT_BODY,
    suggestion=None,
    confidence=0.85,
    rule_name=None,
    gitlab_old_line=CONTEXT_OLD_LINE,
)
RENAME_OLD_PATH = "old/a.py"
RENAME_NEW_PATH = "new/a.py"
RENAME_NEW_LINE = 5
POSITION_RENAME = {
    "position_type": "text",
    "base_sha": BASE,
    "start_sha": START,
    "head_sha": HEAD,
    "old_path": RENAME_OLD_PATH,
    "new_path": RENAME_NEW_PATH,
    "new_line": RENAME_NEW_LINE,
}
RENAME_TITLE = "Rename path"
RENAME_BODY = "Comment on a line after rename."
RENAME_MARKER = _inline_marker(RENAME_NEW_PATH, RENAME_NEW_LINE, "d504dcbd0a41763d")
RENAME_INLINE_BODY = f"**{RENAME_TITLE}**\n\n{RENAME_BODY}\n\n{RENAME_MARKER}"
FINDING_RENAME = PublishedFinding(
    path=RENAME_NEW_PATH,
    line=RENAME_NEW_LINE,
    start_line=None,
    severity="medium",
    category="correctness",
    title=RENAME_TITLE,
    body=RENAME_BODY,
    suggestion=None,
    confidence=0.8,
    rule_name=None,
    gitlab_old_path=RENAME_OLD_PATH,
)

# CONTEXT §9.5 fixture patch on path `a`: added new11 and new12 (lines 11–12).
RANGE_PATH = "a"
RANGE_START = 11
RANGE_END = 12
RANGE_LINE_CODE_SHA = "86f7e437faa5a7fce15d1ddcb9eaeaea377667b8"
RANGE_LINE_CODE_START = f"{RANGE_LINE_CODE_SHA}_12_11"
RANGE_LINE_CODE_END = f"{RANGE_LINE_CODE_SHA}_12_12"
POSITION_RANGE = {
    "position_type": "text",
    "base_sha": BASE,
    "start_sha": START,
    "head_sha": HEAD,
    "old_path": RANGE_PATH,
    "new_path": RANGE_PATH,
    "new_line": RANGE_END,
    "line_range": {
        "start": {
            "line_code": RANGE_LINE_CODE_START,
            "type": "new",
            "new_line": RANGE_START,
        },
        "end": {
            "line_code": RANGE_LINE_CODE_END,
            "type": "new",
            "new_line": RANGE_END,
        },
    },
}
RANGE_TITLE = "Validation runs after change"
RANGE_BODY = "Move the check before the mutation."
RANGE_SUGGESTION = "validate()\nnew_value()"
RANGE_MARKER = _inline_marker(RANGE_PATH, RANGE_END, "02cd8756c5122068")
RANGE_INLINE_BODY = (
    f"**{RANGE_TITLE}**\n\n{RANGE_BODY}\n\n"
    f"```suggestion:-1+0\n{RANGE_SUGGESTION}\n```\n\n{RANGE_MARKER}"
)
FINDING_RANGE = PublishedFinding(
    path=RANGE_PATH,
    line=RANGE_END,
    start_line=RANGE_START,
    severity="high",
    category="correctness",
    title=RANGE_TITLE,
    body=RANGE_BODY,
    suggestion=RANGE_SUGGESTION,
    confidence=0.9,
    rule_name=None,
    gitlab_line_range_start_code=RANGE_LINE_CODE_START,
    gitlab_line_range_end_code=RANGE_LINE_CODE_END,
    gitlab_line_range_start_is_added=True,
    gitlab_line_range_end_is_added=True,
)
LONG_RANGE_START = 1
LONG_RANGE_END = 102
LONG_RANGE_TITLE = "Wide span"
LONG_RANGE_BODY = "Too many lines for an applicable suggestion."
LONG_RANGE_SUGGESTION = "replacement"
LONG_RANGE_MARKER = _inline_marker(RANGE_PATH, LONG_RANGE_END, "4bc11bb9227e6f9f")
LONG_RANGE_INLINE_BODY = f"**{LONG_RANGE_TITLE}**\n\n{LONG_RANGE_BODY}\n\n{LONG_RANGE_MARKER}"
FINDING_LONG_RANGE = PublishedFinding(
    path=RANGE_PATH,
    line=LONG_RANGE_END,
    start_line=LONG_RANGE_START,
    severity="medium",
    category="correctness",
    title=LONG_RANGE_TITLE,
    body=LONG_RANGE_BODY,
    suggestion=LONG_RANGE_SUGGESTION,
    confidence=0.8,
    rule_name=None,
    gitlab_line_range_start_code=f"{RANGE_LINE_CODE_SHA}_0_1",
    gitlab_line_range_end_code=f"{RANGE_LINE_CODE_SHA}_0_102",
)


def test_posts_new_side_range_discussion_with_line_range() -> None:
    """CONTEXT §9.5: multiline new-side findings publish line_range (#171)."""
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            note_id = 701 if "position" not in posts[-1] else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_RANGE,),
        findings_hash=HASH,
    )
    result, seen = _submit(respond, submission)

    assert posts == [
        {"body": RANGE_INLINE_BODY, "position": POSITION_RANGE},
        {"body": SUMMARY_BODY},
    ]
    assert posts[0]["position"]["new_line"] == RANGE_END
    assert result == SubmittedReview(701, ())
    _assert_gitlab_only(seen)


def test_multiline_suggestion_uses_gitlab_span_syntax() -> None:
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            note_id = 701 if "position" not in posts[-1] else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_RANGE,),
        findings_hash=HASH,
    )
    _submit(respond, submission)

    assert "```suggestion:-1+0" in posts[0]["body"]
    assert "```suggestion\n" not in posts[0]["body"]


def test_long_range_omits_applicable_suggestion_block() -> None:
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            note_id = 701 if "position" not in posts[-1] else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_LONG_RANGE,),
        findings_hash=HASH,
    )
    _submit(respond, submission)

    assert posts[0]["body"] == LONG_RANGE_INLINE_BODY
    assert "suggestion" not in posts[0]["body"].lower() or "```" not in posts[0]["body"]


def test_posts_context_line_discussion_with_old_and_new_line() -> None:
    """CONTEXT §9.5: context rows publish old_line and new_line without line_range (#171)."""
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            note_id = 701 if "position" not in posts[-1] else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_CONTEXT,),
        findings_hash=HASH,
    )
    result, seen = _submit(respond, submission)

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", VERSIONS),
        ("GET", DISCUSSIONS),
        ("GET", VERSIONS),
        ("POST", DISCUSSIONS),
        ("GET", VERSIONS),
        ("POST", DISCUSSIONS),
    ]
    _assert_gitlab_only(seen)
    assert posts == [
        {"body": CONTEXT_INLINE_BODY, "position": POSITION_CONTEXT},
        {"body": SUMMARY_BODY},
    ]
    assert "line_range" not in posts[0]["position"]
    assert result == SubmittedReview(701, ())


def test_posts_rename_discussion_with_distinct_old_and_new_paths() -> None:
    """CONTEXT §9.5: rename keeps provider old_path and new_path (#171)."""
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            note_id = 701 if "position" not in posts[-1] else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_RENAME,),
        findings_hash=HASH,
    )
    result, seen = _submit(respond, submission)

    assert posts == [
        {"body": RENAME_INLINE_BODY, "position": POSITION_RENAME},
        {"body": SUMMARY_BODY},
    ]
    assert posts[0]["position"]["old_path"] != posts[0]["position"]["new_path"]
    assert result == SubmittedReview(701, ())
    _assert_gitlab_only(seen)


def test_posts_new_side_discussions_then_one_summary() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            payload = _json(request)
            note_id = 701 if "position" not in payload else 601
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id, "body": ""}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    result, seen = _submit(respond)

    assert [(request.method, request.url.path) for request in seen] == [
        ("GET", VERSIONS),
        ("GET", DISCUSSIONS),
        ("GET", VERSIONS),
        ("POST", DISCUSSIONS),
        ("GET", VERSIONS),
        ("POST", DISCUSSIONS),
        ("GET", VERSIONS),
        ("POST", DISCUSSIONS),
    ]
    _assert_gitlab_only(seen)
    posts = [_json(request) for request in seen if request.method == "POST"]
    assert posts == [
        {"body": INLINE_BODY_APP, "position": POSITION_APP},
        {"body": INLINE_BODY_OTHER, "position": POSITION_OTHER},
        {"body": SUMMARY_BODY},
    ]
    assert result == SubmittedReview(701, ())


def test_second_submit_skips_create_when_markers_are_already_listed() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=_listed_with_markers())
        raise AssertionError(f"unexpected {request.method} {request.url}")

    result, seen = _submit(respond)

    assert [request.method for request in seen] == ["GET", "GET"]
    assert result == SubmittedReview(880, ())


def test_markers_on_a_later_page_suppress_a_second_copy() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            if request.url.params.get("page") == "2":
                return httpx.Response(200, json=_listed_with_markers(), headers={"X-Total": "4"})
            return httpx.Response(
                200,
                json=[{"id": "other", "notes": [{"id": 1, "body": "unrelated"}]}],
                headers={"X-Total": "4", "X-Next-Page": "2"},
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    result, seen = _submit(respond)

    assert [request.method for request in seen] == ["GET", "GET", "GET"]
    assert any(request.url.params.get("page") == "2" for request in seen)
    assert result == SubmittedReview(880, ())


def test_listed_inline_marker_is_not_posted_again() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "already",
                        "notes": [{"id": 610, "body": f"existing\n\n{INLINE_MARKER_APP}"}],
                    }
                ],
            )
        if request.method == "POST":
            note_id = 701 if "position" not in _json(request) else 602
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    _result, seen = _submit(respond)

    posts = [_json(request) for request in seen if request.method == "POST"]
    assert posts == [
        {"body": INLINE_BODY_OTHER, "position": POSITION_OTHER},
        {"body": SUMMARY_BODY},
    ]


def test_retry_posts_the_other_finding_that_shares_a_line() -> None:
    posted: list[str] = []
    rejected = False
    submission = ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_APP, FINDING_TWIN),
        findings_hash=HASH,
    )

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal rejected
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=_notes(posted))
        if request.method == "POST":
            body = str(_json(request)["body"])
            if "Other defect" in body and not rejected:
                rejected = True
                return httpx.Response(503, json={"message": "unavailable"})
            posted.append(body)
            note_id = 701 if "position" not in _json(request) else 600 + len(posted)
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": note_id}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    with pytest.raises(PublishError) as raised:
        _submit(respond, submission)
    assert raised.value.kind == "retryable"

    result, _seen = _submit(respond, submission)

    assert result == SubmittedReview(701, ())
    assert posted == [INLINE_BODY_APP, INLINE_BODY_TWIN, SUMMARY_BODY]


@pytest.mark.parametrize(
    "message",
    [
        "position is invalid",
        "line must be part of the diff",
        "line_code is invalid",
        "diff refs do not match",
    ],
)
def test_position_400_raises_coordinates_once(message: str) -> None:
    posts: list[dict[str, Any]] = []
    version_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal version_reads
        if request.method == "GET" and request.url.path == VERSIONS:
            version_reads += 1
            payload = _versions() if version_reads == 1 else _current_versions(HEAD)
            return httpx.Response(200, json=payload)
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        posts.append(_json(request))
        return httpx.Response(400, json={"message": message})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "coordinates"
    assert raised.value.http_status == 400
    assert str(raised.value) == "GitLab rejected review coordinates"
    assert raised.value.unposted == (FINDING_APP, FINDING_OTHER)
    assert len(posts) == 1
    assert posts[0]["position"]["new_line"] == 3
    assert version_reads == 3


def test_moved_head_after_first_inline_is_stale_without_a_third_post() -> None:
    posts = 0
    version_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts, version_reads
        if request.method == "GET" and request.url.path == VERSIONS:
            version_reads += 1
            if version_reads <= 2:
                return httpx.Response(200, json=_versions())
            return httpx.Response(200, json=_current_versions(OTHER_HEAD))
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        posts += 1
        return httpx.Response(201, json={"id": "disc", "notes": [{"id": 600 + posts}]})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "stale_commit"
    assert posts == 1
    assert version_reads == 3


def test_position_400_after_one_inline_reports_only_the_unposted_finding() -> None:
    posts = 0
    version_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts, version_reads
        if request.method == "GET" and request.url.path == VERSIONS:
            version_reads += 1
            payload = _versions() if version_reads == 1 else _current_versions(HEAD)
            return httpx.Response(200, json=payload)
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        posts += 1
        if posts == 1:
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": 601}]})
        return httpx.Response(400, json={"message": "line must be part of the diff"})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "coordinates"
    assert raised.value.unposted == (FINDING_OTHER,)


def test_position_400_after_the_head_moves_is_stale_commit() -> None:
    version_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal version_reads
        if request.method == "GET" and request.url.path == VERSIONS:
            version_reads += 1
            if version_reads <= 2:
                return httpx.Response(200, json=_versions())
            return httpx.Response(200, json=_current_versions(OTHER_HEAD))
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        return httpx.Response(400, json={"message": "position is invalid"})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "stale_commit"
    assert version_reads == 3
    assert [item["head_commit_sha"] for item in _current_versions(OTHER_HEAD)] == [
        OTHER_HEAD,
        HEAD,
    ]


def test_historical_version_is_stale_before_any_post() -> None:
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_current_versions(OTHER_HEAD))
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        if request.method == "POST" and request.url.path == DISCUSSIONS:
            posts.append(_json(request))
            return httpx.Response(201, json={"id": "disc", "notes": [{"id": 601}]})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "stale_commit"
    assert posts == []
    assert [item["head_commit_sha"] for item in _current_versions(OTHER_HEAD)] == [
        OTHER_HEAD,
        HEAD,
    ]


def test_moved_head_raises_stale_commit() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run when the head moved")
        return httpx.Response(
            200,
            json=[
                {
                    "head_commit_sha": OTHER_HEAD,
                    "base_commit_sha": OTHER_BASE,
                    "start_commit_sha": OTHER_START,
                }
            ],
        )

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "stale_commit"


@pytest.mark.parametrize("status", [401, 403])
def test_unauthorized_and_forbidden_raise_forbidden(status: int) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run without a token grant")
        return httpx.Response(status, json={"message": "not allowed"})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "forbidden"
    assert raised.value.http_status == status


@pytest.mark.parametrize("status", [404, 409, 422])
def test_non_retryable_gitlab_client_errors_raise_forbidden(status: int) -> None:
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        posts.append(_json(request))
        return httpx.Response(status, json={"message": "not allowed"})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "forbidden"
    assert raised.value.http_status == status
    assert len(posts) == 1


def test_rate_limited_post_is_retryable_with_retry_after() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                429, json={"message": "rate limit"}, headers={"Retry-After": "15"}
            )
        if request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        return httpx.Response(200, json=[])

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert raised.value.http_status == 429
    assert raised.value.retry_after is not None
    assert raised.value.retry_after.total_seconds() == 15.0


def test_discussion_pagination_limit_is_not_retryable() -> None:
    page = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal page
        if request.method == "POST":
            raise AssertionError("create must not run when discussion pagination is exhausted")
        if request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        page += 1
        return httpx.Response(
            200,
            json=[{"id": f"p{page}", "notes": [{"id": page, "body": "x"}]}],
            headers={"X-Total": "5000", "X-Next-Page": str(page + 1)},
        )

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "forbidden"
    assert str(raised.value) == "GitLab discussion pagination limit exceeded"
    assert page == 20


@pytest.mark.parametrize("path", [VERSIONS, DISCUSSIONS])
def test_server_error_is_retryable(path: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run after a failed read")
        if request.url.path == path:
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(200, json=_versions() if request.url.path == VERSIONS else [])

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert raised.value.http_status == 502


def test_transport_error_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert raised.value.http_status is None
    assert str(raised.value) == "GitLab publication request failed"


def test_non_json_version_list_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run when the version list is not JSON")
        return httpx.Response(200, content=b"not-json")

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert str(raised.value) == "GitLab response was not JSON"


def test_version_missing_shas_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run when diff SHAs are missing")
        return httpx.Response(200, json=[{"head_commit_sha": HEAD, "base_commit_sha": BASE}])

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert str(raised.value) == "GitLab diff version is missing commit SHAs"


@pytest.mark.parametrize(
    "message",
    ["quota exceeded", "deadline exceeded", "the diff is unavailable"],
)
def test_unrelated_400_is_retryable(message: str) -> None:
    posts: list[dict[str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(200, json=[])
        posts.append(_json(request))
        return httpx.Response(400, json={"message": message})

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert raised.value.http_status == 400
    assert str(raised.value) == "GitLab publication request failed"
    assert len(posts) == 1


def test_marker_note_with_non_integer_id_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run when a marker note id is unusable")
        if request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        return httpx.Response(
            200,
            json=[
                {
                    "id": "already",
                    "notes": [{"id": "610", "body": f"existing\n\n{INLINE_MARKER_APP}"}],
                }
            ],
        )

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"
    assert str(raised.value) == "GitLab discussion list is short"


def test_discussion_page_that_does_not_advance_is_retryable() -> None:
    pages: list[str | None] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("create must not run when discussion pages do not advance")
        if request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        pages.append(request.url.params.get("page"))
        return httpx.Response(
            200,
            json=[{"id": "only", "notes": [{"id": 1, "body": "unrelated"}]}],
            headers={"X-Total": "1", "X-Next-Page": "1"},
        )

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert pages == ["1"]
    assert raised.value.kind == "retryable"
    assert str(raised.value) == "GitLab discussion list is short"


def test_short_discussion_list_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            raise AssertionError("a short discussion list must not be treated as empty")
        if request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        return httpx.Response(
            200,
            json=[{"id": "only", "notes": [{"id": 1, "body": "unrelated"}]}],
            headers={"X-Total": "3"},
        )

    with pytest.raises(PublishError) as raised:
        _submit(respond)

    assert raised.value.kind == "retryable"


def test_completed_summary_blocks_another_inline_after_fallback() -> None:
    moved = (
        f"{BODY}\n\n## Inline findings\n"
        "- **Wrong branch** (`src/app.py:3`): The branch is inverted.\n"
        "- **Narrow check** (`src/other.py:9`): The guard is too wide.\n\n"
        f"{SUMMARY_MARKER}"
    )

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == VERSIONS:
            return httpx.Response(200, json=_versions())
        if request.method == "GET" and request.url.path == DISCUSSIONS:
            return httpx.Response(
                200,
                json=[{"id": "summary", "notes": [{"id": 880, "body": moved}]}],
            )
        raise AssertionError("a completed summary must not create another discussion")

    result, seen = _submit(respond)

    assert [request.method for request in seen] == ["GET", "GET"]
    assert result == SubmittedReview(880, ())


def test_gateway_http_calls_do_not_use_a_database_session() -> None:
    module = importlib.import_module("app.modules.reviews.infrastructure.gitlab_review_publication")
    text = Path(module.__file__ or "").read_text(encoding="utf-8")
    assert "sqlalchemy" not in text
    assert "AsyncSession" not in text
    assert "session" not in text


def _submission() -> ReviewSubmission:
    return ReviewSubmission(
        installation_id=17,
        repository_full_name="group/project",
        pr_number=7,
        commit_sha=HEAD,
        event="COMMENT",
        body=BODY,
        findings=(FINDING_APP, FINDING_OTHER),
        findings_hash=HASH,
    )


def _versions() -> list[dict[str, str]]:
    """Current diff version first; GitLab keeps the previous version after it."""
    return _current_versions(HEAD)


def _current_versions(head_sha: str) -> list[dict[str, str]]:
    """Latest version first, with the previous version still in the list."""
    previous = OTHER_HEAD if head_sha == HEAD else HEAD
    shas = {
        HEAD: (BASE, START),
        OTHER_HEAD: (OTHER_BASE, OTHER_START),
    }
    rows: list[dict[str, str]] = []
    for sha in (head_sha, previous):
        base, start = shas[sha]
        rows.append(
            {
                "head_commit_sha": sha,
                "base_commit_sha": base,
                "start_commit_sha": start,
            }
        )
    return rows


def _notes(bodies: list[str]) -> list[dict[str, Any]]:
    return [
        {"id": f"note-{index}", "notes": [{"id": 610 + index, "body": body}]}
        for index, body in enumerate(bodies)
    ]


def _listed_with_markers() -> list[dict[str, Any]]:
    return [
        {"id": "inline-app", "notes": [{"id": 610, "body": f"kept\n\n{INLINE_MARKER_APP}"}]},
        {"id": "inline-other", "notes": [{"id": 611, "body": f"kept\n\n{INLINE_MARKER_OTHER}"}]},
        {"id": "summary", "notes": [{"id": 880, "body": SUMMARY_BODY}]},
    ]


def _submit(
    respond: Callable[[httpx.Request], httpx.Response],
    submission: ReviewSubmission | None = None,
) -> tuple[SubmittedReview, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return respond(request)

    gateway = GitLabMergeRequestDiscussionGateway(
        client=httpx.AsyncClient(
            base_url="https://gitlab.example.test",
            transport=httpx.MockTransport(transport),
        ),
        token="glpat-test",
    )
    result = asyncio.run(gateway.submit_review(submission or _submission(), project_id=PROJECT_ID))
    return result, seen


def _assert_gitlab_only(seen: list[httpx.Request]) -> None:
    assert seen
    for request in seen:
        assert request.headers["PRIVATE-TOKEN"] == "glpat-test"
        assert request.url.host == "gitlab.example.test"
        assert request.url.path.startswith("/api/v4/projects/")
        assert "api.github.com" not in str(request.url)


def _json(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


@pytest.mark.parametrize("fault", ["transport", "body"])
def test_publication_error_contains_no_provider_canary(fault: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if fault == "transport":
            raise httpx.ConnectError("secret-provider-canary", request=request)
        return httpx.Response(503, json={"message": "secret-provider-canary"})

    with pytest.raises(PublishError) as caught:
        _submit(respond)
    assert "secret-provider-canary" not in str(caught.value)
    assert caught.value.__cause__ is None
