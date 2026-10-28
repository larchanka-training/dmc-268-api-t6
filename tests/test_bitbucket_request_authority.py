"""Bitbucket request credentials stay current and on the trusted Cloud origin."""

from __future__ import annotations

import asyncio
from uuid import uuid4

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketCredentialGenerationChanged,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
    EncryptedCredential,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketHttpClient,
    BitbucketRequestCredentials,
)


class FakeReader:
    def __init__(self, target: BoundBitbucketTarget, encrypted: EncryptedCredential) -> None:
        self.target = target
        self.encrypted = encrypted
        self.generation = target.credential_generation
        self.rotate_after_read = False
        self.checkpoints = 0

    async def credential_envelope_for(self, target: BoundBitbucketTarget) -> EncryptedCredential:
        assert target == self.target
        if self.rotate_after_read:
            self.generation += 1
        return self.encrypted

    async def checkpoint(self, target: BoundBitbucketTarget) -> None:
        self.checkpoints += 1
        if target.credential_generation != self.generation:
            raise BitbucketCredentialGenerationChanged()


def _target() -> BoundBitbucketTarget:
    return BoundBitbucketTarget(uuid4(), uuid4(), uuid4(), uuid4(), 1)


def test_credential_authority_rechecks_generation_after_decryption() -> None:
    target = _target()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    encrypted = cipher.encrypt(
        "test-token",
        context=CredentialContext(target.installation_id, target.workspace_id),
        purpose=CredentialPurpose.TOKEN,
    )
    reader = FakeReader(target, encrypted)
    authority = BitbucketRequestCredentials(reader, cipher)

    credential = asyncio.run(authority.credential_for(target))
    assert credential.token == "test-token"
    assert "test-token" not in repr(credential)
    assert reader.checkpoints == 1
    reader.rotate_after_read = True
    with pytest.raises(BitbucketCredentialGenerationChanged):
        asyncio.run(authority.credential_for(target))


def test_http_client_never_sends_token_to_another_origin_or_redirect() -> None:
    target = _target()
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    reader = FakeReader(
        target,
        cipher.encrypt(
            "test-token",
            context=CredentialContext(target.installation_id, target.workspace_id),
            purpose=CredentialPurpose.TOKEN,
        ),
    )
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(302, headers={"location": "https://attacker.example/steal"})

    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), follow_redirects=True
        ) as http:
            client = BitbucketHttpClient(http, BitbucketRequestCredentials(reader, cipher))
            for invalid in ("https://attacker.example/steal", "//attacker.example/steal", "/other"):
                with pytest.raises(ValueError):
                    await client.get(target, invalid)
            response = await client.get(target, "/2.0/repositories/workspace/repo")
            assert response.status_code == 302

    asyncio.run(scenario())
    assert seen == [
        ("https://api.bitbucket.org/2.0/repositories/workspace/repo", "Bearer test-token")
    ]
