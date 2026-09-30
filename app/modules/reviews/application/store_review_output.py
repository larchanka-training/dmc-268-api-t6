"""Post-process and store an accepted model answer, then hand the Run to the publisher (T8)."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.findings_post_processor import (
    FindingsPostProcessor,
    ProcessedFinding,
    ProcessedReviewOutput,
)
from app.modules.reviews.application.queue_messages import ReviewPublishPointer, ReviewPublishQueue
from app.modules.reviews.application.review_output import (
    ReviewOutputRepository,
    _as_json_object,
    parse_review_output,
)
from app.modules.reviews.application.run_failures import LEASE
from app.modules.reviews.application.verdict import (
    HashedFinding,
    findings_hash,
    review_event,
    severity_counts,
    verdict,
)

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PublishingGuard:
    """T8 guard inputs, read under the Run row lock."""

    state: str
    worker_id: str | None
    cancel_requested: bool
    head_sha: str
    head_current: bool
    repository_review_event: str


class PublishingRepository(ReviewOutputRepository, Protocol):
    async def lock_for_publishing(self, run_id: UUID) -> PublishingGuard | None: ...

    async def enter_publishing(
        self,
        run_id: UUID,
        *,
        lease_until: datetime,
        postprocess_request: dict[str, Any],
        postprocess_response: dict[str, Any],
        started_at: datetime,
        duration_ms: int,
    ) -> None: ...


class PublishingUnitOfWork(UnitOfWork, Protocol):
    @property
    def reviews(self) -> PublishingRepository: ...


def hashed_findings(processed: ProcessedReviewOutput) -> tuple[HashedFinding, ...]:
    return tuple(_hashed(item) for item in (*processed.inline, *processed.body_only))


def _hashed(item: ProcessedFinding) -> HashedFinding:
    finding = item.finding
    return HashedFinding(
        path=finding.path,
        line_start=finding.start_line or finding.line,
        line_end=finding.line if finding.start_line is not None else None,
        severity=finding.severity,
        category=finding.category,
        title=finding.title,
        body=finding.body,
        suggestion=finding.suggestion,
        inline=item.bucket == "inline",
    )


class StoreReviewOutput:
    """``review.postprocess``, findings and ``publishing`` in one transaction, then publish."""

    def __init__(
        self,
        uow_factory: Callable[[], PublishingUnitOfWork],
        queue: ReviewPublishQueue,
        *,
        worker_id: str,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._queue = queue
        self._worker_id = worker_id
        self._now = now

    async def execute(
        self,
        run_id: UUID,
        raw_output: Mapping[str, object] | str | bytes,
    ) -> bool:
        parsed = parse_review_output(raw_output)
        raw_json = _as_json_object(raw_output)
        started_at = self._now()
        started = time.monotonic()
        async with self._uow_factory() as uow:
            context = await uow.reviews.get_post_processing_input(run_id)
        if context is None:
            return False
        processor = FindingsPostProcessor.from_default_patterns()
        processed = processor.process(
            parsed,
            hunk_lines=context.hunk_lines,
            rule_names=context.rule_names,
            repository_max_inline=context.repository_max_inline,
        )
        findings = hashed_findings(processed)
        severities = [item.severity for item in findings]
        run_verdict = verdict(severities)
        hash_ = findings_hash(findings)

        async with self._uow_factory() as uow:
            guard = await uow.reviews.lock_for_publishing(run_id)
            if (
                guard is None
                or guard.state != "running"
                or guard.worker_id != self._worker_id
                or guard.cancel_requested
                or not guard.head_current
            ):
                return False
            event = review_event(guard.repository_review_event, run_verdict)
            if await uow.reviews.store_review_output(run_id, raw_json, parsed, processed) is None:
                return False
            await uow.reviews.enter_publishing(
                run_id,
                lease_until=self._now() + LEASE,
                postprocess_request={
                    "min_confidence": processor.patterns.min_confidence,
                    "max_inline": context.repository_max_inline,
                },
                postprocess_response={
                    "inline": len(processed.inline),
                    "body_only": len(processed.body_only),
                    "dropped": [
                        {"index": item.position, "drop_reason": item.drop_reason}
                        for item in processed.dropped
                    ],
                    "verdict": run_verdict,
                    "severity_counts": severity_counts(severities),
                    "findings_hash": hash_,
                },
                started_at=started_at,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            await uow.commit()

        try:
            await self._queue.publish_review(
                ReviewPublishPointer(run_id, guard.head_sha, hash_, event)
            )
        except Exception:
            # The reconciler republishes review.publish/v1 after the lease expires (T17).
            _LOGGER.exception("review.publish/v1 remains unpublished for run %s", run_id)
        return True
