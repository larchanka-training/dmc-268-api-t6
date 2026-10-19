"""Atomically enqueue one webhook Run, then publish its durable pointer."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.determine_ci_eligibility import (
    CiEligibility,
    EligibilityCandidate,
    WebhookRunIdentity,
)
from app.modules.reviews.application.verdict import SeverityThresholdValue

_LOGGER = logging.getLogger(__name__)


class EnqueueStatus(StrEnum):
    ENQUEUED = "enqueued"
    PUBLICATION_PENDING = "publication_pending"
    INELIGIBLE = "ineligible"
    # The repository lacks what a Run needs; a later delivery does not fix that by itself.
    UNCONFIGURED = "unconfigured"
    STALE = "stale"
    DUPLICATE = "duplicate"
    DEFERRED = "deferred"


class CandidateMiss(StrEnum):
    """Why the locked read found no Run to insert; the value is the logged reason."""

    PULL_REQUEST_GONE = "pull_request_gone"
    REPOSITORY_GONE = "repository_gone"
    MISSING_INSTALLATION = "missing_installation"
    MISSING_RULES = "missing_rules"
    MISSING_PROMPT = "missing_prompt"


class DuplicateReason(StrEnum):
    """Why the insert created no second Run; the value is the logged reason."""

    ACTIVE_RUN = "active_run"
    HEAD_ALREADY_REVIEWED = "head_already_reviewed"


@dataclass(frozen=True)
class ActiveRunBlocker:
    """The locked PR has an active Run; cancellation is still nonterminal."""

    head_sha: str
    cancel_requested: bool


# The PR changed between the gate and the locked read. Logged as the reason of a STALE
# result; keep in sync with the reason table in docs/WEBHOOK_WORKER.md.
STATE_CHANGED_REASON = "state_changed"

_UNCONFIGURED = frozenset(
    {
        CandidateMiss.MISSING_INSTALLATION,
        CandidateMiss.MISSING_RULES,
        CandidateMiss.MISSING_PROMPT,
    }
)


class RunPublicationKind(StrEnum):
    QUEUED = "queued"
    CANCELLATION = "cancellation"


@dataclass(frozen=True)
class EnqueueResult:
    status: EnqueueStatus
    run_id: UUID | None = None
    reason: str | None = None
    # What blocks or delays the CI gate of an INELIGIBLE result (``CiEligibility.detail``).
    detail: str | None = None


def describe_enqueue(code_change_id: UUID, head_sha: str, result: EnqueueResult) -> str:
    """Render one enqueue outcome for a log line; keep in sync with docs/WEBHOOK_WORKER.md."""
    outcome = f"pr={code_change_id} head={head_sha[:7]}: {result.status.value}"
    if result.reason is not None and result.detail is not None:
        outcome += f" ({result.reason}: {result.detail})"
    elif result.reason is not None:
        outcome += f" ({result.reason})"
    if result.run_id is not None:
        outcome += f" run={result.run_id}"
    return outcome


@dataclass(frozen=True)
class RunInsertCandidate:
    ci: EligibilityCandidate
    origin_provider_installation_id: UUID
    repository_id: UUID
    workspace_id: UUID
    repository_external_id: int
    pr_number: int
    base_sha: str
    base_ref: str
    engine: str
    rule_version_id: UUID
    prompt_version_id: UUID
    severity_threshold: SeverityThresholdValue
    max_comments: int
    # The active rows when the run is created; ``None`` when no row of that key is active.
    conventions_prompt_version_id: UUID | None
    verifier_prompt_version_id: UUID | None
    provider: str = "github"

    def __post_init__(self) -> None:
        _validate_provider(self.provider)


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
    # Keep StoredRunMessage's inherited positional trigger constructor compatible.
    provider: str = field(default="github", kw_only=True)

    def __post_init__(self) -> None:
        _validate_provider(self.provider)

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
            provider=candidate.provider,
        )


def _validate_provider(provider: str) -> None:
    if type(provider) is not str or provider not in {"github", "gitlab"}:
        raise ValueError("Unsupported run provider")


class EligibilityChecker(Protocol):
    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
    ) -> CiEligibility: ...


class WebhookRunStore(Protocol):
    async def lock_candidate(self, code_change_id: UUID) -> RunInsertCandidate | CandidateMiss: ...

    async def insert_webhook_run(
        self, candidate: RunInsertCandidate, now: datetime
    ) -> PendingRunMessage | DuplicateReason | ActiveRunBlocker: ...

    async def notify_run_updated(self, run_id: UUID, workspace_id: UUID, status: str) -> None: ...

    async def mark_published(self, run_id: UUID, now: datetime) -> None: ...

    async def pending_messages(self, limit: int) -> tuple[PendingRunMessage, ...]: ...


class WebhookRunUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> WebhookRunStore: ...


class RunMessagePublisher(Protocol):
    """Return only after the outgoing run pointer is durably confirmed."""

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None: ...


class CancellationSignalReplay(Protocol):
    async def replay_pending(self, *, limit: int = 100) -> int: ...


class TryEnqueueWebhookRun:
    def __init__(
        self,
        *,
        eligibility: EligibilityChecker,
        uow_factory: Callable[[], WebhookRunUnitOfWork],
        publisher: RunMessagePublisher,
        cancellation_signals: CancellationSignalReplay | None = None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._eligibility = eligibility
        self._uow_factory = uow_factory
        self._publisher = publisher
        self._cancellation_signals = cancellation_signals
        self._now = now

    async def execute(
        self,
        code_change_id: UUID,
        expected_head_sha: str,
        *,
        expected_installation_external_id: int | None = None,
        expected_identity: WebhookRunIdentity | None = None,
    ) -> EnqueueResult:
        # Receipt paths require their origin; current-state sweeps have no receipt expectation.
        decision = await self._eligibility.execute(
            code_change_id,
            expected_head_sha,
            expected_installation_external_id=expected_installation_external_id,
        )
        if not decision.eligible or decision.candidate is None:
            return EnqueueResult(
                EnqueueStatus.INELIGIBLE, reason=decision.reason.value, detail=decision.detail
            )
        if expected_identity is not None and decision.candidate.identity != expected_identity:
            return EnqueueResult(EnqueueStatus.STALE, reason=STATE_CHANGED_REASON)
        async with self._uow_factory() as uow:
            candidate = await uow.runs.lock_candidate(code_change_id)
            if isinstance(candidate, CandidateMiss):
                status = (
                    EnqueueStatus.UNCONFIGURED
                    if candidate in _UNCONFIGURED
                    else EnqueueStatus.STALE
                )
                return EnqueueResult(status, reason=candidate.value)
            if (
                candidate.ci != decision.candidate
                or not _consistent_identity(candidate, expected_identity)
                or (
                    expected_installation_external_id is not None
                    and candidate.ci.installation_external_id != expected_installation_external_id
                )
            ):
                return EnqueueResult(EnqueueStatus.STALE, reason=STATE_CHANGED_REASON)
            message = await uow.runs.insert_webhook_run(candidate, self._now())
            if isinstance(message, ActiveRunBlocker):
                status = (
                    EnqueueStatus.DEFERRED
                    if message.head_sha != candidate.ci.head_sha and message.cancel_requested
                    else EnqueueStatus.DUPLICATE
                )
                return EnqueueResult(status, reason=DuplicateReason.ACTIVE_RUN.value)
            if isinstance(message, DuplicateReason):
                return EnqueueResult(EnqueueStatus.DUPLICATE, reason=message.value)
            await uow.runs.notify_run_updated(message.run_id, message.workspace_id, "queued")
            await uow.commit()

        if not await self._publish_and_mark(message):
            return EnqueueResult(EnqueueStatus.PUBLICATION_PENDING, message.run_id)
        return EnqueueResult(EnqueueStatus.ENQUEUED, message.run_id)

    async def replay_pending_publications(self, *, limit: int = 100) -> int:
        confirmed = 0
        if self._cancellation_signals is not None:
            try:
                confirmed += await self._cancellation_signals.replay_pending(limit=limit)
            except Exception:
                _LOGGER.exception("Cancellation signal replay failed; ordinary replay continues")
        async with self._uow_factory() as uow:
            pending = await uow.runs.pending_messages(limit)
        for message in pending:
            if await self._publish_and_mark(message):
                confirmed += 1
        return confirmed

    async def _publish_and_mark(self, message: PendingRunMessage) -> bool:
        try:
            await self._publisher.publish_confirmed(message)
        except Exception:
            _LOGGER.exception("Run publication remains pending for run %s", message.run_id)
            return False
        async with self._uow_factory() as uow:
            await uow.runs.mark_published(message.run_id, self._now())
            await uow.commit()
        return True


def _consistent_identity(
    candidate: RunInsertCandidate, expected: WebhookRunIdentity | None
) -> bool:
    if (
        type(candidate.origin_provider_installation_id) is not UUID
        or type(candidate.workspace_id) is not UUID
        or type(candidate.repository_id) is not UUID
        or type(candidate.repository_external_id) is not int
        or not 0 < candidate.repository_external_id <= 9223372036854775807
    ):
        return False
    identity = candidate.ci.identity
    if identity is None:
        # Compatibility for existing GitHub callers/fakes only, never GitLab authority.
        return candidate.provider == "github" and expected is None
    return (
        (expected is None or identity == expected)
        and candidate.provider == identity.provider
        and candidate.origin_provider_installation_id == identity.origin_provider_installation_id
        and candidate.workspace_id == identity.workspace_id
        and candidate.repository_id == identity.repository_id
        and candidate.repository_external_id == identity.repository_external_id
    )
