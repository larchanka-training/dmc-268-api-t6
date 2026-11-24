"""Shared in-memory fakes for review output storage and Run publication tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from app.modules.reviews.application.check_runs import (
    CheckRunReport,
    CheckRunTarget,
    CheckRunView,
    RunProviderCheckpoint,
)
from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.get_run_diff import DiffSnapshot
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.publish_run_review import (
    PublishContext,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from app.modules.reviews.application.saved_review_result import (
    SavedReviewResult,
    read_saved_review_result,
)
from app.modules.reviews.application.store_review_output import PublishingGuard
from app.modules.reviews.application.verdict import coverage_verdict
from tests.review_output_fakes import InMemoryFinalization

HEAD_SHA = "a" * 40
BASE_SHA = "b" * 40
DIFF_OLD_SHA = "c" * 40


@dataclass
class PublicationRepositoryFake(InMemoryFinalization):
    """In-memory publishing and run-lifecycle store for publication tests."""

    snapshots: list[DiffSnapshot]
    coverage: RunCoverage
    review_event: str = "REQUEST_CHANGES"
    state: str = "running"
    error_code: str | None = None
    answers: list[dict[str, object] | None] = field(default_factory=list)
    response: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    findings_hash: str = ""

    def __post_init__(self) -> None:
        self._diff_map = diff_map_from_snapshots(
            repository_id=UUID(int=1),
            code_change_number=7,
            head_sha=HEAD_SHA,
            base_sha=BASE_SHA,
            diff_old_sha=DIFF_OLD_SHA,
            snapshots=self.snapshots,
        )
        self._checked_paths = frozenset(
            item.filename for item in self.snapshots if item.review_patch is not None
        )

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            self._diff_map,
            self._checked_paths,
            frozenset(),
            coverage=self.coverage,
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            self.state, "worker", False, HEAD_SHA, True, self.review_event, attempt=1
        )

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.answers.append(model_output)
        self.body = processed.review_body
        self.state = "publishing"
        return ReviewPublication(HEAD_SHA, (), self.body)

    async def finalize_review(
        self,
        run_id: UUID,
        *,
        model_output: dict[str, object] | None,
        processed: ProcessedReviewOutput,
        result: dict[str, Any],
        review_event: str,
        findings_hash: str,
        verify_request: dict[str, Any] | None,
        verify_response: dict[str, Any] | None,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        await super().finalize_review(
            run_id,
            model_output=model_output,
            processed=processed,
            result=result,
            review_event=review_event,
            findings_hash=findings_hash,
            verify_request=verify_request,
            verify_response=verify_response,
            started_at=started_at,
            duration_ms=duration_ms,
        )
        self.answers.append(model_output)
        self.body = processed.review_body
        self.response = result
        self.findings_hash = findings_hash

    async def enter_publishing_from_final(
        self,
        run_id: UUID,
        *,
        lease_until: datetime,
    ) -> bool:
        if self.state != "running":
            return False
        self.state = "publishing"
        return True

    async def enter_publishing(
        self,
        run_id: UUID,
        *,
        lease_until: datetime,
        postprocess_request: dict[str, Any],
        postprocess_response: dict[str, Any],
        started_at: datetime,
        duration_ms: int,
        verify_request: dict[str, Any] | None = None,
        verify_response: dict[str, Any] | None = None,
    ) -> None:
        self.response = postprocess_response
        self.findings_hash = str(postprocess_response.get("findings_hash", ""))

    async def mark_review_published(self, run_id: UUID) -> None:
        raise AssertionError("finalization only queues publication")

    async def publish_context(self, run_id: UUID) -> PublishContext | None:
        return PublishContext(
            run_id=run_id,
            state=self.state,
            cancel_requested=False,
            head_sha=HEAD_SHA,
            pr_head_sha=HEAD_SHA,
            pr_open=True,
            origin_active=True,
            installation_id=1,
            repository_full_name="o/r",
            pr_number=7,
            review_body=self.body,
            findings=(),
            findings_hash=self.findings_hash,
            provider="github",
            project_id=1,
        )

    async def provider_origin_active(self, run_id: UUID) -> bool:
        return True

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
        self.state = "succeeded"
        self.error_code = None
        return "succeeded"

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
        self.state = state
        self.error_code = error_code
        return True

    async def check_run_report(self, run_id: UUID) -> CheckRunReport | None:
        saved: SavedReviewResult | None = read_saved_review_result(
            self.response, head_sha=HEAD_SHA, severities=(), run_id=run_id
        )
        target = CheckRunTarget(1, "o/r", HEAD_SHA, run_id)
        if self.state != "succeeded" or saved is None:
            return CheckRunReport(target, self.state, 1, error_code=self.error_code)
        return CheckRunReport(
            target,
            self.state,
            1,
            verdict=saved.verdict,
            coverage_status=saved.coverage.status.value,
            saved_summary=saved.summary,
            inline_count=int(self.response.get("inline", 0)),
            body_count=int(self.response.get("body_only", 0)),
        )


@dataclass
class InlinePublishingRepositoryFake(PublicationRepositoryFake):
    """Repository fake for inline publishing paths that directly mark runs succeeded."""

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.answers.append(model_output)
        self.body = processed.review_body
        self.state = "publishing"
        self.coverage = processed.coverage
        status_val = (
            processed.coverage.status.value
            if hasattr(processed.coverage.status, "value")
            else str(processed.coverage.status)
        )
        severities = tuple(
            item.finding.severity for item in (*processed.inline, *processed.body_only)
        )
        self.response = {
            "verdict": coverage_verdict(severities, processed.coverage.status),
            "coverage": {
                "status": status_val,
                "reviewed_files": processed.coverage.reviewed_files,
                "reason_counts": (
                    dict(processed.coverage.reason_counts)
                    if hasattr(processed.coverage, "reason_counts")
                    else {}
                ),
            },
        }
        return ReviewPublication(HEAD_SHA, (), self.body)

    async def mark_review_published(self, run_id: UUID) -> None:
        self.state = "succeeded"


@dataclass
class PublicationUowFake:
    reviews: PublicationRepositoryFake
    committed: list[str] = field(default_factory=list)
    blob_cache: None = None

    @property
    def runs(self) -> PublicationRepositoryFake:
        return self.reviews

    async def __aenter__(self) -> PublicationUowFake:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        self.committed.append("commit")

    async def rollback(self) -> None:
        return None


@dataclass
class PublicationQueueFake:
    pointers: list[ReviewPublishPointer] = field(default_factory=list)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        self.pointers.append(pointer)


@dataclass
class CapturingReviewGateway:
    submissions: list[ReviewSubmission] = field(default_factory=list)

    async def submit_review(
        self, submission: ReviewSubmission, *, checkpoint: RunProviderCheckpoint
    ) -> SubmittedReview:
        await checkpoint()
        self.submissions.append(submission)
        return SubmittedReview(review_id=9001, comment_ids=())


@dataclass
class CapturingCheckRunGateway:
    views: list[CheckRunView] = field(default_factory=list)

    async def upsert(
        self, target: CheckRunTarget, view: CheckRunView, *, checkpoint: RunProviderCheckpoint
    ) -> None:
        await checkpoint()
        self.views.append(view)


@dataclass
class RecordingRunTrace:
    records: list[tuple[UUID, str, dict[str, Any], Any, datetime, int]] = field(
        default_factory=list
    )

    async def record(
        self,
        run_id: UUID,
        tool: str,
        request: dict[str, Any],
        response: Any,
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        self.records.append((run_id, tool, request, response, started_at, duration_ms))

    @property
    def steps(self) -> list[tuple[UUID, str, dict[str, Any], Any]]:
        return [(r[0], r[1], r[2], r[3]) for r in self.records]
