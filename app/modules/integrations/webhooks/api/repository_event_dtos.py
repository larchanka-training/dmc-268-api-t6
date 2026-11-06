"""Parse the GitHub events that change a repository's saved visibility (#454)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationSuspendedEvent,
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


class _InstallationSuspendedDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    installation: _Id


def parse_installation_suspended_event(
    payload: Mapping[str, object],
) -> InstallationSuspendedEvent:
    parsed = _InstallationSuspendedDelivery.model_validate(payload)
    return InstallationSuspendedEvent(installation_external_id=parsed.installation.id)


def parse_repository_visibility_event(payload: Mapping[str, object]) -> RepositoryVisibilityEvent:
    parsed = _RepositoryVisibilityDelivery.model_validate(payload)
    return RepositoryVisibilityEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        private=parsed.action == "privatized",
    )
