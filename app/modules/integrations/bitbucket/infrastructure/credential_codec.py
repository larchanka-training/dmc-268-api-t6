"""Application port adapter for the Bitbucket AES-GCM credential cipher."""

from __future__ import annotations

from uuid import UUID

from app.modules.integrations.bitbucket.application.manage_connection import (
    BitbucketManagementError,
    InstallationSnapshot,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
    EncryptedCredential,
)


class BitbucketCredentialCodecAdapter:
    def __init__(self, cipher: BitbucketCredentialCipher) -> None:
        self._cipher = cipher

    def encrypt_token(
        self, token: str, *, installation_id: UUID, workspace_id: UUID
    ) -> EncryptedCredential:
        try:
            return self._cipher.encrypt(
                token,
                context=CredentialContext(installation_id, workspace_id),
                purpose=CredentialPurpose.TOKEN,
            )
        except ValueError:
            raise BitbucketManagementError("invalid_credential") from None

    def decrypt_token(self, snapshot: InstallationSnapshot, *, workspace_id: UUID) -> str:
        try:
            return self._cipher.decrypt(
                EncryptedCredential(snapshot.token_key_id, snapshot.token_ciphertext),
                context=CredentialContext(snapshot.installation_id, workspace_id),
                purpose=CredentialPurpose.TOKEN,
            )
        except ValueError:
            raise BitbucketManagementError("credential_unavailable") from None

    def encrypt_webhook_secret(
        self,
        secret: str,
        *,
        installation_id: UUID,
        workspace_id: UUID,
        repository_id: UUID,
    ) -> EncryptedCredential:
        try:
            return self._cipher.encrypt(
                secret,
                context=CredentialContext(installation_id, workspace_id, repository_id),
                purpose=CredentialPurpose.WEBHOOK_SECRET,
            )
        except ValueError:
            raise BitbucketManagementError("credential_unavailable") from None
