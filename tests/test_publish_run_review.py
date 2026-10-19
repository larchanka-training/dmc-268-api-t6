"""The review.publish consumer: T14-T16 and GitHub publication retries (#34)."""

from __future__ import annotations

import asyncio
import json
import logging
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
    RunProviderCheckpoint,
)
from app.modules.reviews.application.publish_run_review import (
    PublishContext,
    PublishError,
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
from tests.installation_onboarding_fakes import provider_checkpoint

RUN = UUID("11111111-1111-1111-1111-111111111111")
FINDING_ID = UUID("22222222-2222-2222-2222-222222222222")
OTHER_FINDING_ID = UUID("33333333-3333-3333-3333-333333333333")
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
    finding_id=FINDING_ID,
)
POINTER = ReviewPublishPointer(RUN, HEAD, HASH, "COMMENT")
PROJECT_ID = 5150
STORED_BODY = "\n".join(
    [
        "## Review summary",
        "",
        "**Verdict:** blocking",
        "**Risk summary:** critical: 0, high: 1, medium: 0, low: 0, info: 0",
        "**Checked files:**",
        "- src/app.py",
        "",
        "**Main issue:** inverted branch",
    ]
)


def context(**change: Any) -> PublishContext:
    base = PublishContext(
        run_id=RUN,
        state="publishing",
        cancel_requested=False,
        head_sha=HEAD,
        pr_head_sha=HEAD,
        pr_open=True,
        origin_active=True,
        installation_id=17,
        repository_full_name="octo/repo",
        pr_number=7,
        review_body="## Review summary",
        findings=(FINDING,),
        findings_hash=HASH,
        provider="github",
        project_id=101,
    )
    return replace(base, **change)


def _inline_placement_keys(ctx: PublishContext) -> dict[UUID, bool]:
    return {item.finding_id: True for item in ctx.findings if item.finding_id is not None}


@dataclass
class Store:
    context: PublishContext
    state: str = "publishing"
    error_code: str | None = None
    open_units: int = 0
    fail_complete_once: bool = False
    completed: list[tuple[int, tuple[int, ...], tuple[UUID, ...]]] = field(default_factory=list)
    inline_placement: dict[UUID, bool] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.inline_placement:
            self.inline_placement = _inline_placement_keys(self.context)

    async def publish_context(self, run_id: UUID) -> PublishContext | None:
        return replace(self.context, state=self.state)

    async def complete_publication(
        self,
        run_id: UUID,
        *,
        review_id: int,
        comment_ids: tuple[int, ...],
        findings_hash: str,
        moved_to_body_finding_ids: tuple[UUID, ...] = (),
        now: datetime,
        expected_publication: PublishContext | None = None,
    ) -> str | None:
        if self.fail_complete_once:
            self.fail_complete_once = False
            raise RuntimeError("simulated crash before publication commit")
        assert findings_hash == HASH
        for key in moved_to_body_finding_ids:
            if key in self.inline_placement:
                self.inline_placement[key] = False
        self.completed.append((review_id, comment_ids, moved_to_body_finding_ids))
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
        expected_publication: PublishContext | None = None,
    ) -> bool:
        assert from_state == "publishing"
        self.state, self.error_code = state, error_code
        return True

    async def provider_origin_active(self, run_id: UUID) -> bool:
        return True

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        inline = sum(1 for placed in self.inline_placement.values() if placed)
        body = len(self.inline_placement) - inline
        return CheckRunReport(
            CheckRunTarget(17, "octo/repo", HEAD, RUN),
            self.state,
            1,
            self.error_code,
            verdict="blocking",
            inline_count=inline,
            body_count=body,
        )


class Uow:
    def __init__(self, store: Store) -> None:
        self.runs = store
        self._store = store

    async def __aenter__(self) -> Self:
        self._store.open_units += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._store.open_units -= 1
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
    results: list[SubmittedReview | PublishError]
    submissions: list[ReviewSubmission] = field(default_factory=list)

    async def submit_review(
        self, submission: ReviewSubmission, *, checkpoint: RunProviderCheckpoint
    ) -> SubmittedReview:
        await checkpoint()
        self.submissions.append(submission)
        result = self.results.pop(0)
        if isinstance(result, PublishError):
            raise result
        return result


