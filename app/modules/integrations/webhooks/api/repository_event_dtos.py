"""Parse the GitHub events that change a repository's saved visibility or name (#454)."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    REPOSITORY_FULL_NAME_PATTERN,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationSuspendedEvent,
    RepositoryRenamedEvent,
    RepositoryTransferredEvent,
    RepositoryVisibilityEvent,
)


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


class _NamedRepository(_Id):
    full_name: str = Field(
        pattern=REPOSITORY_FULL_NAME_PATTERN, max_length=REPOSITORY_FULL_NAME_MAX_LENGTH
    )
    # An invalid value reads as absent: the name alone is what the badge needs.
    html_url: str | None = None

    @field_validator("html_url", mode="before")
    @classmethod
    def _url_or_none(cls, value: object) -> str | None:
        return value if isinstance(value, str) and value else None


class _RepositoryRenamedDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    installation: _Id
    repository: _NamedRepository


class _RepositoryTransferredDelivery(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    repository: _Id


def parse_installation_suspended_event(
    payload: Mapping[str, object],
) -> InstallationSuspendedEvent:
    parsed = _InstallationSuspendedDelivery.model_validate(payload)
    return InstallationSuspendedEvent(installation_external_id=parsed.installation.id)


def _parse_visibility(payload: Mapping[str, object]) -> RepositoryVisibilityEvent:
    parsed = _RepositoryVisibilityDelivery.model_validate(payload)
    return RepositoryVisibilityEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        private=parsed.action == "privatized",
    )


def _parse_renamed(payload: Mapping[str, object]) -> RepositoryRenamedEvent:
    parsed = _RepositoryRenamedDelivery.model_validate(payload)
    return RepositoryRenamedEvent(
        installation_external_id=parsed.installation.id,
        repository_external_id=parsed.repository.id,
        full_name=parsed.repository.full_name,
        web_url=parsed.repository.html_url,
    )


def _parse_transferred(payload: Mapping[str, object]) -> RepositoryTransferredEvent:
    parsed = _RepositoryTransferredDelivery.model_validate(payload)
    return RepositoryTransferredEvent(repository_external_id=parsed.repository.id)


# The ``repository`` actions the service acts on; the others end as irrelevant.
REPOSITORY_EVENT_PARSERS: Mapping[
    str,
    Callable[
        [Mapping[str, object]],
        RepositoryVisibilityEvent | RepositoryRenamedEvent | RepositoryTransferredEvent,
    ],
] = {
    "privatized": _parse_visibility,
    "publicized": _parse_visibility,
    "renamed": _parse_renamed,
    "transferred": _parse_transferred,
}
