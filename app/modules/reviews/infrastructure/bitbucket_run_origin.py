"""Fail-closed authority predicate for persisted Bitbucket Run pointers."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ColumnElement, exists, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
    BitbucketRunOrigin,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_origin import active_origin_condition


def active_bitbucket_run_origin(*, require_current_credential: bool = True) -> ColumnElement[bool]:
    """A Run must match the *same* currently active remote UUID and binding epoch."""
    credential_generation = (
        (BitbucketConnection.credential_generation == BitbucketRunOrigin.credential_generation,)
        if require_current_credential
        else ()
    )
    return exists(
        select(BitbucketRunOrigin.run_id)
        .select_from(BitbucketRunOrigin)
        .join(
            BitbucketRepositoryIdentity,
            BitbucketRepositoryIdentity.repository_id == BitbucketRunOrigin.repository_id,
        )
        .join(
            BitbucketConnection,
            BitbucketConnection.provider_installation_id
            == BitbucketRunOrigin.provider_installation_id,
        )
        .join(
            BitbucketRepositoryWebhookSecret,
            BitbucketRepositoryWebhookSecret.repository_id == BitbucketRunOrigin.repository_id,
        )
        .where(
            ProviderInstallation.provider == "bitbucket",
            BitbucketRunOrigin.run_id == Run.id,
            BitbucketRunOrigin.workspace_id == ProviderInstallation.workspace_id,
            BitbucketRunOrigin.provider_installation_id == ProviderInstallation.id,
            BitbucketRunOrigin.repository_id == Repository.id,
            BitbucketRunOrigin.repository_uuid == BitbucketRepositoryIdentity.repository_uuid,
            BitbucketRunOrigin.pr_number == CodeChange.external_number,
            BitbucketRunOrigin.head_sha == Run.head_sha,
            BitbucketRepositoryIdentity.workspace_id == BitbucketRunOrigin.workspace_id,
            BitbucketRepositoryIdentity.provider_installation_id
            == BitbucketRunOrigin.provider_installation_id,
            BitbucketConnection.workspace_id == BitbucketRunOrigin.workspace_id,
            *credential_generation,
            BitbucketConnection.revoked_at.is_(None),
            BitbucketConnection.token_ciphertext.is_not(None),
            BitbucketRepositoryWebhookSecret.binding_generation
            == BitbucketRunOrigin.binding_generation,
            BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
        )
        .correlate(Run, Repository, ProviderInstallation, CodeChange)
    )


async def bitbucket_run_origin_active(session: AsyncSession, run_id: UUID) -> bool:
    """Check the immutable Bitbucket Run snapshot against current binding authority."""
    return (
        await session.scalar(
            select(Run.id)
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Run.origin_provider_installation_id,
            )
            .where(Run.id == run_id, active_origin_condition(), active_bitbucket_run_origin())
        )
        is not None
    )


async def bitbucket_rerun_origin_continuous(session: AsyncSession, run_id: UUID) -> bool:
    """Allow new generations only while the original repository binding epoch survives."""
    return (
        await session.scalar(
            select(Run.id)
            .select_from(Run)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Run.origin_provider_installation_id,
            )
            .where(
                Run.id == run_id,
                active_origin_condition(),
                active_bitbucket_run_origin(require_current_credential=False),
            )
        )
        is not None
    )