@dataclass
class CheckRuns:
    views: list[CheckRunView] = field(default_factory=list)

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        self.views.append(view)


@dataclass
class GitLabReviews:
    """Fake GitLab discussions gateway. Records posts and refuses work inside a unit of work."""

    results: list[SubmittedReview | PublishError]
    store: Store
    calls: list[tuple[ReviewSubmission, int]] = field(default_factory=list)
    inlines: list[tuple[str, int]] = field(default_factory=list)
    summaries: list[str] = field(default_factory=list)

    async def submit_review(
        self, submission: ReviewSubmission, *, project_id: int
    ) -> SubmittedReview:
        assert self.store.open_units == 0
        self.calls.append((submission, project_id))
        result = self.results.pop(0)
        if isinstance(result, PublishError):
            raise result
        for finding in submission.findings:
            self.inlines.append((finding.path, finding.line))
        self.summaries.append(submission.body)
        return result


def run(
    store: Store,
    reviews: Reviews | None,
    sleeps: list[float] | None = None,
    gitlab: GitLabReviews | None = None,
) -> tuple[Trace, CheckRuns]:
    trace, check_runs = Trace(), CheckRuns()
    pauses = sleeps if sleeps is not None else []

    async def sleep(seconds: float) -> None:
        pauses.append(seconds)

    publisher_kwargs: dict[str, Any] = {}
    if gitlab is not None:
        publisher_kwargs["gitlab_reviews"] = gitlab

    asyncio.run(
        PublishRunReview(
            uow_factory=lambda: Uow(store),
            reviews=reviews,
            check_runs=check_runs,
            trace=trace,
            sleep=sleep,
            now=lambda: NOW,
            **publisher_kwargs,
        ).execute(POINTER)
    )
    return trace, check_runs


def gitlab_context(**change: Any) -> PublishContext:
    return context(provider="gitlab", project_id=PROJECT_ID, **change)


def test_publication_succeeds_with_one_trace_record_and_a_neutral_check_run() -> None:
    store = Store(context())
    reviews = Reviews([SubmittedReview(501, (9001,))])
    trace, check_runs = run(store, reviews)

    assert store.state == "succeeded"
    assert store.completed == [(501, (9001,), ())]
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


def test_coordinate_fallback_with_no_inline_findings_fails_after_one_retry() -> None:
    store = Store(context(findings=()))
    reviews = Reviews(
        [
            PublishError("coordinates", "line outside diff", http_status=422),
            PublishError("coordinates", "line outside diff", http_status=422),
        ]
    )
    run(store, reviews)

    assert store.state == "failed"
    assert store.error_code == "github_publish_failed"
    assert len(reviews.submissions) == 2
    assert reviews.submissions[1].body.count("## Inline findings") == 0


def test_coordinate_422_moves_inline_findings_into_the_body_once() -> None:
    store = Store(context())
    reviews = Reviews(
        [
            PublishError("coordinates", "line outside diff", http_status=422),
            SubmittedReview(7, ()),
        ]
    )
    trace, _ = run(store, reviews)

    assert store.state == "succeeded"
    assert store.completed == [(7, (), (FINDING_ID,))]
    assert reviews.submissions[1].findings == ()
    assert "`src/app.py:3`" in reviews.submissions[1].body
    assert [record[1]["try"] for record in trace.records] == [1, 2]


def test_stale_commit_422_cancels_as_superseded() -> None:
    store = Store(context())
    run(store, Reviews([PublishError("stale_commit", "commit_id", http_status=422)]))
    assert (store.state, store.error_code) == ("cancelled", "superseded")


def test_forbidden_fails_without_retry() -> None:
    store = Store(context())
    trace, check_runs = run(store, Reviews([PublishError("forbidden", "no", http_status=403)]))
    assert (store.state, store.error_code) == ("failed", "github_forbidden")
    assert len(trace.records) == 1
    assert check_runs.views[-1].conclusion == "neutral"


