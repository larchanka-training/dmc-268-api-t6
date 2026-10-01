"""Transport models of the queue messages (contracts/schemas/review.{run,publish}.v1)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.modules.reviews.application.queue_messages import ReviewPublishPointer, message_trigger
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.application.verdict import publish_message_id


class _Message(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)

    def wire(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


class RepoRef(_Message):
    id: UUID
    provider: str
    external_id: int
    full_name: str


class PullRef(_Message):
    number: int
    head_sha: str
    base_sha: str
    base_ref: str


class ReviewRunMessage(_Message):
    schema_name: Literal["review.run/v1"] = Field(default="review.run/v1", alias="schema")
    message_id: UUID
    run_id: UUID
    workspace_id: UUID
    installation_id: int
    repo: RepoRef
    pr: PullRef
    engine: Literal["fast", "deep"]
    rule_version_id: UUID
    prompt_version_id: UUID
    trigger: Literal["webhook", "manual", "rerun", "dry_run"]
    attempt: int
    requested_at: datetime

    @classmethod
    def from_pending(cls, message: PendingRunMessage) -> ReviewRunMessage:
        return cls(
            message_id=message.run_id,
            run_id=message.run_id,
            workspace_id=message.workspace_id,
            installation_id=message.installation_id,
            repo=RepoRef(
                id=message.repository_id,
                provider="github",
                external_id=message.repository_external_id,
                full_name=message.repository_full_name,
            ),
            pr=PullRef(
                number=message.pr_number,
                head_sha=message.head_sha,
                base_sha=message.base_sha,
                base_ref=message.base_ref,
            ),
            engine=message.engine,  # type: ignore[arg-type]
            rule_version_id=message.rule_version_id,
            prompt_version_id=message.prompt_version_id,
            trigger=message_trigger(message),  # type: ignore[arg-type]
            attempt=message.attempt,
            requested_at=message.requested_at.astimezone(UTC),
        )


class ReviewPublishMessage(_Message):
    schema_name: Literal["review.publish/v1"] = Field(default="review.publish/v1", alias="schema")
    message_id: UUID
    run_id: UUID
    head_sha: str
    findings_hash: str
    review_event: Literal["COMMENT", "REQUEST_CHANGES"]

    @classmethod
    def from_pointer(cls, pointer: ReviewPublishPointer) -> ReviewPublishMessage:
        return cls(
            message_id=publish_message_id(pointer.run_id, pointer.head_sha, pointer.findings_hash),
            run_id=pointer.run_id,
            head_sha=pointer.head_sha,
            findings_hash=pointer.findings_hash,
            review_event=pointer.review_event,  # type: ignore[arg-type]
        )

    def to_pointer(self) -> ReviewPublishPointer:
        return ReviewPublishPointer(
            self.run_id, self.head_sha, self.findings_hash, self.review_event
        )
