"""Re-evaluate current PR eligibility after actionable PR or CI deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    describe_enqueue,
)

_NO_OPEN_PULL_REQUEST = "no open pull request"
# CI of a head that is no longer current (it finished after a push) also ends up here.
_NO_OPEN_PULL_REQUEST_AT_HEAD = "no open pull request at this head"
_NOT_AN_AI_REVIEW_LABELED_ACTION = "not an ai-review labeled action"


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


class WebhookRunEnqueuer(Protocol):
    async def execute(self, code_change_id: UUID, expected_head_sha: str) -> EnqueueResult: ...


class TriggerFromDelivery:
    """Re-evaluate eligibility and return a one-line outcome for the delivery log."""

    def __init__(self, *, targets: RunTriggerTargets, enqueuer: WebhookRunEnqueuer) -> None:
        self._targets = targets
        self._enqueuer = enqueuer

    async def on_pr(self, event: PullRequestEvent) -> str:
        target = await self._targets.for_pr(event)
        if target is None:
            return _NO_OPEN_PULL_REQUEST
        return await self._enqueue(target.code_change_id, target.head_sha)

    async def on_label(self, event: PullRequestLabelEvent) -> str:
        if event.label_name != "ai-review" or event.pull_request.action != "labeled":
            return _NOT_AN_AI_REVIEW_LABELED_ACTION
        return await self.on_pr(event.pull_request)

    async def on_ci(self, event: CiTriggerEvent) -> str:
        targets = await self._targets.for_ci(event)
        if not targets:
            return _NO_OPEN_PULL_REQUEST_AT_HEAD
        return "; ".join([await self._enqueue(target, event.head_sha) for target in targets])

    async def _enqueue(self, code_change_id: UUID, head_sha: str) -> str:
        result = await self._enqueuer.execute(code_change_id, head_sha)
        return describe_enqueue(code_change_id, head_sha, result)
