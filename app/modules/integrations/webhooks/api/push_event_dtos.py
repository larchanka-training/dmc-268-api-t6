"""Parse the GitHub ``push`` webhook at the inbound transport boundary (#150, #143)."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.modules.integrations.webhooks.application.github_installation_dispatch import PushEvent

_SHA_PATTERN = r"^(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})$"


class _Id(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)


class _Repository(_Id):
    default_branch: str = Field(min_length=1, max_length=255)


class _PushDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    ref: str = Field(min_length=1, max_length=1024)
    # The head after the push; all zeros, like `deleted`, when the push deleted the ref.
    after: str | None = Field(default=None, pattern=_SHA_PATTERN)
    deleted: bool = False
    installation: _Id
    repository: _Repository


def parse_push_event(payload: Mapping[str, object]) -> PushEvent:
    parsed = _PushDelivery.model_validate(payload)
    removed = parsed.deleted or parsed.after is None or parsed.after.strip("0") == ""
    return PushEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        ref=parsed.ref,
        default_branch=parsed.repository.default_branch,
        after=None if removed else parsed.after,
    )
