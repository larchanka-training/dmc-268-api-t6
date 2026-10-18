"""Parse the GitHub ``push`` webhook at the inbound transport boundary (#150)."""

from __future__ import annotations

from collections.abc import Mapping

from pydantic import BaseModel, ConfigDict, Field

from app.modules.integrations.webhooks.application.github_installation_dispatch import PushEvent


class _Id(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)


class _Repository(_Id):
    default_branch: str = Field(min_length=1, max_length=255)


class _PushDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    ref: str = Field(min_length=1, max_length=1024)
    installation: _Id
    repository: _Repository


def parse_push_event(payload: Mapping[str, object]) -> PushEvent:
    parsed = _PushDelivery.model_validate(payload)
    return PushEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        ref=parsed.ref,
        default_branch=parsed.repository.default_branch,
    )
