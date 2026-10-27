"""Short-lived Bitbucket request credentials and fixed Cloud HTTP authority."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketCredentialUnavailable,
    BitbucketRequestCredential,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
    EncryptedCredential,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketInstallationIdentity,
    BitbucketRepositoryIdentity,
    BitbucketRepositoryWebhookSecret,
)
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository


class BitbucketCredentialReader(Protocol):
    async def credential_envelope_for(
        self, target: BoundBitbucketTarget
    ) -> EncryptedCredential: ...

    async def checkpoint(self, target: BoundBitbucketTarget) -> None: ...


class BitbucketRequestCredentials:
    def __init__(
        self, reader: BitbucketCredentialReader, cipher: BitbucketCredentialCipher
    ) -> None:
        self._reader = reader
        self._cipher = cipher

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        await self._reader.checkpoint(target)

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        encrypted = await self._reader.credential_envelope_for(target)
        try:
            token = self._cipher.decrypt(
                encrypted,
                context=CredentialContext(target.installation_id, target.workspace_id),
                purpose=CredentialPurpose.TOKEN,
            )
        except ValueError:
            raise BitbucketCredentialUnavailable() from None
        await self._reader.checkpoint(target)
        return BitbucketRequestCredential(target, token)


class SqlAlchemyBitbucketTargetReader:
    """Detached metadata reads; the caller owns any final-write transaction."""

    def __init__(self, session_factory: Callable[[], AsyncSession]) -> None:
        self._sessions = session_factory

    async def resolve_repository_target(self, repository_id: UUID) -> BoundBitbucketTarget:
        """Resolve a code-index job by portal repository id, then fence its active binding."""
        async with self._sessions() as session:
            origin = (
                await session.execute(
                    select(
                        BitbucketRepositoryIdentity.workspace_id,
                        BitbucketRepositoryIdentity.provider_installation_id,
                        BitbucketRepositoryIdentity.repository_uuid,
                    ).where(BitbucketRepositoryIdentity.repository_id == repository_id)
                )
            ).one_or_none()
        if origin is None:
            raise BitbucketTargetInactive()
        target = await self.resolve_target(
            workspace_id=origin.workspace_id,
            installation_id=origin.provider_installation_id,
            repository_uuid=origin.repository_uuid,
        )
        if target.repository_id != repository_id:
            raise BitbucketTargetInactive()
        return target

    async def resolve_target(
        self, *, workspace_id: UUID, installation_id: UUID, repository_uuid: UUID
    ) -> BoundBitbucketTarget:
        async with self._sessions() as session:
            row = await self._active_row(
                session,
                workspace_id=workspace_id,
                installation_id=installation_id,
                repository_uuid=repository_uuid,
            )
        if row is None:
            raise BitbucketTargetInactive()
        repository_id, generation, binding_generation, _, _ = row
        return BoundBitbucketTarget(
            installation_id,
            workspace_id,
            repository_id,
            repository_uuid,
            generation,
            binding_generation,
        )

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        async with self._sessions() as session:
            await self.checkpoint_in_session(session, target, lock=False)

    async def checkpoint_in_session(
        self, session: AsyncSession, target: BoundBitbucketTarget, *, lock: bool = True
    ) -> None:
        await self._checked_row(session, target, lock=lock)

    async def credential_envelope_for(self, target: BoundBitbucketTarget) -> EncryptedCredential:
        async with self._sessions() as session:
            _, _, _, key_id, ciphertext = await self._checked_row(session, target)
        return EncryptedCredential(key_id, ciphertext)

    async def _checked_row(
        self, session: AsyncSession, target: BoundBitbucketTarget, *, lock: bool = False
    ) -> tuple[UUID, int, int, str, bytes]:
        row = await self._active_row(
            session,
            workspace_id=target.workspace_id,
            installation_id=target.installation_id,
            repository_uuid=target.repository_uuid,
            lock=lock,
        )
        if row is None or row[0] != target.repository_id:
            raise BitbucketTargetInactive()
        if row[1] != target.credential_generation:
            raise BitbucketCredentialGenerationChanged()
        if row[2] != target.binding_generation:
            raise BitbucketBindingGenerationChanged()
        return row

    @staticmethod
    async def _active_row(
        session: AsyncSession,
        *,
        workspace_id: UUID,
        installation_id: UUID,
        repository_uuid: UUID,
        lock: bool = False,
    ) -> tuple[UUID, int, int, str, bytes] | None:
        statement = (
            select(
                Repository.id,
                BitbucketConnection.credential_generation,
                BitbucketRepositoryWebhookSecret.binding_generation,
                BitbucketConnection.token_key_id,
                BitbucketConnection.token_ciphertext,
            )
            .select_from(BitbucketRepositoryIdentity)
            .join(Repository, Repository.id == BitbucketRepositoryIdentity.repository_id)
            .join(
                BitbucketRepositoryWebhookSecret,
                BitbucketRepositoryWebhookSecret.repository_id == Repository.id,
            )
            .join(
                BitbucketInstallationIdentity,
                BitbucketInstallationIdentity.provider_installation_id
                == BitbucketRepositoryIdentity.provider_installation_id,
            )
            .join(
                ProviderInstallation,
                ProviderInstallation.id == BitbucketInstallationIdentity.provider_installation_id,
            )
            .join(
                BitbucketConnection,
                BitbucketConnection.provider_installation_id == ProviderInstallation.id,
            )
            .where(
                BitbucketRepositoryIdentity.workspace_id == workspace_id,
                BitbucketRepositoryIdentity.provider_installation_id == installation_id,
                BitbucketRepositoryIdentity.repository_uuid == repository_uuid,
                BitbucketInstallationIdentity.workspace_id == workspace_id,
                ProviderInstallation.workspace_id == workspace_id,
                ProviderInstallation.provider == "bitbucket",
                ProviderInstallation.deleted_at.is_(None),
                BitbucketConnection.workspace_id == workspace_id,
                BitbucketConnection.revoked_at.is_(None),
                BitbucketConnection.token_key_id.is_not(None),
                BitbucketConnection.token_ciphertext.is_not(None),
                Repository.provider_installation_id == installation_id,
                Repository.installation_available.is_(True),
            )
        )
        if lock:
            statement = statement.with_for_update(
                of=(ProviderInstallation, BitbucketConnection, Repository)
            )
        row = (await session.execute(statement)).one_or_none()
        if row is None or row[3] is None or row[4] is None:
            return None
        return row[0], row[1], row[2], row[3], row[4]


class BitbucketHttpClient:
    """Construct requests on the sole trusted API origin and refuse redirects."""

    def __init__(self, client: httpx.AsyncClient, credentials: BitbucketRequestCredentials) -> None:
        self._client = client
        self._credentials = credentials

    async def get(self, target: BoundBitbucketTarget, path: str) -> httpx.Response:
        if not isinstance(path, str) or not path.isascii() or "\\" in path:
            raise ValueError("Invalid Bitbucket API path")
        parsed = urlsplit(path)
        if (
            parsed.scheme
            or parsed.netloc
            or parsed.fragment
            or not parsed.path.startswith("/2.0/")
            or any(part in {".", ".."} for part in parsed.path.split("/"))
        ):
            raise ValueError("Invalid Bitbucket API path")
        credential = await self._credentials.credential_for(target)
        return await self._client.get(
            f"https://api.bitbucket.org{path}",
            headers={"Authorization": f"Bearer {credential.token}"},
            follow_redirects=False,
        )
