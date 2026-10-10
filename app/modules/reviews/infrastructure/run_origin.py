"""Immutable Run origin controls provider work; portal grants use current Repository."""

from __future__ import annotations

from sqlalchemy import ColumnElement, and_

from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.infrastructure.models import Run


def active_origin_condition() -> ColumnElement[bool]:
    return and_(
        Run.origin_provider_installation_id == ProviderInstallation.id,
        Repository.provider_installation_id == ProviderInstallation.id,
        ProviderInstallation.deleted_at.is_(None),
        Repository.installation_available.is_(True),
    )


def origin_active(run: Run, repository: Repository, installation: ProviderInstallation) -> bool:
    return (
        run.origin_provider_installation_id is not None
        and run.origin_provider_installation_id == installation.id
        and repository.provider_installation_id == installation.id
        and installation.deleted_at is None
        and repository.installation_available
    )
