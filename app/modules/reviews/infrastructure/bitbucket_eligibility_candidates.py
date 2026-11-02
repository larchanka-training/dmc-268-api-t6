"""Detached active Bitbucket candidate metadata under repository UUID authority."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.determine_ci_eligibility import (
    CiWaitMode,
    EligibilityCandidate,
    WebhookRunIdentity,
)
from app.modules.reviews.application.project_github_pull_request import PullRequestState
from app.modules.reviews.infrastructure.models import CodeChange


async def read_bitbucket_eligibility_candidate(
    session: AsyncSession,
    code_change_id: UUID,
    *,
    expected_identity: WebhookRunIdentity | None = None,
    require_confirmed_webhook: bool = True,
) -> EligibilityCandidate | None:
    row = (
        await session.execute(
            select(
                CodeChange,
                Repository,
                ProviderInstallation,
                BitbucketRepositoryIdentity.repository_uuid,
                BitbucketConnection.credential_generation,
                BitbucketRepositoryWebhookSecret.binding_generation,
                BitbucketRepositoryWebhookSecret.generation,
            )
            .select_from(CodeChange)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Repository.provider_installation_id,
            )
            .join(
                BitbucketRepositoryIdentity,
                BitbucketRepositoryIdentity.repository_id == Repository.id,
            )
            .join(
                BitbucketConnection,
                BitbucketConnection.provider_installation_id == ProviderInstallation.id,
            )
            .join(
                BitbucketRepositoryWebhookSecret,
                BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
            )
            .where(
                CodeChange.id == code_change_id,
                ProviderInstallation.provider == "bitbucket",
                ProviderInstallation.deleted_at.is_(None),
                Repository.enabled.is_(True),
                Repository.installation_available.is_(True),
                BitbucketRepositoryIdentity.provider_installation_id == ProviderInstallation.id,
                BitbucketRepositoryIdentity.workspace_id == ProviderInstallation.workspace_id,
                BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
                BitbucketConnection.revoked_at.is_(None),
                BitbucketConnection.token_ciphertext.is_not(None),
                BitbucketRepositoryWebhookSecret.secret_ciphertext.is_not(None),
                BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
                *(
                    (BitbucketRepositoryWebhookSecret.confirmed_at.is_not(None),)
                    if require_confirmed_webhook
                    else ()
                ),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    change, repository, installation, repository_uuid, credential, binding, webhook = row
    identity = WebhookRunIdentity(
        "bitbucket",
        installation.id,
        installation.workspace_id,
        repository.id,
        repository.external_id,
        credential_generation=credential,
        repository_uuid=repository_uuid,
        binding_generation=binding,
        webhook_generation=webhook,
    )
    if expected_identity is not None and identity != expected_identity:
        return None
    return EligibilityCandidate(
        code_change_id=change.id,
        installation_external_id=installation.external_id,
        repository_full_name=repository.full_name,
        head_sha=change.head_sha,
        state=PullRequestState(change.state.value),
        repository_enabled=True,
        ai_review_labeled=None,
        ai_review_labeled_at=None,
        head_first_seen_at=None,
        wait_for_ci=CiWaitMode(repository.wait_for_ci.value),
        identity=identity,
    )


class SqlAlchemyBitbucketEligibilityCandidateStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def get(self, code_change_id: UUID) -> EligibilityCandidate | None:
        async with self._sessions() as session:
            return await read_bitbucket_eligibility_candidate(session, code_change_id)
