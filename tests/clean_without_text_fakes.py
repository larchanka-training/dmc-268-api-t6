"""Shared recording fakes for clean-without-text result tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field, replace
from datetime import datetime
from types import TracebackType
from typing import Any
from uuid import UUID

from app.modules.reviews.application.findings_post_processor import ProcessedReviewOutput
from app.modules.reviews.application.post_processing_diff_map import diff_map_from_snapshots
from app.modules.reviews.application.queue_messages import ReviewPublishPointer
from app.modules.reviews.application.review_output import (
    FindingPostProcessingInput,
    ReviewOutput,
    ReviewPublication,
)
from app.modules.reviews.application.run_coverage import RunCoverage
from app.modules.reviews.application.store_review_output import (
    AcceptedReviewCheckpoint,
    PublishingGuard,
    StoreReviewOutput,
)
from app.modules.reviews.application.verdict import SeverityThresholdValue
from tests.review_output_fakes import InMemoryFinalization
from tests.run_coverage_fakes import code_snapshot

RUN_ID = UUID(int=213)


@dataclass
class Repository(InMemoryFinalization):
    coverage: RunCoverage | None
    state: str = "running"
    response: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    diagnostic: dict[str, object] = field(default_factory=dict)
    severity_threshold: SeverityThresholdValue = "all"

    async def get_post_processing_input(self, run_id: UUID) -> FindingPostProcessingInput:
        return FindingPostProcessingInput(
            diff_map_from_snapshots(
                repository_id=UUID(int=213),
                code_change_number=7,
                head_sha="a" * 40,
                base_sha="b" * 40,
                diff_old_sha="c" * 40,
                snapshots=[code_snapshot()],
            ),
            frozenset({"src/app.py"}),
            frozenset(),
            coverage=self.coverage,
            severity_threshold=self.severity_threshold,
        )

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return PublishingGuard(
            self.state, "worker", False, "a" * 40, True, "REQUEST_CHANGES", attempt=1
        )

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
        self.response = result
        self.body = processed.review_body
        if model_output is not None:
            self.diagnostic = model_output

    async def enter_publishing_from_final(self, run_id: UUID, *, lease_until: datetime) -> bool:
        self.state = "publishing"
        return True

    async def store_review_output(
        self,
        run_id: UUID,
        model_output: dict[str, object] | None,
        parsed: ReviewOutput,
        processed: ProcessedReviewOutput,
    ) -> ReviewPublication:
        self.body = processed.review_body
        if model_output is not None:
            self.diagnostic = model_output
        self.state = "publishing"
        return ReviewPublication("a" * 40, (), self.body)

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

    async def mark_review_published(self, run_id: UUID) -> None:
        raise AssertionError("finalization only queues publication")


@dataclass
class Uow:
    reviews: Repository
    committed: list[str]

    async def __aenter__(self) -> Uow:
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
class Queue:
    committed: list[str]
    pointers: list[ReviewPublishPointer] = field(default_factory=list)

    async def publish_review(self, pointer: ReviewPublishPointer) -> None:
        assert self.committed == ["commit", "commit"]
        self.pointers.append(pointer)


def answer() -> dict[str, object]:
    return {
        "findings": [],
        "summary": {
            "problem": "All changes were checked.",
            "done_well": "The change is clear.",
            "effort": "small",
        },
    }


def finalize(
    result: RunCoverage | None, findings: list[dict[str, object]] | None = None
) -> tuple[Repository, Queue]:
    repository = Repository(result)
    committed: list[str] = []
    queue = Queue(committed)
    value = answer()
    value["findings"] = [] if findings is None else findings
    assert (
        asyncio.run(
            StoreReviewOutput(
                lambda: Uow(repository, committed),
                queue,
                worker_id="worker",
                expected_attempt=1,
                run_url=lambda _: None,
            ).execute(RUN_ID, value)
        )
        is True
    )
    return repository, queue


def finding(path: str, severity: str = "high") -> dict[str, object]:
    return {
        "path": path,
        "line": 1,
        "start_line": None,
        "severity": severity,
        "category": "security",
        "title": "Unchecked authorization",
        "body": "A remote caller can bypass the permission check.",
        "suggestion": None,
        "confidence": 0.9,
        "rule_name": None,
    }


class _CheckpointRepository(Repository):
    """Shared unit adapter for a claimed snapshot run's short storage stages."""

    async def load_review_checkpoint(self, run_id: UUID) -> AcceptedReviewCheckpoint | None:
        return None

    async def checkpoint_review_candidates(self, run_id: UUID, **kwargs: object) -> None:
        return None

    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard:
        return replace(await super().lock_for_publishing(run_id), attempt=1)
