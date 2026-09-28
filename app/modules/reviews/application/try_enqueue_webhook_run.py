"""Atomically enqueue one webhook Run, then publish its durable pointer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    EligibilityCandidate,
)

_LOGGER = logging.getLogger(__name__)


class EnqueueStatus(StrEnum):
    ENQUEUED = "enqueued"
    PUBLICATION_PENDING = "publication_pending"
    INELIGIBLE = "ineligible"
    STALE = "stale"
    DUPLICATE = "duplicate"


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    run_id: UUID | None = None


@dataclass(frozen=True)
class RunInsertCandidate:
    ci: EligibilityCandidate
    repository_id: UUID
    workspace_id: UUID
    repository_external_id: int
    pr_number: int
    base_sha: str
    base_ref: str
    engine: str
    rule_version_id: UUID
    prompt_version_id: UUID


@dataclass(frozen=True)
class PendingRunMessage:
    run_id: UUID
    workspace_id: UUID
    installation_id: int
    repository_id: UUID
    repository_external_id: int
    repository_full_name: str
    pr_number: int
    head_sha: str
    base_sha: str
    base_ref: str
    engine: str
    rule_version_id: UUID
    prompt_version_id: UUID
    attempt: int
    requested_at: datetime
    schema: str = "review.run/v1"
    trigger: str = "webhook"

    @property
    def message_id(self) -> UUID:
        return self.run_id

    @classmethod
    def from_candidate(
        cls, run_id: UUID, candidate: RunInsertCandidate, requested_at: datetime
    ) -> PendingRunMessage:
        return cls(
            run_id=run_id,
            workspace_id=candidate.workspace_id,
            installation_id=candidate.ci.installation_external_id,
            repository_id=candidate.repository_id,
            repository_external_id=candidate.repository_external_id,
            repository_full_name=candidate.ci.repository_full_name,
            pr_number=candidate.pr_number,
            head_sha=candidate.ci.head_sha,
            base_sha=candidate.base_sha,
            base_ref=candidate.base_ref,
            engine=candidate.engine,
            rule_version_id=candidate.rule_version_id,
            prompt_version_id=candidate.prompt_version_id,
            attempt=1,
            requested_at=requested_at,
        )

    def as_payload(self) -> dict[str, object]:
        """Match the #34 `review.run/v1` pointer schema, without transport details."""
        utc_requested_at = self.requested_at.astimezone(UTC).isoformat().replace("+00:00", "Z")
        return {
            "schema": self.schema,
            "message_id": str(self.message_id),
            "run_id": str(self.run_id),
            "workspace_id": str(self.workspace_id),
            "installation_id": self.installation_id,
            "repo": {
                "id": str(self.repository_id),
                "provider": "github",
                "external_id": self.repository_external_id,
                "full_name": self.repository_full_name,
            },
            "pr": {
                "number": self.pr_number,
                "head_sha": self.head_sha,
                "base_sha": self.base_sha,
                "base_ref": self.base_ref,
            },
            "engine": self.engine,
            "rule_version_id": str(self.rule_version_id),
            "prompt_version_id": str(self.prompt_version_id),
            "trigger": self.trigger,
            "attempt": self.attempt,
            "requested_at": utc_requested_at,
        }


class EligibilityChecker(Protocol):
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> CiEligibility: ...


class WebhookRunStore(Protocol):
    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | None: ...

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | None: ...

    async def notify_run_updated(self, run_id: UUID, workspace_id: UUID, status: str) -> None: ...

    async def mark_published(self, run_id: UUID, now: datetime) -> None: ...

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]: ...


class WebhookRunUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> WebhookRunStore: ...


class RunMessagePublisher(Protocol):
    """Return only after a persistent `review.run/v1` publisher confirm."""

    async def publish_confirmed(self, message: PendingRunMessage) -> None: ...


class TryEnqueueWebhookRun:
    def __init__(
        self,
        *,
        eligibility: EligibilityChecker,
        uow_factory: Callable[[], WebhookRunUnitOfWork],
        publisher: RunMessagePublisher,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._eligibility = eligibility
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._now = now

    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult:
        decision = await self._eligibility.execute(code_change_id, expected_head_sha)
        if not decision.eligible or decision.candidate is None:
            return EnqueueResult(EnqueueStatus.INELIGIBLE)
        async with self._uow_factory() as uow:
            candidate = await uow.runs.lock_candidate(code_change_id)
            if candidate is None or candidate.ci != decision.candidate:
                return EnqueueResult(EnqueueStatus.STALE)
            message = await uow.runs.insert_webhook_run(candidate, self._now())
            if message is None:
                return EnqueueResult(EnqueueStatus.DUPLICATE)
            await uow.runs.notify_run_updated(message.run_id, message.workspace_id, "queued")
            await uow.commit()

        if not await self._publish_and_mark(message):
            return EnqueueResult(EnqueueStatus.PUBLICATION_PENDING, message.run_id)
        return EnqueueResult(EnqueueStatus.ENQUEUED, message.run_id)

    async def replay_pending_publications(self, *, limit: int = 100) -> int:
        async with self._uow_factory() as uow:
            pending = await uow.runs.pending_messages(limit)
        confirmed = 0
        for message in pending:
            if await self._publish_and_mark(message):
                confirmed += 1
        return confirmed

    async def _publish_and_mark(self, message: PendingRunMessage) -> bool:
        try:
            await self._publisher.publish_confirmed(message)
        except Exception:
            _LOGGER.exception(
                "review.run/v1 publication remains pending for run %s", message.run_id
            )
            return False
        async with self._uow_factory() as uow:
            await uow.runs.mark_published(message.run_id, self._now())
            await uow.commit()
        return True
