"""Validate GitHub installation payloads at the webhook transport boundary."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.modules.repositories.application.installation_repositories import (
    InstallationRepositoriesEvent,
    RepositorySnapshot,
)


class InstallationEventValidationError(ValueError):
    """A supported installation action has an invalid transport payload."""


class UnsupportedInstallationAction(InstallationEventValidationError):
    """A well-formed installation envelope names an action we do not process."""

    def __init__(self, action: str) -> None:
        super().__init__(f"unsupported installation action: {action}")
        self.action = action


_STRICT_GITHUB_PAYLOAD = ConfigDict(extra="ignore", frozen=True, strict=True)


class _GitHubInstallation(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    external_id: int = Field(alias="id", ge=1)


class _InstallationEnvelope(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: str = Field(min_length=1)
    installation: _GitHubInstallation


class _RepositoryDto(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    external_id: int = Field(alias="id", ge=1)
    full_name: str = Field(min_length=1, max_length=512)
    default_branch: str = Field(min_length=1, max_length=255)
    web_url: str = Field(alias="html_url", min_length=1)

    def to_application(self) -> RepositorySnapshot:
        return RepositorySnapshot(
            external_id=self.external_id,
            full_name=self.full_name,
            default_branch=self.default_branch,
            web_url=self.web_url,
        )


class _InstallationSnapshotPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["created", "deleted"]
    installation: _GitHubInstallation
    repositories: list[_RepositoryDto]


class _InstallationRepositoriesAddedPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["added"]
    installation: _GitHubInstallation
    repositories_added: list[_RepositoryDto]


class _InstallationRepositoriesRemovedPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["removed"]
    installation: _GitHubInstallation
    repositories_removed: list[_RepositoryDto]


def parse_installation_repositories_event(
    *, event_name: str, payload: Mapping[str, object]
) -> InstallationRepositoriesEvent:
    """Parse supported events, ignoring extra fields while validating required fields."""
    if event_name not in {"installation", "installation_repositories"}:
        raise InstallationEventValidationError(f"unsupported GitHub event: {event_name}")
    try:
        envelope = _InstallationEnvelope.model_validate(payload)
    except ValidationError as error:
        raise InstallationEventValidationError("invalid installation repository payload") from error
    if event_name == "installation" and envelope.action not in {"created", "deleted"}:
        raise UnsupportedInstallationAction(envelope.action)
    if event_name == "installation_repositories" and envelope.action not in {
        "added",
        "removed",
    }:
        raise UnsupportedInstallationAction(envelope.action)
    if event_name == "installation":
        try:
            parsed = _InstallationSnapshotPayload.model_validate(payload)
        except ValidationError as error:
            raise InstallationEventValidationError(
                "invalid installation repository payload"
            ) from error
        repositories = tuple(item.to_application() for item in parsed.repositories)
        if parsed.action == "created":
            return InstallationRepositoriesEvent(
                installation_external_id=parsed.installation.external_id,
                action="created",
                added_repositories=repositories,
                removed_repositories=(),
            )
        return InstallationRepositoriesEvent(
            installation_external_id=parsed.installation.external_id,
            action="deleted",
            added_repositories=(),
            removed_repositories=repositories,
        )

    action = envelope.action
    try:
        if action == "added":
            added = _InstallationRepositoriesAddedPayload.model_validate(payload)
            return InstallationRepositoriesEvent(
                installation_external_id=added.installation.external_id,
                action="added",
                added_repositories=tuple(
                    item.to_application() for item in added.repositories_added
                ),
                removed_repositories=(),
            )
        if action == "removed":
            removed = _InstallationRepositoriesRemovedPayload.model_validate(payload)
            return InstallationRepositoriesEvent(
                installation_external_id=removed.installation.external_id,
                action="removed",
                added_repositories=(),
                removed_repositories=tuple(
                    item.to_application() for item in removed.repositories_removed
                ),
            )
    except ValidationError as error:
        raise InstallationEventValidationError("invalid installation repository payload") from error

    raise AssertionError("validated installation action was not handled")
