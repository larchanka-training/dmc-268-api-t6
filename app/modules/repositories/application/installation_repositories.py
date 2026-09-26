"""Typed GitHub installation repository snapshots."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.common.application.languages import classify_languages


class InstallationEventValidationError(ValueError):
    """A webhook is not one of the repository-sync events supported by the service."""


_STRICT_GITHUB_PAYLOAD = ConfigDict(extra="ignore", frozen=True, strict=True)


class _GitHubInstallation(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    external_id: int = Field(alias="id", ge=1)


class RepositorySnapshot(BaseModel):
    """The GitHub repository fields required to synchronize a local repository."""

    model_config = _STRICT_GITHUB_PAYLOAD

    external_id: int = Field(alias="id", ge=1)
    full_name: str = Field(min_length=1, max_length=512)
    default_branch: str = Field(min_length=1, max_length=255)
    web_url: str = Field(alias="html_url", min_length=1)


class _InstallationSnapshotPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["created", "deleted"]
    installation: _GitHubInstallation
    # GitHub sends JSON arrays.  Keep them as lists at the transport boundary,
    # then expose immutable tuples in the normalized application event.
    repositories: list[RepositorySnapshot]


class _InstallationRepositoriesAddedPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["added"]
    installation: _GitHubInstallation
    repositories_added: list[RepositorySnapshot]


class _InstallationRepositoriesRemovedPayload(BaseModel):
    model_config = _STRICT_GITHUB_PAYLOAD

    action: Literal["removed"]
    installation: _GitHubInstallation
    repositories_removed: list[RepositorySnapshot]


@dataclass(frozen=True)
class InstallationRepositoriesEvent:
    """Normalized repository changes from a supported GitHub installation webhook."""

    installation_external_id: int
    action: Literal["created", "deleted", "added", "removed"]
    added_repositories: tuple[RepositorySnapshot, ...]
    removed_repositories: tuple[RepositorySnapshot, ...]


@dataclass(frozen=True)
class RepositoryTreeBlob:
    """One default-branch Git tree entry used for language classification."""

    path: str
    size: int
    entry_type: Literal["blob", "tree", "commit"]


def parse_installation_repositories_event(
    *, event_name: str, payload: Mapping[str, object]
) -> InstallationRepositoriesEvent:
    """Parse the four GitHub events that synchronize installed repositories.

    Unknown payload fields are deliberately ignored because GitHub adds fields to
    webhook payloads.  The fields that form the persistence contract remain strict.
    """
    if event_name not in {"installation", "installation_repositories"}:
        raise InstallationEventValidationError(f"unsupported GitHub event: {event_name}")
    if event_name == "installation":
        try:
            parsed = _InstallationSnapshotPayload.model_validate(payload)
        except ValidationError as error:
            raise InstallationEventValidationError(
                "invalid installation repository payload"
            ) from error
        if parsed.action == "created":
            return InstallationRepositoriesEvent(
                installation_external_id=parsed.installation.external_id,
                action="created",
                added_repositories=tuple(parsed.repositories),
                removed_repositories=(),
            )
        return InstallationRepositoriesEvent(
            installation_external_id=parsed.installation.external_id,
            action="deleted",
            added_repositories=(),
            removed_repositories=tuple(parsed.repositories),
        )

    action = payload.get("action")
    try:
        if action == "added":
            parsed_added = _InstallationRepositoriesAddedPayload.model_validate(payload)
            return InstallationRepositoriesEvent(
                installation_external_id=parsed_added.installation.external_id,
                action="added",
                added_repositories=tuple(parsed_added.repositories_added),
                removed_repositories=(),
            )
        if action == "removed":
            parsed_removed = _InstallationRepositoriesRemovedPayload.model_validate(payload)
            return InstallationRepositoriesEvent(
                installation_external_id=parsed_removed.installation.external_id,
                action="removed",
                added_repositories=(),
                removed_repositories=tuple(parsed_removed.repositories_removed),
            )
    except ValidationError as error:
        raise InstallationEventValidationError("invalid installation repository payload") from error

    raise InstallationEventValidationError(
        f"unsupported action {action!r} for GitHub event {event_name!r}"
    )


def classify_tree_languages(blobs: tuple[RepositoryTreeBlob, ...]) -> dict[str, int]:
    """Classify only Git blob entries through the shared language contract."""
    return classify_languages(blob for blob in blobs if blob.entry_type == "blob")