def test_server_errors_retry_three_times_with_spec_pauses_then_fail() -> None:
    store = Store(context())
    error = PublishError("retryable", "bad gateway", http_status=502)
    sleeps: list[float] = []
    trace, _ = run(store, Reviews([error, error, error, error]), sleeps)

    assert (store.state, store.error_code) == ("failed", "github_publish_failed")
    assert sleeps == [2.0, 8.0, 30.0]
    assert len(trace.records) == 4
    assert trace.records[0][2] == {"error": {"http_status": 502, "message": "bad gateway"}}


def test_retry_after_up_to_sixty_seconds_replaces_the_pause() -> None:
    store = Store(context())
    limited = PublishError(
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
            return httpx.Response(
                200,
                json=[
                    {
                        "id": 900,
                        "path": FINDING.path,
                        "line": FINDING.line,
                        "body": "**Wrong branch**\n\nThe branch is inverted.",
                    }
                ],
            )
        raise AssertionError(f"unexpected {request.method} {request.url}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)

    assert asyncio.run(
        gateway.submit_review(submission, checkpoint=provider_checkpoint)
    ) == SubmittedReview(55, (900,), posted_inline_finding_ids=frozenset({FINDING_ID}))
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
        if request.method == "GET" and request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
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
    with pytest.raises(PublishError) as raised:
        asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint))
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
    asyncio.run(
        gateway.upsert(
            target,
            CheckRunView("in_progress", None, "t", "попытка 1 из 3"),
            checkpoint=provider_checkpoint,
        )
    )
    asyncio.run(
        gateway.upsert(
            target,
            CheckRunView("completed", "neutral", "t", "done"),
            checkpoint=provider_checkpoint,
        )
    )

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


def _check_run_bodies(details_url: str | None) -> dict[str, Any]:
    """Create then update one check-run; return the POST and PATCH bodies by method."""
    bodies: dict[str, Any] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            runs = [{"id": 31, "external_id": str(RUN)}] if "POST" in bodies else []
            return httpx.Response(200, json={"total_count": len(runs), "check_runs": runs})
        bodies[request.method] = _json(request)
        return httpx.Response(201 if request.method == "POST" else 200, json={"id": 31})

    gateway = GitHubCheckRunGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    target = CheckRunTarget(17, "octo/repo", HEAD, RUN)
    for view in (
        CheckRunView("in_progress", None, "t", "попытка 1 из 3", details_url=details_url),
        CheckRunView("completed", "neutral", "t", "done", details_url=details_url),
    ):
        asyncio.run(gateway.upsert(target, view, checkpoint=provider_checkpoint))
    assert set(bodies) == {"POST", "PATCH"}
    return bodies


def test_check_run_gateway_sends_the_run_link_as_details_url_on_create_and_update() -> None:
    bodies = _check_run_bodies(f"https://review.example/runs/{RUN}")

    assert bodies["POST"]["details_url"] == (
        "https://review.example/runs/11111111-1111-1111-1111-111111111111"
    )
    assert bodies["PATCH"]["details_url"] == (
        "https://review.example/runs/11111111-1111-1111-1111-111111111111"
    )


def test_check_run_gateway_omits_details_url_without_a_run_link() -> None:
    bodies = _check_run_bodies(None)

    assert "details_url" not in bodies["POST"]
    assert "details_url" not in bodies["PATCH"]


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
        if path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if path.endswith("/pulls/7/reviews"):
            body = f"x\n\n<!-- ai-review findings_hash={HASH} -->"
            return httpx.Response(200, json=[{"id": 55, "body": body}] if posts else [])
        comment_reads.append(1)
        if len(comment_reads) == 1:
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(
            200,
            json=[
                {
                    "id": 900,
                    "path": FINDING.path,
                    "line": FINDING.line,
                    "body": "**Wrong branch**\n\nThe branch is inverted.",
                }
            ],
        )

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)

    with pytest.raises(PublishError) as raised:
        asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint))
    assert raised.value.kind == "retryable"
    assert asyncio.run(
        gateway.submit_review(submission, checkpoint=provider_checkpoint)
    ) == SubmittedReview(55, (900,), posted_inline_finding_ids=frozenset({FINDING_ID}))
    assert len(posts) == 1


