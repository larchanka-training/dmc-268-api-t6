"""Validated JSON boundary for the immutable final review publication plan."""

from __future__ import annotations

from typing import Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator

from app.modules.reviews.application.review_output import PublishedFinding


class SavedPublicationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str
    idempotency_key: str
    review_event: Literal["COMMENT", "REQUEST_CHANGES"]
    findings_hash: str
    review_body: str
    finding_ids: tuple[UUID, ...]
    findings: tuple[PublishedFinding, ...]

    @model_validator(mode="after")
    def validate_finding_ids(self) -> Self:
        ids = self.finding_ids
        inline_ids = self.inline_finding_ids
        if (
            len(ids) != len(set(ids))
            or len(inline_ids) != len(set(inline_ids))
            or not set(inline_ids) <= set(ids)
        ):
            raise ValueError("saved publication plan has inconsistent finding IDs")
        return self

    @property
    def inline_finding_ids(self) -> tuple[UUID, ...]:
        ids = tuple(finding.finding_id for finding in self.findings)
        if any(finding_id is None for finding_id in ids):
            raise ValueError("saved publication finding has no ID")
        return tuple(finding_id for finding_id in ids if finding_id is not None)

    @classmethod
    def from_json(cls, value: object) -> Self:
        return cls.model_validate(value)

    def to_json(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
