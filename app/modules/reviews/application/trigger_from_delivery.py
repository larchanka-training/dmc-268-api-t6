"""Re-evaluate current PR eligibility after actionable PR or CI deliveries."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.project_github_pull_request import (
    PullRequestEvent,
    PullRequestLabelEvent,
)
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult


@dataclass(frozen=True)
class CiTriggerEvent:
    installation_external_id: int
    repository_external_id: int
    head_sha: str


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
    def __init__(self, *, targets: RunTriggerTargets, enqueuer: WebhookRunEnqueuer) -> None:
        self._targets = targets
        self._enqueuer = enqueuer

    async def on_pr(self, event: PullRequestEvent) -> None:
        target = await self._targets.for_pr(event)
        if target is not None:
            await self._enqueuer.execute(target.code_change_id, target.head_sha)

    async def on_label(self, event: PullRequestLabelEvent) -> None:
        if event.label_name != "ai-review" or event.pull_request.action != "labeled":
            return
        await self.on_pr(event.pull_request)

    async def on_ci(self, event: CiTriggerEvent) -> None:
        for target in await self._targets.for_ci(event):
            await self._enqueuer.execute(target, event.head_sha)
