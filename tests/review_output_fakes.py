"""Shared recording fakes for review-output tests."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import TracebackType
from uuid import UUID

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    PublishedFinding,
    ReviewOutput,
    ReviewPublication,
)

RUN_ID = UUID("00000000-0000-0000-0000-000000000408")


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
