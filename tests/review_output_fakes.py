"""Shared recording fakes for review-output tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishedFinding,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.store_review_output import (
    AcceptedReviewCheckpoint,
    FinalizedReviewPlan,
    PublishingGuard,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000408")


class InMemoryFinalization:
    """Keep a fake finalized plan available across short units of work."""

    _finalized_plan: FinalizedReviewPlan | None = None
    finalized_processed: ProcessedReviewOutput | None = None
    finalized_result: dict[str, Any] | None = None
    finalized_model_output: dict[str, object] | None = None

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        raise NotImplementedError

    async def load_review_checkpoint(self, run_id: UUID) -> AcceptedReviewCheckpoint | None:
        return None

    async def checkpoint_review_candidates(
        self,
        run_id: UUID,
        *,
        model_output: dict[str, object],
        candidates: list[dict[str, object]],
        dropped: list[dict[str, object]],
        accepted_call_id: UUID | None,
        expected_attempt: int,
        incomplete_paths: tuple[str, ...],
        fitted_context: dict[str, object],
        started_at: datetime,
        duration_ms: int,
    ) -> None:
        raise AssertionError("candidate checkpoint is outside this fake's scenario")

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
        guard = await self.lock_for_publishing(run_id)
        self._finalized_plan = FinalizedReviewPlan(guard.head_sha, findings_hash, review_event)
        self.finalized_processed = processed
        self.finalized_result = result
        self.finalized_model_output = model_output

    async def load_finalized_plan(self, run_id: UUID) -> FinalizedReviewPlan | None:
        return self._finalized_plan

    async def enter_publishing_from_final(self, run_id: UUID, *, lease_until: datetime) -> bool:
        return True


@dataclass
class RecordingRepository:
    context: FindingPostProcessingInput | None
    publication: ReviewPublication | None
    raw_objects: list[dict[str, object]] = field(default_factory=list)
    marked: int = 0

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput | None:
        assert run_id == RUN_ID
        return self.context

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object],
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication | None:
        assert run_id == RUN_ID
        assert parsed.findings[0].title == "Missing transaction boundary"
        self.raw_objects.append(model_output)
        return self.publication

    async def mark_review_published(self, run_id: UUID) -> None:
        assert run_id == RUN_ID
        self.marked += 1


@dataclass
class RecordingUnitOfWork:
    reviews: RecordingRepository
    commits: int = 0
    active: bool = False

    async def __aenter__(self) -> RecordingUnitOfWork:
        self.active = True
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.active = False

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        return None


@dataclass
class RecordingFactory:
    repository: RecordingRepository
    units: list[RecordingUnitOfWork] = field(default_factory=list)

    def __call__(self) -> RecordingUnitOfWork:
        unit = RecordingUnitOfWork(self.repository)
        self.units.append(unit)
        return unit


@dataclass
class RecordingProvider:
    calls: list[tuple[str, str, tuple[PublishedFinding, ...], str]] = field(default_factory=list)
    units: list[RecordingUnitOfWork] | None = None

    async def publish_review(
        self,
        *,
        commit_sha: str,
        body: str,
        findings: tuple[PublishedFinding, ...],
        idempotency_key: str,
    ) -> None:
        if self.units is not None:
            assert len(self.units) == 2  # context read and output write have completed
            assert all(not unit.active for unit in self.units)
        self.calls.append((commit_sha, body, findings, idempotency_key))
