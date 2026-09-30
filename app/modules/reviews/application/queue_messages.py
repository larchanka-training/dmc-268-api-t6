"""Outgoing queue ports besides ``RunMessagePublisher`` (docs/PIPELINE_SPEC.md §12)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage


@dataclass(frozen=True)
class StoredRunMessage(PendingRunMessage):
    """A ``review.run/v1`` pointer rebuilt from ``runs``; keeps the Run's own ``trigger``.

    Webhook publications (#11) carry no trigger field and are always ``webhook``.
    """

    trigger: str = "webhook"


def message_trigger(message: PendingRunMessage) -> str:
    return message.trigger if isinstance(message, StoredRunMessage) else "webhook"


@dataclass(frozen=True)
class ReviewPublishPointer:
    """The ``review.publish/v1`` pointer (T8, T17)."""

    run_id: UUID
    head_sha: str
    findings_hash: str
    review_event: str


class ReviewPublishQueue(Protocol):
    """Return only after the broker confirmed the ``review.publish/v1`` message."""

    async def publish_review(self, pointer: ReviewPublishPointer) -> None: ...


class RunRetryQueue(Protocol):
    """Publish a copy of ``review.run/v1`` into ``retry.{delay}.{engine}`` with confirms (T9)."""

    async def publish_retry(self, message: PendingRunMessage, delay_key: str) -> None: ...
