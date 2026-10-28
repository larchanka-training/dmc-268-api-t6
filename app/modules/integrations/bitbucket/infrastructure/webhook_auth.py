"""Read-only authenticated Bitbucket webhook origin lookup."""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.bitbucket.application.verify_webhook import verify_bitbucket_signature
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    AuthenticatedBitbucketOrigin,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
    EncryptedCredential,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository

_LOGGER = logging.getLogger(__name__)


class SqlAlchemyBitbucketWebhookAuthenticator:
    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], cipher: BitbucketCredentialCipher
    ) -> None:
        self._session_factory = session_factory
        self._cipher = cipher

    async def authenticate(
        self, *, installation_id: UUID, repository_id: UUID, body: bytes, signature: str | None
    ) -> AuthenticatedBitbucketOrigin | None:
        if signature is None or len(signature) != 71:
            return None
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ProviderInstallation.workspace_id,
                        BitbucketRepositoryIdentity.repository_uuid,
                        BitbucketRepositoryWebhookSecret.binding_generation,
                        BitbucketRepositoryWebhookSecret.generation,
                        BitbucketRepositoryWebhookSecret.secret_key_id,
                        BitbucketRepositoryWebhookSecret.secret_ciphertext,
                    )
                    .select_from(ProviderInstallation)
                    .join(
                        BitbucketConnection,
                        BitbucketConnection.provider_installation_id == ProviderInstallation.id,
                    )
                    .join(
                        Repository, Repository.provider_installation_id == ProviderInstallation.id
                    )
                    .join(
                        BitbucketRepositoryIdentity,
                        BitbucketRepositoryIdentity.repository_id == Repository.id,
                    )
                    .join(
                        BitbucketRepositoryWebhookSecret,
                        BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
                    )
                    .where(
                        ProviderInstallation.id == installation_id,
                        ProviderInstallation.provider == "bitbucket",
                        ProviderInstallation.deleted_at.is_(None),
                        BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
                        BitbucketConnection.revoked_at.is_(None),
                        BitbucketConnection.token_ciphertext.is_not(None),
                        Repository.id == repository_id,
                        Repository.enabled.is_(True),
                        Repository.installation_available.is_(True),
                        BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                        BitbucketRepositoryIdentity.workspace_id
                        == ProviderInstallation.workspace_id,
                        BitbucketRepositoryWebhookSecret.revoked_at.is_(None),
                    )
                )
            ).one_or_none()
        if row is None or row[4] is None or row[5] is None:
            return None
        (
            workspace_id,
            repository_uuid,
            binding_generation,
            webhook_generation,
            key_id,
            ciphertext,
        ) = row
        try:
            secret = self._cipher.decrypt(
                EncryptedCredential(key_id, ciphertext),
                context=CredentialContext(installation_id, workspace_id, repository_id),
                purpose=CredentialPurpose.WEBHOOK_SECRET,
            )
        except ValueError as error:
            _LOGGER.warning(
                "Bitbucket webhook secret unavailable for repository %s",
                repository_id,
                extra={"error_type": type(error).__name__},
            )
            return None
        if not verify_bitbucket_signature(body, signature, secret):
            return None
        return AuthenticatedBitbucketOrigin(
            installation_id,
            repository_id,
            workspace_id,
            repository_uuid,
            binding_generation,
            webhook_generation,
        )
