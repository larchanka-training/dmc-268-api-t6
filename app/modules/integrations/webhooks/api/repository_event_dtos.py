"""Parse the GitHub ``repository`` webhook at the inbound transport boundary (#454)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    RepositoryVisibilityEvent,
)

REPOSITORY_VISIBILITY_ACTIONS = frozenset({"privatized", "publicized"})


class _Id(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    id: int = Field(gt=0, le=2**63 - 1)


class _RepositoryVisibilityDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    action: Literal["privatized", "publicized"]
    installation: _Id
    repository: _Id


def parse_repository_visibility_event(payload: Mapping[str, object]) -> RepositoryVisibilityEvent:
    parsed = _RepositoryVisibilityDelivery.model_validate(payload)
    return RepositoryVisibilityEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        private=parsed.action == "privatized",
    )
