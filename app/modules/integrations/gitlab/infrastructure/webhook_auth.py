"""Short, detached digest reads; webhook verification never loads a stored PAT."""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.authenticate_webhook import GitLabWebhookAuthRecord
from app.modules.integrations.gitlab.infrastructure.credential_cipher import verify_webhook_secret
from app.modules.integrations.gitlab.infrastructure.models import GitLabConnection
from app.modules.repositories.infrastructure.models import ProviderInstallation


class GitLabWebhookSecretVerifierAdapter:
    def verify(self, secret: str | None, expected_digest: str) -> bool:
        return verify_webhook_secret(secret, expected_digest)


class SqlAlchemyGitLabWebhookAuthReader:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def read(self, connection_id: UUID) -> GitLabWebhookAuthRecord | None:
        async with self._session_factory() as session:
            row = (
                await session.execute(
                    select(
                        GitLabConnection.provider_installation_id,
                        GitLabConnection.webhook_secret_digest,
                        GitLabConnection.intake_enabled,
                        and_(
                            ProviderInstallation.deleted_at.is_(None),
                            GitLabConnection.revoked_at.is_(None),
                            GitLabConnection.token_ciphertext.is_not(None),
                            func.octet_length(GitLabConnection.token_ciphertext) > 0,
                            GitLabConnection.credential_key_id.is_not(None),
                            func.length(func.trim(GitLabConnection.credential_key_id)) > 0,
                        ).label("active"),
                    )
                    .join(
                        ProviderInstallation,
                        (ProviderInstallation.id == GitLabConnection.provider_installation_id)
                        & (ProviderInstallation.workspace_id == GitLabConnection.workspace_id)
                        & (ProviderInstallation.provider == GitLabConnection.provider),
                    )
                    .where(
                        GitLabConnection.provider_installation_id == connection_id,
                        GitLabConnection.provider == "gitlab",
                        ProviderInstallation.provider == "gitlab",
                    )
                )
            ).one_or_none()
        if row is None:
            return None
        return GitLabWebhookAuthRecord(row[0], row[1], bool(row[3]), row[2])