def test_review_gateway_does_not_post_on_a_head_that_moved_on_github() -> None:
    posts: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            posts.append(request.url.path)
            return httpx.Response(200, json={"id": 1})
        if request.url.path.endswith("/pulls/7/reviews"):
            return httpx.Response(200, json=[])
        if request.url.path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": "c" * 40}})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    submission = ReviewSubmission(17, "octo/repo", 7, HEAD, "COMMENT", "body", (FINDING,), HASH)

    with pytest.raises(PublishError) as raised:
        asyncio.run(gateway.submit_review(submission, checkpoint=provider_checkpoint))
    assert raised.value.kind == "stale_commit"
    assert posts == []


@pytest.mark.parametrize("github_configured", [False, True])
def test_gitlab_publication_posts_the_stored_overview_and_skips_the_check_run(
    github_configured: bool,
) -> None:
    store = Store(gitlab_context(review_body=STORED_BODY))
    gitlab = GitLabReviews([SubmittedReview(880, (4242,))], store)
    trace, check_runs = run(store, Reviews([]) if github_configured else None, gitlab=gitlab)

    assert store.state == "succeeded"
    assert store.completed == [(880, (), ())]
    assert [(call[0].body, call[1]) for call in gitlab.calls] == [(STORED_BODY, PROJECT_ID)]
    assert gitlab.inlines == [("src/app.py", 3)]
    assert gitlab.summaries == [STORED_BODY]
    assert "**Verdict:** blocking" in gitlab.summaries[0]
    assert (
        "**Risk summary:** critical: 0, high: 1, medium: 0, low: 0, info: 0" in gitlab.summaries[0]
    )
    assert "**Checked files:**" in gitlab.summaries[0]
    assert check_runs.views == []
    assert trace.records == [
        (
            "gitlab.publish_review",
            {
                "head_sha": HEAD,
                "findings_hash": HASH,
                "review_event": "COMMENT",
                "inline_count": 1,
                "try": 1,
            },
            {"gitlab_review_id": 880},
        )
    ]


def test_gitlab_execute_again_with_the_same_hash_does_not_post_again() -> None:
    store = Store(gitlab_context(review_body=STORED_BODY))
    gitlab = GitLabReviews(
        [SubmittedReview(880, (4242,)), SubmittedReview(881, (4243,))],
        store,
    )
    run(store, Reviews([]), gitlab=gitlab)
    trace, check_runs = run(store, Reviews([]), gitlab=gitlab)

    assert gitlab.inlines == [("src/app.py", 3)]
    assert gitlab.summaries == [STORED_BODY]
    assert len(gitlab.results) == 1
    assert store.completed == [(880, (), ())]
    assert trace.records == []
    assert check_runs.views == []


def test_unset_gitlab_token_leaves_the_run_publishing_without_an_http_call(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store(gitlab_context(review_body=STORED_BODY))
    github = Reviews([SubmittedReview(1, (2,))])
    with caplog.at_level(logging.WARNING):
        trace, check_runs = run(store, github)

    assert store.state == "publishing"
    assert store.completed == []
    assert github.submissions == []
    assert trace.records == []
    assert check_runs.views == []
    assert "GitLab publication is not configured" in caplog.text
    assert "GitHub App is not configured" not in caplog.text

    neither = Store(gitlab_context(review_body=STORED_BODY))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        trace, check_runs = run(neither, None)
    assert neither.state == "publishing"
    assert trace.records == []
    assert check_runs.views == []
    assert "GitLab publication is not configured" in caplog.text
    assert "GitHub App" not in caplog.text


def test_unset_github_gateway_leaves_the_run_publishing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = Store(context())
    with caplog.at_level(logging.WARNING):
        trace, check_runs = run(store, None)

    assert store.state == "publishing"
    assert store.completed == []
    assert trace.records == []
    assert check_runs.views == []
    assert "GitHub App is not configured" in caplog.text


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"pr_open": False}, "pr_closed"),
        ({"pr_head_sha": "c" * 40}, "superseded"),
        ({"cancel_requested": True}, "cancelled_by_user"),
    ],
)
def test_gitlab_publisher_cancels_before_post_without_a_check_run(
    change: dict[str, Any], reason: str
) -> None:
    store = Store(gitlab_context(**change))
    gitlab = GitLabReviews([], store)
    trace, check_runs = run(store, Reviews([]), gitlab=gitlab)

    assert (store.state, store.error_code) == ("cancelled", reason)
    assert gitlab.calls == []
    assert gitlab.summaries == []
    assert trace.records == []
    assert check_runs.views == []


