"""Bitbucket credential encryption stays bound to its portal owner (#333)."""

from __future__ import annotations

from uuid import uuid4

import pytest

from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)


def test_token_and_webhook_secret_are_bound_to_installation_and_repository() -> None:
    cipher = BitbucketCredentialCipher(active_key_id="first", keys={"first": b"a" * 32})
    context = CredentialContext(uuid4(), uuid4())
    hook_context = CredentialContext(context.installation_id, context.workspace_id, uuid4())
    token = "test-api-token-123"
    webhook_secret = "s" * 40

    encrypted_token = cipher.encrypt(token, context=context, purpose=CredentialPurpose.TOKEN)
    encrypted_secret = cipher.encrypt(
        webhook_secret, context=hook_context, purpose=CredentialPurpose.WEBHOOK_SECRET
    )

    assert token.encode() not in encrypted_token.ciphertext
    assert webhook_secret.encode() not in encrypted_secret.ciphertext
    assert token not in repr(encrypted_token)
    assert webhook_secret not in repr(encrypted_secret)
    assert (
        cipher.decrypt(encrypted_token, context=context, purpose=CredentialPurpose.TOKEN) == token
    )
    assert (
        cipher.decrypt(
            encrypted_secret, context=hook_context, purpose=CredentialPurpose.WEBHOOK_SECRET
        )
        == webhook_secret
    )
    with pytest.raises(ValueError):
        cipher.decrypt(
            encrypted_secret,
            context=CredentialContext(context.installation_id, context.workspace_id, uuid4()),
            purpose=CredentialPurpose.WEBHOOK_SECRET,
        )
    with pytest.raises(ValueError):
        cipher.decrypt(
            encrypted_token,
            context=CredentialContext(context.installation_id, uuid4()),
            purpose=CredentialPurpose.TOKEN,
        )
    with pytest.raises(ValueError):
        cipher.decrypt(
            encrypted_token,
            context=CredentialContext(uuid4(), context.workspace_id),
            purpose=CredentialPurpose.TOKEN,
        )
    with pytest.raises(ValueError):
        cipher.decrypt(encrypted_token, context=context, purpose=CredentialPurpose.WEBHOOK_SECRET)


def test_rotated_key_reads_old_ciphertext_but_encrypts_with_new_key() -> None:
    context = CredentialContext(uuid4(), uuid4())
    first = BitbucketCredentialCipher(active_key_id="first", keys={"first": b"a" * 32})
    old = first.encrypt("first-token", context=context, purpose=CredentialPurpose.TOKEN)
    rotated = BitbucketCredentialCipher(
        active_key_id="second", keys={"first": b"a" * 32, "second": b"b" * 32}
    )

    assert rotated.decrypt(old, context=context, purpose=CredentialPurpose.TOKEN) == "first-token"
    new = rotated.encrypt("second-token", context=context, purpose=CredentialPurpose.TOKEN)
    assert new.key_id == "second"
    assert rotated.decrypt(new, context=context, purpose=CredentialPurpose.TOKEN) == "second-token"
    with pytest.raises(ValueError):
        first.decrypt(new, context=context, purpose=CredentialPurpose.TOKEN)
