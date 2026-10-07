"""Typed installation repository events and language shares."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.common.application.languages import classify_languages


@dataclass(frozen=True)
class RepositorySnapshot:
    """The repository fields needed to synchronize a local repository."""

    external_id: int
    full_name: str
    default_branch: str
    web_url: str


@dataclass(frozen=True)
class RepositoryReference:
    """A repository as an installation webhook names it.

    GitHub sends only ``id`` and ``full_name`` (plus ``name``/``private``) for the
    repositories of ``installation`` and ``installation_repositories`` events, so the
    default branch and web URL are usually absent and are read from GitHub later.
    """

    external_id: int
    full_name: str
    default_branch: str | None
    web_url: str | None


@dataclass(frozen=True)
class InstallationRepositoriesEvent:
    """Normalized repository changes from a supported GitHub installation webhook."""

    installation_external_id: int
    action: Literal["created", "deleted", "added", "removed"]
    added_repositories: tuple[RepositoryReference, ...]
    removed_repositories: tuple[RepositoryReference, ...]
    delivery_id: str | None = None


@dataclass(frozen=True)
class RepositoryTreeBlob:
    """One default-branch Git tree entry used for language classification."""

    path: str
    size: int
    entry_type: Literal["blob", "tree", "commit"]


def classify_tree_languages(blobs: tuple[RepositoryTreeBlob, ...]) -> dict[str, int]:
    """Classify only Git blob entries through the shared language contract."""
    return classify_languages(blob for blob in blobs if blob.entry_type == "blob")