def test_gitlab_stale_commit_cancels_as_superseded_without_a_check_run() -> None:
    store = Store(gitlab_context())
    gitlab = GitLabReviews([PublishError("stale_commit", "head moved", http_status=400)], store)
    trace, check_runs = run(store, Reviews([]), gitlab=gitlab)

    assert (store.state, store.error_code) == ("cancelled", "superseded")
    assert gitlab.summaries == []
    assert check_runs.views == []
    assert [record[0] for record in trace.records] == ["gitlab.publish_review"]


def test_gitlab_forbidden_stores_gitlab_forbidden_and_skips_the_check_run() -> None:
    store = Store(gitlab_context())
    gitlab = GitLabReviews([PublishError("forbidden", "no", http_status=403)], store)
    trace, check_runs = run(store, Reviews([]), gitlab=gitlab)

    assert (store.state, store.error_code) == ("failed", "gitlab_forbidden")
    assert check_runs.views == []
    assert gitlab.summaries == []
    assert trace.records[0][0] == "gitlab.publish_review"


def test_gitlab_retryable_failures_store_gitlab_publish_failed() -> None:
    store = Store(gitlab_context())
    error = PublishError("retryable", "bad gateway", http_status=502)
    gitlab = GitLabReviews([error, error, error, error], store)
    sleeps: list[float] = []
    trace, check_runs = run(store, Reviews([]), sleeps, gitlab)

    assert (store.state, store.error_code) == ("failed", "gitlab_publish_failed")
    assert sleeps == [2.0, 8.0, 30.0]
    assert len(trace.records) == 4
    assert {record[0] for record in trace.records} == {"gitlab.publish_review"}
    assert check_runs.views == []


def test_gitlab_coordinate_failure_moves_only_unposted_findings() -> None:
    other = PublishedFinding(
        path="src/other.py",
        line=9,
        start_line=None,
        severity="medium",
        category="correctness",
        title="Narrow check",
        body="The guard is too wide.",
        suggestion=None,
        confidence=0.8,
        rule_name=None,
        finding_id=OTHER_FINDING_ID,
    )
    store = Store(gitlab_context(findings=(FINDING, other)))
    gitlab = GitLabReviews(
        [
            PublishError("coordinates", "line outside diff", http_status=400, unposted=(other,)),
            SubmittedReview(880, ()),
        ],
        store,
    )
    _, check_runs = run(store, Reviews([]), gitlab=gitlab)

    assert store.state == "succeeded"
    assert gitlab.calls[1][0].findings == ()
    assert "`src/other.py:9`" in gitlab.calls[1][0].body
    assert "`src/app.py:3`" not in gitlab.calls[1][0].body
    assert store.inline_placement[FINDING_ID] is True
    assert store.inline_placement[OTHER_FINDING_ID] is False
    assert store.completed[-1][2] == (OTHER_FINDING_ID,)
    assert check_runs.views == []


