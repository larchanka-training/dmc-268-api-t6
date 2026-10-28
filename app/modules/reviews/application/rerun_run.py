"""``POST /api/runs/{id}/rerun``: a new Run for the PR's current head (PIPELINE_SPEC T3)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.get_run import RunDetailRepository
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.try_enqueue_webhook_run import (
    CandidateMiss,
    PendingRunMessage,
    RunInsertCandidate,
    RunMessagePublisher,
)

_LOGGER = logging.getLogger(__name__)


class RerunOutcome(StrEnum):
    CREATED = "created"
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    NOT_CONFIGURED = "not_configured"
    ORIGIN_CHANGED = "origin_changed"


@dataclass(frozen=True)
class RerunResult:
    outcome: RerunOutcome
    message: PendingRunMessage | None = None


class RerunConflict(Exception):
    """The PR is closed/active or repository reviews are paused/unavailable; no Run created."""


class RerunNotConfigured(Exception):
    """The repository has no active rule or prompt version to review with."""


class RerunOriginConflict(RerunConflict):
    """The historical Run no longer authorizes this current repository origin."""


@dataclass(frozen=True)
class RerunOriginFacts:
    installation_id: UUID | None
    provider: str | None
    workspace_id: UUID | None
    account_external_id: int | None
    retired: bool


@dataclass(frozen=True)
class RerunFacts:
    """Detached rows read while the caller's source/PR/active-run locks remain held."""

    code_change_id: UUID
    original_repository_id: UUID
    rule_repository_id: UUID
    origin: RerunOriginFacts
    candidate: RunInsertCandidate | CandidateMiss
    current_origin: RerunOriginFacts | None
    current_repository_id: UUID | None
    pull_request_open: bool
    active_run: bool
    origin_active: bool


class RerunStore(Protocol):
    async def load_rerun_facts(self, run_id: UUID) -> RerunFacts | None:
        """Read current and historical metadata under source/PR/active-run locks."""
        ...

    async def insert_rerun(
        self, candidate: RunInsertCandidate, code_change_id: UUID, now: datetime
    ) -> PendingRunMessage:
        """Persist the application-selected candidate; flushes only."""
        ...

    async def mark_rerun_published(self, run_id: UUID, now: datetime) -> None: ...


def _rerun_outcome(facts: RerunFacts) -> RerunOutcome:
    if not facts.pull_request_open or facts.active_run:
        return RerunOutcome.CONFLICT
    origin, current = facts.origin, facts.current_origin
    if origin.provider == "bitbucket" and not facts.origin_active:
        return RerunOutcome.ORIGIN_CHANGED
    if (
        origin.installation_id is None
        or current is None
        or current.retired
        or facts.current_repository_id != facts.original_repository_id
    ):
        return RerunOutcome.ORIGIN_CHANGED
    if current.installation_id == origin.installation_id:
        if current.provider != origin.provider or current.workspace_id != origin.workspace_id:
            return RerunOutcome.ORIGIN_CHANGED
    elif (
        origin.provider != "github"
        or current.provider != "github"
        or not origin.retired
        or facts.current_repository_id != facts.rule_repository_id
        or current.workspace_id != origin.workspace_id
        or origin.account_external_id is None
        or current.account_external_id != origin.account_external_id
    ):
        return RerunOutcome.ORIGIN_CHANGED
    candidate = facts.candidate
    if not isinstance(candidate, RunInsertCandidate):
        if candidate in {CandidateMiss.MISSING_RULES, CandidateMiss.MISSING_PROMPT}:
            return RerunOutcome.NOT_CONFIGURED
        return RerunOutcome.ORIGIN_CHANGED
    if not candidate.ci.repository_enabled:
        return RerunOutcome.CONFLICT
    if (
        candidate.origin_provider_installation_id != current.installation_id
        or candidate.provider != current.provider
        or candidate.workspace_id != current.workspace_id
        or candidate.repository_id != facts.current_repository_id
    ):
        return RerunOutcome.ORIGIN_CHANGED
    return RerunOutcome.CREATED


async def create_rerun(store: RerunStore, run_id: UUID, now: datetime) -> RerunResult:
    """Decide T3 from locked raw facts, then persist within the caller's same UOW.

    Only a retired GitHub installation may be replaced: the owned repository,
    historical rule repository, workspace and external GitHub account must match.
    SQL adapters do not decide this exception and never receive an authority boolean.
    """
    facts = await store.load_rerun_facts(run_id)
    if facts is None:
        return RerunResult(RerunOutcome.NOT_FOUND)
    outcome = _rerun_outcome(facts)
    if outcome is not RerunOutcome.CREATED:
        return RerunResult(outcome)
    assert isinstance(facts.candidate, RunInsertCandidate)
    message = await store.insert_rerun(facts.candidate, facts.code_change_id, now)
    return RerunResult(RerunOutcome.CREATED, message)


class RerunUnitOfWork(UnitOfWork, Protocol):
    @property
    def runs(self) -> RerunStore: ...


class RerunRun:
    def __init__(
        self,
        uow_factory: Callable[[], RerunUnitOfWork],
        runs: RunDetailRepository,
        publisher: RunMessagePublisher | None,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._uow_factory = uow_factory
        self._runs = runs
        self._publisher = publisher
        self._now = now

    async def execute(self, run_id: UUID) -> RunListItem | None:
        async with self._uow_factory() as uow:
            result = await create_rerun(uow.runs, run_id, self._now())
            if result.outcome is RerunOutcome.CREATED:
                await uow.commit()
        if result.outcome is RerunOutcome.NOT_FOUND:
            return None
        if result.outcome is RerunOutcome.CONFLICT:
            raise RerunConflict
        if result.outcome is RerunOutcome.NOT_CONFIGURED:
            raise RerunNotConfigured
        if result.outcome is RerunOutcome.ORIGIN_CHANGED:
            raise RerunOriginConflict
        assert result.message is not None
        message = result.message
        if self._publisher is not None:
            try:
                # AMQP priority 9 comes from the ``rerun`` trigger of the message.
                await self._publisher.publish_confirmed(message)
            except Exception:
                # The reconciler republishes a queued Run after 10 minutes (T18).
                _LOGGER.exception("Rerun %s publication remains pending", message.run_id)
            else:
                async with self._uow_factory() as uow:
                    await uow.runs.mark_rerun_published(message.run_id, self._now())
                    await uow.commit()
        return await self._runs.get_run(message.run_id)
