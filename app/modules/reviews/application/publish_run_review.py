"""The ``review.publish`` consumer: one GitHub review per Run (T14-T16, PIPELINE_SPEC §5.2)."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.check_runs import CheckRunGateway
from app.modules.reviews.application.handle_review_run import (
    CheckRunReports,
    update_check_run,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.run_failures import cancellation_reason
from app.modules.reviews.application.run_trace import RunTrace

_LOGGER = logging.getLogger(__name__)

# Three in-process retries after the first try (§3): pauses 2 s, 8 s, 30 s.
PUBLISH_RETRY_PAUSES = (timedelta(seconds=2), timedelta(seconds=8), timedelta(seconds=30))
MAX_RETRY_AFTER = timedelta(seconds=60)


@dataclass(frozen=True)
class PublishContext:
    run_id: UUID
    state: str
    cancel_requested: bool
    head_sha: str
    pr_head_sha: str
    pr_open: bool
    installation_id: int
    repository_full_name: str
    pr_number: int
    review_body: str
    findings: tuple[PublishedFinding, ...]
    findings_hash: str


@dataclass(frozen=True)
class ReviewSubmission:
    installation_id: int
    repository_full_name: str
    pr_number: int
    commit_sha: str
    event: str
    body: str
    findings: tuple[PublishedFinding, ...]
    findings_hash: str


@dataclass(frozen=True)
class SubmittedReview:
    review_id: int
    comment_ids: tuple[int, ...]


class GitHubPublishError(Exception):
    """``kind``: ``retryable``, ``forbidden``, ``coordinates`` or ``stale_commit`` (§5.2)."""

    def __init__(
        self,
        kind: str,
        message: str,
        *,
        http_status: int | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status
        self.retry_after = retry_after


class PullRequestReviewGateway(Protocol):
    """Idempotent by ``findings_hash``: a review already posted for it is returned, not reposted."""

    async def submit_review(self, submission: ReviewSubmission) -> SubmittedReview: ...


class ReviewPublicationStore(CheckRunReports, Protocol):
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
    ) -> bool: ...

    async def publish_context(self, run_id: UUID) -> PublishContext | None: ...

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
        """Final state (``succeeded``, or ``cancelled`` if cancelled during the POST)."""
        ...


class ReviewPublicationUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> ReviewPublicationStore: ...


def move_inline_to_body(body: str, findings: tuple[PublishedFinding, ...]) -> str:
    """The 422 fallback: every inline comment becomes a line of the review body (SD §8.3)."""
    lines = [body, "", "## Inline findings"]
    lines.extend(
        f"- **{item.title}** (`{item.path}:{item.line}`): {item.body}" for item in findings
    )
    return "\n".join(lines)


class PublishRunReview:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], ReviewPublicationUnitOfWork],
        reviews: PullRequestReviewGateway | None,
        check_runs: CheckRunGateway | None,
        trace: RunTrace,
        run_url: Callable[[UUID], str | None] = lambda _: None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._reviews = reviews
        self._check_runs = check_runs
        self._trace = trace
        self._run_url = run_url
        self._sleep = sleep
        self._now = now

    async def execute(self, pointer: ReviewPublishPointer) -> None:
        if self._reviews is None:
            _LOGGER.warning("GitHub App is not configured; run %s stays publishing", pointer.run_id)
            return
        async with self._uow_factory() as uow:
            context = await uow.runs.publish_context(pointer.run_id)
        if context is None or context.state != "publishing":
            return
        if (context.head_sha, context.findings_hash) != (pointer.head_sha, pointer.findings_hash):
            _LOGGER.warning("Stale review.publish/v1 for run %s is ignored", pointer.run_id)
            return
        reason = cancellation_reason(
            pr_open=context.pr_open,
            head_current=context.head_sha == context.pr_head_sha,
            cancel_requested=context.cancel_requested,
        )
        if reason is not None:
            await self._finish(pointer.run_id, "cancelled", reason, None)
            return

        submission = ReviewSubmission(
            installation_id=context.installation_id,
            repository_full_name=context.repository_full_name,
            pr_number=context.pr_number,
            commit_sha=context.head_sha,
            event=pointer.review_event,
            body=context.review_body,
            findings=context.findings,
            findings_hash=context.findings_hash,
        )
        moved = False
        retries = 0
        try_no = 0
        while True:
            try_no += 1
            result = await self._submit(submission, pointer, try_no)
            if isinstance(result, SubmittedReview):
                break
            error = result
            if error.kind == "coordinates" and not moved:
                moved = True
                submission = replace(
                    submission,
                    body=move_inline_to_body(submission.body, submission.findings),
                    findings=(),
                )
                continue
            if error.kind == "stale_commit":
                await self._finish(pointer.run_id, "cancelled", "superseded", None)
                return
            if error.kind == "forbidden":
                await self._finish(pointer.run_id, "failed", "github_forbidden", str(error))
                return
            if error.kind == "retryable" and retries < len(PUBLISH_RETRY_PAUSES):
                pause = PUBLISH_RETRY_PAUSES[retries]
                if error.retry_after is not None and error.retry_after <= MAX_RETRY_AFTER:
                    pause = error.retry_after
                retries += 1
                await self._sleep(pause.total_seconds())
                continue
            await self._finish(pointer.run_id, "failed", "github_publish_failed", str(error))
            return

        async with self._uow_factory() as uow:
            completed = await uow.runs.complete_publication(
                pointer.run_id,
                review_id=result.review_id,
                comment_ids=result.comment_ids,
                findings_hash=context.findings_hash,
                moved_to_body=moved,
                now=self._now(),
            )
            await uow.commit()
        if completed:
            await update_check_run(
                self._uow_factory, self._check_runs, pointer.run_id, self._run_url
            )

    async def _submit(
        self, submission: ReviewSubmission, pointer: ReviewPublishPointer, try_no: int
    ) -> SubmittedReview | GitHubPublishError:
        assert self._reviews is not None
        request = {
            "head_sha": submission.commit_sha,
            "findings_hash": submission.findings_hash,
            "review_event": submission.event,
            "inline_count": len(submission.findings),
            "try": try_no,
        }
        started_at = self._now()
        started = asyncio.get_running_loop().time()
        result: SubmittedReview | GitHubPublishError
        try:
            result = await self._reviews.submit_review(submission)
            response: object = {"github_review_id": result.review_id}
        except GitHubPublishError as exc:
            result = exc
            response = {"error": {"http_status": exc.http_status, "message": str(exc)[:1000]}}
        duration_ms = int((asyncio.get_running_loop().time() - started) * 1000)
        await self._trace.record(
            pointer.run_id, "github.publish_review", request, response, started_at, duration_ms
        )
        return result

    async def _finish(
        self, run_id: UUID, state: str, error_code: str, error_message: str | None
    ) -> None:
        async with self._uow_factory() as uow:
            finished = await uow.runs.finish(
                run_id,
                from_state="publishing",
                worker_id=None,
                state=state,
                error_code=error_code,
                error_message=error_message,
                now=self._now(),
            )
            await uow.commit()
        if finished:
            await update_check_run(self._uow_factory, self._check_runs, run_id, self._run_url)
