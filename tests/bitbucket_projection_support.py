"""Shared canonical Bitbucket projection test doubles."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID, uuid4

from app.modules.integrations.bitbucket.application.request_credentials import BoundBitbucketTarget
from app.modules.reviews.application.determine_ci_eligibility import WebhookRunIdentity
from app.modules.reviews.application.project_bitbucket_pull_request import (
    CanonicalBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    EnqueueResult,
    EnqueueStatus,
    PendingRunMessage,
    RunPublicationKind,
)

NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)


@dataclass
class Current:
    head: str = "a" * 40
    updated_at: datetime = NOW
    title: str = "First"

    async def get_current(
        self, target: BoundBitbucketTarget, number: int
    ) -> CanonicalBitbucketPullRequest:
        return CanonicalBitbucketPullRequest(
            target.repository_uuid,
            number,
            "open",
            self.title,
            None,
            "reviewer",
            "feature",
            "main",
            "b" * 40,
            self.head,
            self.updated_at,
            f"https://bitbucket.org/team/repo/pull-requests/{number}",
        )


@dataclass
class Enqueuer:
    heads: list[str] = field(default_factory=list)

    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        assert expected_identity.provider == "bitbucket"
        self.heads.append(expected_head_sha)
        return EnqueueResult(EnqueueStatus.ENQUEUED, uuid4())


@dataclass
class Publisher:
    messages: list[PendingRunMessage] = field(default_factory=list)
    fail_heads: set[str] = field(default_factory=set)

    async def publish_confirmed(
        self, message: PendingRunMessage, *, kind: RunPublicationKind = RunPublicationKind.QUEUED
    ) -> None:
        assert message.provider == "bitbucket" and kind == RunPublicationKind.QUEUED
        self.messages.append(message)
        if message.head_sha in self.fail_heads:
            raise RuntimeError("synthetic publisher failure")


class FailingEnqueuer:
    async def execute(
        self, code_change_id: UUID, expected_head_sha: str, *, expected_identity: WebhookRunIdentity
    ) -> EnqueueResult:
        raise RuntimeError("synthetic crash after PR projection")
