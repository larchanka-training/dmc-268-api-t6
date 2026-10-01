"""The review.publish consumer: T14-T16 and GitHub publication retries (#34)."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self
from uuid import UUID

import httpx
import pytest

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
)
from app.modules.reviews.application.publish_run_review import (
    GitHubPublishError,
    PublishContext,
    PublishRunReview,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.infrastructure.github_review_publication import (
    GitHubCheckRunGateway,
    GitHubPullRequestReviewGateway,
)

RUN = UUID("11111111-1111-1111-1111-111111111111")
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
HEAD = "a" * 40
HASH = "f" * 64
FINDING = PublishedFinding(
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
POINTER = ReviewPublishPointer(RUN, HEAD, HASH, "COMMENT")


def context(**change: Any) -> PublishContext:
    base = PublishContext(
        run_id=RUN,
        state="publishing",
        cancel_requested=False,
        head_sha=HEAD,
        pr_head_sha=HEAD,
        pr_open=True,
        installation_id=17,
        repository_full_name="octo/repo",
        pr_number=7,
        review_body="## Review summary",
        findings=(FINDING,),
        findings_hash=HASH,
    )
    return replace(base, **change)


@dataclass
class Store:
    context: PublishContext
    state: str = "publishing"
    error_code: str | None = None
    completed: list[tuple[int, tuple[int, ...], bool]] = field(default_factory=list)

    async def publish_context(self, run_id: UUID) -> PublishContext | None:
        return replace(self.context, state=self.state)

    async def complete_publication(
        self,
        run_id: UUID,
        *,
        review_id: int,
        comment_ids: tuple[int, ...],
        findings_hash: str,
        moved_to_body: bool,
        now: datetime,
    ) -> str | None:
        assert findings_hash == HASH
        self.completed.append((review_id, comment_ids, moved_to_body))
        self.state = "succeeded"
        return self.state

    async def finish(
        self,
        run_id: UUID,
        *,
        from_state: str,
        worker_id: str | None,
        state: str,
        error_code: str | None,
        error_message: str | None,
        now: datetime,
    ) -> bool:
        assert from_state == "publishing"
        self.state, self.error_code = state, error_code
        return True

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        return CheckRunReport(
            CheckRunTarget(17, "octo/repo", HEAD, RUN),
            self.state,
            1,
            self.error_code,
            verdict="blocking",
        )


class Uow:
    def __init__(self, store: Store) -> None:
        self.runs = store

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


@dataclass
class Trace:
    records: list[tuple[str, dict[str, Any], Any]] = field(default_factory=list)

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        assert duration_ms >= 0
        self.records.append((tool, request, response))


@dataclass
class Reviews:
    results: list[SubmittedReview | GitHubPublishError]
    submissions: list[ReviewSubmission] = field(default_factory=list)

    async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview:
        self.submissions.append(submission)
        result = self.results.pop(0)
        if isinstance(result, GitHubPublishError):
            raise result
        return result


@dataclass
class CheckRuns:
    views: list[CheckRunView] = field(default_factory=list)

    async def upsert(self, target: CheckRunTarget, view: CheckRunView) -> None:
        self.views.append(view)


def run(
    store: Store, reviews: Reviews, sleeps: list[float] | None = None
) -> tuple[Trace, CheckRuns]:
    trace, check_runs = Trace(), CheckRuns()
    pauses = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    asyncio.run(
        PublishRunReview(
            uow_factory=lambda: Uow(store),
            reviews=reviews,
            check_runs=check_runs,
            trace=trace,
            sleep=sleep,
            now=lambda: NOW,
        ).execute(POINTER)
    )
    return trace, check_runs


def test_publication_succeeds_with_one_trace_record_and_a_neutral_check_run() -> None:
    store = Store(context())
    reviews = Reviews([SubmittedReview(501, (9001,))])
    trace, check_runs = run(store, reviews)

    assert store.state == "succeeded"
    assert store.completed == [(501, (9001,), False)]
    assert reviews.submissions[0].commit_sha == HEAD
    assert reviews.submissions[0].event == "COMMENT"
    assert trace.records == [
        (
            "github.publish_review",
            {
                "head_sha": HEAD,
                "findings_hash": HASH,
                "review_event": "COMMENT",
                "inline_count": 1,
                "try": 1,
            },
            {"github_review_id": 501},
        )
    ]
    assert [(v.status, v.conclusion, v.title) for v in check_runs.views] == [
        ("completed", "neutral", "AI-ревью: blocking")
    ]


def test_coordinate_422_moves_inline_findings_into_the_body_once() -> None:
    store = Store(context())
    reviews = Reviews(
        [
            GitHubPublishError("coordinates", "line outside diff", http_status=422),
            SubmittedReview(7, ()),
        ]
    )
    trace, _ = run(store, reviews)

    assert store.state == "succeeded"
    assert store.completed == [(7, (), True)]
    assert reviews.submissions[1].findings == ()
    assert "`src/app.py:3`" in reviews.submissions[1].body
    assert [record[1]["try"] for record in trace.records] == [1, 2]


def test_stale_commit_422_cancels_as_superseded() -> None:
    store = Store(context())
    run(store, Reviews([GitHubPublishError("stale_commit", "commit_id", http_status=422)]))
    assert (store.state, store.error_code) == ("cancelled", "superseded")


def test_forbidden_fails_without_retry() -> None:
    store = Store(context())
    trace, check_runs = run(
        store, Reviews([GitHubPublishError("forbidden", "no", http_status=403)])
    )
    assert (store.state, store.error_code) == ("failed", "github_forbidden")
    assert len(trace.records) == 1
    assert check_runs.views[-1].conclusion == "neutral"


def test_server_errors_retry_three_times_with_spec_pauses_then_fail() -> None:
    store = Store(context())
    error = GitHubPublishError("retryable", "bad gateway", http_status=502)
    sleeps: list[float] = []
    trace, _ = run(store, Reviews([error, error, error, error]), sleeps)

    assert (store.state, store.error_code) == ("failed", "github_publish_failed")
    assert sleeps == [2.0, 8.0, 30.0]
    assert len(trace.records) == 4
    assert trace.records[0][2] == {"error": {"http_status": 502, "message": "bad gateway"}}


def test_retry_after_up_to_sixty_seconds_replaces_the_pause() -> None:
    store = Store(context())
    limited = GitHubPublishError(
        "retryable", "secondary limit", http_status=403, retry_after=timedelta(seconds=45)
    )
    sleeps: list[float] = []
    run(store, Reviews([limited, SubmittedReview(1, (2,))]), sleeps)
    assert sleeps == [45.0]
    assert store.state == "succeeded"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"pr_open": False}, "pr_closed"),
        ({"pr_head_sha": "c" * 40}, "superseded"),
        ({"cancel_requested": True}, "cancelled_by_user"),
    ],
)
def test_publisher_cancels_before_post(change: dict[str, Any], reason: str) -> None:
    store = Store(context(**change))
    reviews = Reviews([])
    run(store, reviews)
    assert (store.state, store.error_code) == ("cancelled", reason)
    assert reviews.submissions == []


def test_redelivery_after_success_does_not_post_again() -> None:
    store = Store(context(), state="succeeded")
    reviews = Reviews([])
    trace, _ = run(store, reviews)
    assert reviews.submissions == [] and trace.records == []


def _json(request: httpx.Request) -> Any:
    return json.loads(request.content) if request.content else None


def test_github_review_gateway_reuses_a_review_already_posted_for_the_hash() -> None:
    calls: list[tuple[str, str]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.url.path.endswith("/pulls/7/reviews") and request.method == "GET":
            return httpx.Response(
                200, json=[{"id": 55, "body": f"x\n\n<!-- ai-review findings_hash={HASH} -->"}]
            )
        if request.url.path.endswith("/reviews/55/comments"):
            return httpx.Response(200, json=[{"id": 900}])
        raise AssertionError(f"unexpected {request.method} {request.url}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)

    assert asyncio.run(gateway.submit_review(submission)) == SubmittedReview(55, (900,))
    assert ("POST", "/repos/octo/repo/pulls/7/reviews") not in calls


@pytest.mark.parametrize(
    ("status", "body", "headers", "kind"),
    [
        (
            422,
            {"message": "Unprocessable", "errors": ["Line must be part of the diff"]},
            {},
            "coordinates",
        ),
        (422, {"message": "commit_id is not part of the pull request"}, {}, "stale_commit"),
        (403, {"message": "Resource not accessible"}, {}, "forbidden"),
        (404, {"message": "Not Found"}, {}, "forbidden"),
        (403, {"message": "secondary"}, {"Retry-After": "30"}, "retryable"),
        (502, {"message": "Bad Gateway"}, {}, "retryable"),
    ],
)
def test_github_review_gateway_classifies_publication_errors(
    status: int, body: dict[str, Any], headers: dict[str, str], kind: str
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json=[])
        payload = _json(request)
        assert payload["commit_id"] == HEAD
        assert payload["comments"][0]["side"] == "RIGHT"
        return httpx.Response(status, json=body, headers=headers)

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)
    with pytest.raises(GitHubPublishError) as raised:
        asyncio.run(gateway.submit_review(submission))
    assert raised.value.kind == kind


def test_check_run_gateway_creates_once_then_updates_the_same_check_run() -> None:
    created: dict[str, Any] = {}
    requests: list[tuple[str, str, Any]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path, _json(request)))
        if request.method == "GET":
            runs = [{"id": 31, "external_id": str(RUN)}] if created else []
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        if request.method == "POST":
            created.update(_json(request))
            return httpx.Response(201, json={"id": 31})
        return httpx.Response(200, json={"id": 31})

    gateway = GitHubCheckRunGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    target = CheckRunTarget(17, "octo/repo", HEAD, RUN)
    asyncio.run(gateway.upsert(target, CheckRunView("in_progress", None, "t", "попытка 1 из 3")))
    asyncio.run(gateway.upsert(target, CheckRunView("completed", "neutral", "t", "done")))

    methods = [(method, path) for method, path, _ in requests]
    assert methods == [
        ("GET", f"/repos/octo/repo/commits/{HEAD}/check-runs"),
        ("POST", "/repos/octo/repo/check-runs"),
        ("GET", f"/repos/octo/repo/commits/{HEAD}/check-runs"),
        ("PATCH", "/repos/octo/repo/check-runs/31"),
    ]
    assert created["external_id"] == str(RUN)
    assert created["status"] == "in_progress" and "conclusion" not in created
    assert requests[3][2]["conclusion"] == "neutral"


class _Tokens:
    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == 17
        return "token"


def test_failed_comment_read_is_retried_and_finds_the_posted_review() -> None:
    posts: list[str] = []
    comment_reads: list[int] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST":
            posts.append(path)
            return httpx.Response(200, json={"id": 55})
        if path.endswith("/pulls/7/reviews"):
            body = f"x\n\n<!-- ai-review findings_hash={HASH} -->"
            return httpx.Response(200, json=[{"id": 55, "body": body}] if posts else [])
        comment_reads.append(1)
        if len(comment_reads) == 1:
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(200, json=[{"id": 900}])

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)

    with pytest.raises(GitHubPublishError) as raised:
        asyncio.run(gateway.submit_review(submission))
    assert raised.value.kind == "retryable"
    assert asyncio.run(gateway.submit_review(submission)) == SubmittedReview(55, (900,))
    assert len(posts) == 1