def test_partial_coordinate_fallback_keeps_posted_inline_for_check_run_counts() -> None:
    other = PublishedFinding(
        path="src/other.py",
        line=9,
        start_line=None,
        severity="medium",
        category="correctness",
        title="Narrow check",
        body="The guard is too wide.",
        suggestion=None,
        confidence=0.8,
        rule_name=None,
        finding_id=OTHER_FINDING_ID,
    )
    store = Store(context(findings=(FINDING, other)))
    reviews = Reviews(
        [
            PublishError("coordinates", "line outside diff", http_status=422, unposted=(other,)),
            SubmittedReview(7, (900,)),
        ]
    )
    _, check_runs = run(store, reviews)

    assert store.inline_placement[FINDING_ID] is True
    assert store.inline_placement[OTHER_FINDING_ID] is False
    assert check_runs.views[-1].summary is not None
    assert "inline: 1" in check_runs.views[-1].summary
    assert "в теле ревью: 1" in check_runs.views[-1].summary


def test_github_redelivery_after_failed_complete_restores_body_only_placement() -> None:
    """Body-only fallback + crash before commit; redelivery reads 0 comments → all in body."""
    posts = 0
    marker = f"<!-- ai-review findings_hash={HASH} -->"

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal posts
        path = request.url.path
        if request.method == "GET" and path.endswith("/pulls/7"):
            return httpx.Response(200, json={"head": {"sha": HEAD}})
        if request.method == "GET" and path.endswith("/pulls/7/reviews"):
            if posts >= 2:
                return httpx.Response(200, json=[{"id": 55, "body": f"summary\n\n{marker}"}])
            return httpx.Response(200, json=[])
        if request.method == "GET" and path.endswith("/reviews/55/comments"):
            return httpx.Response(200, json=[])
        if request.method == "POST" and path.endswith("/reviews"):
            posts += 1
            if posts == 1:
                return httpx.Response(
                    422,
                    json={
                        "message": "Unprocessable",
                        "errors": ["Line must be part of the diff"],
                    },
                )
            return httpx.Response(200, json={"id": 55})
        raise AssertionError(f"unexpected {request.method} {request.url}")

    gateway = GitHubPullRequestReviewGateway(
        client=httpx.AsyncClient(
            base_url="https://api.github.test", transport=httpx.MockTransport(respond)
        ),
        token_provider=_Tokens(),
    )
    store = Store(context(), fail_complete_once=True)
    trace, check_runs = Trace(), CheckRuns()

    async def publish() -> None:
        await PublishRunReview(
            uow_factory=lambda: Uow(store),
            reviews=gateway,
            check_runs=check_runs,
            trace=trace,
            now=lambda: NOW,
        ).execute(POINTER)

    with pytest.raises(RuntimeError, match="simulated crash"):
        asyncio.run(publish())

    assert store.state == "publishing"
    assert store.inline_placement[FINDING_ID] is True
    assert posts == 2

    asyncio.run(publish())

    assert store.state == "succeeded"
    assert posts == 2
    assert store.inline_placement[FINDING_ID] is False
    assert store.completed[-1][2] == (FINDING_ID,)
    assert check_runs.views[-1].summary is not None
    assert "inline: 0" in check_runs.views[-1].summary
    assert "в теле ревью: 1" in check_runs.views[-1].summary


def test_unsupported_provider_stays_publishing_without_a_gateway_call() -> None:
    store = Store(context(provider="bitbucket"))
    reviews = Reviews([])
    trace, check_runs = run(store, reviews)

    assert store.state == "publishing"
    assert reviews.submissions == []
    assert trace.records == []
    assert check_runs.views == []


def test_worker_never_uses_a_legacy_process_pat_for_installed_publication() -> None:
    from app.worker import WorkerSettings

    env = {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}
    assert WorkerSettings.from_environment(env).gitlab is None
    assert (
        WorkerSettings.from_environment(
            {
                **env,
                "GITLAB_TOKEN": "glpat-test",
                "GITLAB_BASE_URL": "https://gitlab.example",
            }
        ).gitlab
        is None
    )


def test_unconfigured_gitlab_uses_safe_readiness_log(caplog: pytest.LogCaptureFixture) -> None:
    store = Store(context(provider="gitlab"))
    asyncio.run(
        PublishRunReview(
            uow_factory=lambda: Uow(store), reviews=None, check_runs=None, trace=Trace()
        ).execute(POINTER)
    )
    assert "GitLab publication is not configured" in caplog.text
    assert "GITLAB_TOKEN" not in caplog.text
