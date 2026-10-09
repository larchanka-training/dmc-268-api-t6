"""Re-evaluate current PR eligibility after actionable PR or CI deliveries."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
    describe_enqueue,
)

_NO_OPEN_PULL_REQUEST = "no open pull request"
# CI of a head that is no longer current (it finished after a push) also ends up here.
_NO_OPEN_PULL_REQUEST_AT_HEAD = "no open pull request at this head"
_NOT_AN_AI_REVIEW_LABELED_ACTION = "not an ai-review labeled action"


@dataclass(frozen=True)
class RunTriggerOutcome:
    """Rendered log detail plus typed retry intent; strings never control retries."""

    detail: str
    retry_required: bool = False


@dataclass(frozen=True)
class CiTriggerEvent:
    installation_external_id: int
    repository_external_id: int
    head_sha: str
    event_name: str = "ci"


@dataclass(frozen=True)
class ProjectedPullRequestTarget:
    code_change_id: UUID
    head_sha: str


class RunTriggerTargets(Protocol):
    async def for_pr(self, event: PullRequestEvent) -> ProjectedPullRequestTarget | None: ...

    async def for_ci(self, event: CiTriggerEvent) -> tuple[UUID, ...]: ...


class RunTriggerUnitOfWork(UnitOfWork, Protocol):
    @property
    def targets(self) -> RunTriggerTargets: ...


class WebhookRunEnqueuer(Protocol):
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult: ...


class TriggerFromDelivery:
    """Re-evaluate eligibility and return log detail with retry intent for the delivery log."""

    def __init__(
        self,
        *,
        enqueuer: WebhookRunEnqueuer,
        uow_factory: Callable[[], RunTriggerUnitOfWork],
    ) -> None:
        self._enqueuer = enqueuer
        self._uow_factory = uow_factory

    async def on_pr(self, event: PullRequestEvent) -> RunTriggerOutcome:
        async with self._uow_factory() as uow:
            target = await uow.targets.for_pr(event)
        if target is None:
            return RunTriggerOutcome(_NO_OPEN_PULL_REQUEST)
        return await self._enqueue(target.code_change_id, target.head_sha)

    async def on_label(self, event: PullRequestLabelEvent) -> RunTriggerOutcome:
        if event.label_name != "ai-review" or event.pull_request.action != "labeled":
            return RunTriggerOutcome(_NOT_AN_AI_REVIEW_LABELED_ACTION)
        return await self.on_pr(event.pull_request)

    async def on_ci(self, event: CiTriggerEvent) -> RunTriggerOutcome:
        async with self._uow_factory() as uow:
            targets = await uow.targets.for_ci(event)
            await uow.commit()
        if not targets:
            return RunTriggerOutcome(_NO_OPEN_PULL_REQUEST_AT_HEAD)
        outcomes = [await self._enqueue(target, event.head_sha) for target in targets]
        return RunTriggerOutcome(
            "; ".join(outcome.detail for outcome in outcomes),
            retry_required=any(outcome.retry_required for outcome in outcomes),
        )

    async def _enqueue(self, code_change_id: UUID, head_sha: str) -> RunTriggerOutcome:
        result = await self._enqueuer.execute(code_change_id, head_sha)
        return RunTriggerOutcome(
            describe_enqueue(code_change_id, head_sha, result),
            retry_required=result.status == EnqueueStatus.DEFERRED,
        )
