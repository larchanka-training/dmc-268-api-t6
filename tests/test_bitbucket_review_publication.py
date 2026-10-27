"""Bitbucket Cloud publication: pinned comments and replay-safe markers."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publication_coordinates import (
    enrich_bitbucket_publication_finding,
)
from app.modules.reviews.application.publish_run_review import (
    PublicationIdentity,
    PublishContext,
    PublishError,
    PublishRunReview,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.bitbucket_publication_resources import (
    BitbucketPublicationHttp,
)
from app.modules.reviews.infrastructure.bitbucket_review_publication import (
    BitbucketPullRequestCommentGateway,
)
from tests.test_diff_map import _build, _file
from tests.test_publish_run_review import CheckRuns, Reviews, Store, Trace, Uow, context

RUN = UUID("00000000-0000-4000-8000-000000000333")
REPO = UUID("00000000-0000-4000-8000-000000000123")
SOURCE = UUID("00000000-0000-4000-8000-000000000456")
HEAD = "a" * 40
HASH = "f" * 64


def _finding(*, old_line: int | None = None, suggestion: str | None = None) -> PublishedFinding:
    return PublishedFinding(
        path="src/example.py",
        line=12,
        start_line=None,
        severity="high",
        category="correctness",
        title="Fix guard",
        body="The guard is inverted.",
        suggestion=suggestion,
        confidence=0.9,
        rule_name=None,
        finding_id=UUID("00000000-0000-4000-8000-000000000789"),
        bitbucket_old_line=old_line,
    )


def _submission(*findings: PublishedFinding) -> ReviewSubmission:
    return ReviewSubmission(42, "team/repo", 7, HEAD, "COMMENT", "Review summary", findings, HASH)


def sync_test(test: Callable[[], Coroutine[Any, Any, None]]) -> Callable[[], None]:
    def run() -> None:
        asyncio.run(test())

    return run


class CommentsPort:
    def __init__(self) -> None:
        self.comments: list[dict[str, object]] = []
        self.posts: list[dict[str, object]] = []
        self.head = HEAD
        self.destination = REPO
        self.source = SOURCE
        self.fail_after_post = False
        self.page_size = 2
        self.checkpoints = 0
        self.source_readable = True

    async def checkpoint(self) -> None:
        self.checkpoints += 1

    async def pull_request(self) -> object:
        return {
            "id": 7,
            "state": "OPEN",
            "source": {"commit": {"hash": self.head}, "repository": {"uuid": f"{{{self.source}}}"}},
            "destination": {"repository": {"uuid": f"{{{self.destination}}}"}},
        }

    async def source_commit(self, repository_uuid: UUID, head_sha: str) -> object:
        if not self.source_readable:
            raise PublishError("forbidden", "Bitbucket fork source unavailable")
        return {"hash": head_sha}

    async def list_comments(self, cursor: str | None) -> object:
        page = int(cursor or "0")
        values = self.comments[page : page + self.page_size]
        next_page = page + len(values)
        return {
            "values": values,
            "next": str(next_page) if next_page < len(self.comments) else None,
        }

    async def post_comment(self, payload: dict[str, object]) -> object:
        self.posts.append(payload)
        comment = {"id": len(self.comments) + 10, **payload}
        self.comments.append(comment)
        if self.fail_after_post:
            self.fail_after_post = False
            raise PublishError("retryable", "ambiguous response")
        return comment


@sync_test
async def test_inline_and_summary_replay_after_ambiguous_post() -> None:
    port = CommentsPort()
    port.fail_after_post = True
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    finding = _finding(suggestion="return early")
    submission = _submission(finding)

    with pytest.raises(PublishError, match="ambiguous"):
        await gateway.submit_review(submission)
    result = await gateway.submit_review(submission)
    assert result.review_id == 11
    assert result.posted_inline_finding_ids == frozenset({finding.finding_id})
    assert len(port.posts) == 2
    inline = port.posts[0]
    assert inline["inline"] == {"path": finding.path, "to": 12}
    raw = inline["content"]["raw"]  # type: ignore[index]
    assert "Suggested patch to apply manually" in raw
    assert "```suggestion" in raw
    assert f"run={RUN}" in raw
    assert port.posts[1].get("inline") is None
    assert port.checkpoints >= 2

    replay = await gateway.submit_review(submission)
    assert replay == result
    assert len(port.posts) == 2


@sync_test
async def test_old_side_coordinate_uses_from_only() -> None:
    port = CommentsPort()
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    await gateway.submit_review(_submission(_finding(old_line=9)))
    assert port.posts[0]["inline"] == {"path": "src/example.py", "from": 9}


@sync_test
async def test_multiline_new_side_uses_start_to_on_the_same_hunk() -> None:
    port = CommentsPort()
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    finding = replace(_finding(), start_line=11)
    await gateway.submit_review(_submission(finding))
    assert port.posts[0]["inline"] == {
        "path": "src/example.py",
        "to": 12,
        "start_to": 11,
    }


@sync_test
async def test_head_or_destination_change_stops_before_any_write() -> None:
    for mutation in ("head", "destination"):
        port = CommentsPort()
        setattr(port, mutation, "b" * 40 if mutation == "head" else SOURCE)
        gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
        with pytest.raises(PublishError, match="moved|identity"):
            await gateway.submit_review(_submission(_finding()))
        assert port.posts == []


@sync_test
async def test_unreadable_private_fork_stops_before_comment() -> None:
    port = CommentsPort()
    port.source_readable = False
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    with pytest.raises(PublishError, match="fork source"):
        await gateway.submit_review(_submission(_finding()))
    assert port.posts == []


@sync_test
async def test_second_finding_rechecks_head_before_write_and_resume_keeps_first() -> None:
    port = CommentsPort()
    first = _finding()
    second = replace(first, finding_id=UUID("00000000-0000-4000-8000-000000000790"), line=13)
    original_post = port.post_comment

    async def post_then_move(payload: dict[str, object]) -> object:
        result = await original_post(payload)
        port.head = "b" * 40
        return result

    port.post_comment = post_then_move  # type: ignore[method-assign]
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    with pytest.raises(PublishError, match="moved"):
        await gateway.submit_review(_submission(first, second))
    assert len(port.posts) == 1


@sync_test
async def test_incomplete_comment_pagination_fails_closed() -> None:
    port = CommentsPort()
    port.comments.append({"id": 1, "content": {"raw": "old"}})

    async def bad_page(cursor: str | None) -> object:
        return {"values": [], "next": "0"}

    port.list_comments = bad_page  # type: ignore[method-assign]
    gateway = BitbucketPullRequestCommentGateway(port, run_id=RUN, repository_uuid=REPO)
    with pytest.raises(PublishError, match="pagination"):
        await gateway.submit_review(_submission(_finding()))
    assert port.posts == []


def test_diff_map_derives_right_and_old_side_and_rejects_unknown_anchor() -> None:
    right = _build(_file("src/example.py", patch="@@ -9 +12 @@\n-old\n+new"))
    mapped = enrich_bitbucket_publication_finding(right, _finding())
    assert mapped.bitbucket_old_line is None
    assert mapped.bitbucket_anchor_valid

    removed = _build(
        _file(
            "src/example.py",
            status="removed",
            patch="@@ -9 +0,0 @@\n-old",
            additions=0,
            deletions=1,
        )
    )
    old = enrich_bitbucket_publication_finding(removed, replace(_finding(), line=9))
    assert old.bitbucket_old_line == 9
    assert old.bitbucket_anchor_valid

    missing = enrich_bitbucket_publication_finding(right, replace(_finding(), line=99))
    assert not missing.bitbucket_anchor_valid

    ranged = enrich_bitbucket_publication_finding(
        _build(
            _file(
                "src/example.py",
                patch="@@ -9,2 +11,2 @@\n-old\n+new\n context",
            )
        ),
        replace(_finding(), start_line=11),
    )
    assert ranged.bitbucket_anchor_valid
    cross_hunk = enrich_bitbucket_publication_finding(right, replace(_finding(), start_line=2))
    assert not cross_hunk.bitbucket_anchor_valid


class Credentials:
    def __init__(self) -> None:
        self.active = True
        self.checks = 0

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        self.checks += 1
        if not self.active:
            raise BitbucketTargetInactive()

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        await self.checkpoint(target)
        return BitbucketRequestCredential(target, "canary-token")


@sync_test
async def test_fixed_origin_http_rejects_redirect_and_never_leaks_token() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(302, headers={"Location": "https://example.invalid/steal"})

    target = BoundBitbucketTarget(RUN, RUN, RUN, REPO, 1)
    credentials = Credentials()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        http = BitbucketPublicationHttp(
            client, credentials, target, pr_number=7, checkpoint=lambda: asyncio.sleep(0)
        )
        with pytest.raises(PublishError) as error:
            await http.post_comment({"content": {"raw": "hello"}})
    assert error.value.kind == "forbidden"
    assert len(seen) == 1
    assert seen[0].url.host == "api.bitbucket.org"
    assert seen[0].headers["Authorization"] == "Bearer canary-token"


@sync_test
async def test_publication_http_rejects_foreign_or_filtering_cursor() -> None:
    target = BoundBitbucketTarget(RUN, RUN, RUN, REPO, 1)
    credentials = Credentials()

    def respond(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        http = BitbucketPublicationHttp(
            client, credentials, target, pr_number=7, checkpoint=lambda: asyncio.sleep(0)
        )
        for cursor in (
            "https://example.invalid/2.0/repositories/elsewhere/comments?page=2",
            f"https://api.bitbucket.org/2.0/repositories/%7B%7D/%7B{REPO}%7D/"
            "pullrequests/7/comments?q=id%3D1",
        ):
            with pytest.raises(PublishError, match="unsafe"):
                await http.list_comments(cursor)


@sync_test
async def test_publication_http_rechecks_revocation_before_write() -> None:
    target = BoundBitbucketTarget(RUN, RUN, RUN, REPO, 1)
    credentials = Credentials()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: pytest.fail("request escaped revocation"))
    ) as client:
        http = BitbucketPublicationHttp(
            client, credentials, target, pr_number=7, checkpoint=lambda: asyncio.sleep(0)
        )
        credentials.active = False
        with pytest.raises(RunCancelled, match="installation_unavailable"):
            await http.post_comment({"content": {"raw": "hello"}})


@sync_test
async def test_publication_http_bounds_body_and_preserves_retry_after() -> None:
    target = BoundBitbucketTarget(RUN, RUN, RUN, REPO, 1)
    credentials = Credentials()
    replies = [
        httpx.Response(429, headers={"Retry-After": "17"}),
        httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1)),
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        return replies.pop(0)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        http = BitbucketPublicationHttp(
            client, credentials, target, pr_number=7, checkpoint=lambda: asyncio.sleep(0)
        )
        with pytest.raises(PublishError) as rate:
            await http.pull_request()
        assert rate.value.kind == "retryable"
        assert rate.value.retry_after is not None
        assert rate.value.retry_after.total_seconds() == 17
        with pytest.raises(PublishError, match="too large"):
            await http.pull_request()


@sync_test
async def test_publication_http_accepts_only_pinned_comment_cursor() -> None:
    target = BoundBitbucketTarget(RUN, RUN, RUN, REPO, 1)
    credentials = Credentials()
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"values": []})

    cursor = (
        f"https://api.bitbucket.org/2.0/repositories/%7B%7D/%7B{REPO}%7D/"
        "pullrequests/7/comments?page=2&pagelen=100"
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        http = BitbucketPublicationHttp(
            client, credentials, target, pr_number=7, checkpoint=lambda: asyncio.sleep(0)
        )
        assert await http.list_comments(cursor) == {"values": []}
    assert len(seen) == 1
    assert seen[0].url.params["page"] == "2"
    assert seen[0].headers["Authorization"] == "Bearer canary-token"


def _publication_store(*findings: PublishedFinding) -> Store:
    repository_id = UUID("00000000-0000-4000-8000-000000000101")
    code_change_id = UUID("00000000-0000-4000-8000-000000000102")
    installation_id = UUID("00000000-0000-4000-8000-000000000103")
    workspace_id = UUID("00000000-0000-4000-8000-000000000104")
    base = "b" * 40
    locator = PullRequestLocator(
        42,
        "team/repo",
        7,
        provider="bitbucket",
        origin_provider_installation_id=installation_id,
        repository_external_id=101,
        repository_uuid=REPO,
    )
    return Store(
        context(
            provider="bitbucket",
            installation_id=42,
            repository_full_name="team/repo",
            project_id=101,
            workspace_id=workspace_id,
            vcs_input=RunVcsInput(code_change_id, repository_id, HEAD, base, locator),
            publication_identity=PublicationIdentity(
                installation_id, workspace_id, 42, "bitbucket", repository_id, code_change_id, base
            ),
            findings=findings or context().findings,
        )
    )


@sync_test
async def test_application_routes_bitbucket_without_github_or_check_run() -> None:
    store = _publication_store()
    posts: list[ReviewSubmission] = []
    statuses: list[tuple[UUID, str, str | None]] = []

    class Statuses:
        async def upsert(self, run_id: UUID, run_url: str | None) -> None:
            statuses.append((run_id, store.state, run_url))

    class Gateway:
        async def checkpoint(self) -> None:
            assert store.open_units == 0

        async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
            assert store.open_units == 0
            posts.append(submission)
            return SubmittedReview(71, (), posted_inline_finding_ids=frozenset())

    class Resources:
        @asynccontextmanager
        async def open(
            self, context: PublishContext, *, checkpoint: RunProviderCheckpoint
        ) -> AsyncIterator[Gateway]:
            await checkpoint()
            yield Gateway()

    reviews = Reviews([SubmittedReview(99, (88,))])
    checks = CheckRuns()
    trace = Trace()
    await PublishRunReview(
        uow_factory=lambda: Uow(store),
        reviews=reviews,
        check_runs=checks,
        trace=trace,
        bitbucket_publications=Resources(),
        bitbucket_statuses=Statuses(),
        run_url=lambda _: "https://review.example/runs/one",
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    ).execute(ReviewPublishPointer(store.context.run_id, HEAD, HASH, "COMMENT"))
    assert store.state == "succeeded"
    assert store.completed == [(71, (), (store.context.findings[0].finding_id,))]
    assert len(posts) == 1
    assert reviews.submissions == []
    assert checks.views == []
    assert statuses == [(store.context.run_id, "succeeded", "https://review.example/runs/one")]
    assert trace.records[0][0] == "bitbucket.publish_review"


@sync_test
async def test_bitbucket_publication_failure_updates_failed_status() -> None:
    store = _publication_store()
    observed: list[str] = []

    class Statuses:
        async def upsert(self, run_id: UUID, run_url: str | None) -> None:
            assert run_id == store.context.run_id
            observed.append(store.state)

    class Gateway:
        async def checkpoint(self) -> None:
            return None

        async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
            raise PublishError("forbidden", "status author denied", http_status=403)

    class Resources:
        @asynccontextmanager
        async def open(
            self, context: PublishContext, *, checkpoint: RunProviderCheckpoint
        ) -> AsyncIterator[Gateway]:
            await checkpoint()
            yield Gateway()

    await PublishRunReview(
        uow_factory=lambda: Uow(store),
        reviews=None,
        check_runs=None,
        trace=Trace(),
        bitbucket_publications=Resources(),
        bitbucket_statuses=Statuses(),
        run_url=lambda _: "https://review.example/runs/one",
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    ).execute(ReviewPublishPointer(store.context.run_id, HEAD, HASH, "COMMENT"))
    assert store.state == "failed"
    assert observed == ["failed"]


@sync_test
async def test_partial_coordinate_fallback_marks_only_remaining_findings_in_body() -> None:
    first = _finding()
    second = replace(first, line=13, finding_id=UUID("00000000-0000-4000-8000-000000000790"))
    store = _publication_store(first, second)
    submissions: list[ReviewSubmission] = []

    class Gateway:
        async def checkpoint(self) -> None:
            assert store.open_units == 0

        async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
            submissions.append(submission)
            if len(submissions) == 1:
                raise PublishError("coordinates", "second inline was rejected", unposted=(second,))
            return SubmittedReview(72, (), posted_inline_finding_ids=frozenset())

    class Resources:
        @asynccontextmanager
        async def open(
            self, context: PublishContext, *, checkpoint: RunProviderCheckpoint
        ) -> AsyncIterator[Gateway]:
            await checkpoint()
            yield Gateway()

    await PublishRunReview(
        uow_factory=lambda: Uow(store),
        reviews=None,
        check_runs=None,
        trace=Trace(),
        bitbucket_publications=Resources(),
        now=lambda: datetime(2026, 10, 26, tzinfo=UTC),
    ).execute(ReviewPublishPointer(store.context.run_id, HEAD, HASH, "COMMENT"))
    assert len(submissions) == 2
    assert submissions[1].findings == ()
    assert "src/example.py:13" in submissions[1].body
    assert store.completed == [(72, (), (second.finding_id,))]
