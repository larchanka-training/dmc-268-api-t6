"""Public credential boundary: installation identity and secrets stay isolated."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import cast
from uuid import UUID

import pytest

from app.modules.integrations.gitlab.infrastructure import credential_cipher as credential_codec
from app.modules.integrations.gitlab.infrastructure.credential_cipher import (
    CredentialCipher,
    CredentialContext,
    EncryptedCredential,
)


def test_pat_round_trip_is_bound_to_installation_context() -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    context = CredentialContext(
        installation_id=UUID("11111111-1111-1111-1111-111111111111"),
        workspace_id=UUID("22222222-2222-2222-2222-222222222222"),
        instance_origin="https://gitlab.example.test",
    )

    encrypted = cipher.encrypt("synthetic-pat-for-test", context=context)

    assert encrypted.key_id == "primary"
    assert b"synthetic-pat-for-test" not in encrypted.token_ciphertext
    assert cipher.decrypt(encrypted, context=context) == "synthetic-pat-for-test"


# Literal identities make the AAD contract independent of codec internals.
_CONTEXT = CredentialContext(
    installation_id=UUID("11111111-1111-1111-1111-111111111111"),
    workspace_id=UUID("22222222-2222-2222-2222-222222222222"),
    instance_origin="https://gitlab.example.test",
)


@pytest.mark.parametrize(
    "context",
    [
        replace(_CONTEXT, installation_id=UUID("33333333-3333-3333-3333-333333333333")),
        replace(_CONTEXT, workspace_id=UUID("33333333-3333-3333-3333-333333333333")),
        replace(_CONTEXT, instance_origin="https://other.example.test"),
    ],
)
def test_wrong_identity_fails_with_generic_error(context: CredentialContext) -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    encrypted = cipher.encrypt("synthetic-pat-for-test", context=_CONTEXT)

    with pytest.raises(ValueError, match="^GitLab credential operation failed$") as error:
        cipher.decrypt(encrypted, context=context)
    assert error.value.__cause__ is None
    assert error.value.__suppress_context__


@pytest.mark.parametrize("offset", [0, 4, 15, 16, -1])
def test_envelope_version_nonce_ciphertext_and_tag_are_authenticated(offset: int) -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    encrypted = cipher.encrypt("synthetic-pat-for-test", context=_CONTEXT)
    changed = bytearray(encrypted.token_ciphertext)
    changed[offset] ^= 1

    with pytest.raises(ValueError, match="^GitLab credential operation failed$"):
        cipher.decrypt(replace(encrypted, token_ciphertext=bytes(changed)), context=_CONTEXT)


def test_wrong_key_and_key_label_fail_without_exposing_values() -> None:
    cipher = CredentialCipher(
        active_key_id="primary", keys={"primary": b"a" * 32, "alias": b"a" * 32}
    )
    encrypted = cipher.encrypt("synthetic-pat-for-test", context=_CONTEXT)
    wrong_key = CredentialCipher(active_key_id="primary", keys={"primary": b"b" * 32})
    for codec, credential in [
        (wrong_key, encrypted),
        (cipher, replace(encrypted, key_id="alias")),
        (cipher, replace(encrypted, key_id="unknown")),
    ]:
        with pytest.raises(ValueError, match="^GitLab credential operation failed$"):
            codec.decrypt(credential, context=_CONTEXT)


@pytest.mark.parametrize("payload", [b"", b"GLC1", b"GLC1" + b"x" * 28, b"x" * 5000])
def test_malformed_and_oversized_envelopes_fail_closed(payload: bytes) -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    with pytest.raises(ValueError, match="^GitLab credential operation failed$"):
        cipher.decrypt(EncryptedCredential("primary", payload), context=_CONTEXT)


@pytest.mark.parametrize(
    "origin",
    [
        "https://GitLab.example.test",
        "https://gitlab.example.test/",
        "https://gitlab.example.test:443",
        "https://gitlab.example.test:0443",
        "https://gitlab.example.test:99999",
        "https://user:password@gitlab.example.test",
        "http://gitlab.example.test",
        "https://gitlab.example.test/path",
        "https://gitlab..example.test",
        "https://gitlab.example.test?token=invalid-value",
        "https://gitlab.example.test#fragment",
        "https://gitlab.example.test\n",
    ],
)
def test_context_rejects_noncanonical_origins_without_echoing_values(origin: str) -> None:
    with pytest.raises(ValueError, match="^Invalid GitLab credential context$"):
        replace(_CONTEXT, instance_origin=origin)


@pytest.mark.parametrize(
    "active, keys",
    [
        ("missing", {"primary": b"a" * 32}),
        ("primary", {"primary": b"too-short"}),
        ("primary", {"primary": b"a" * 16}),
        ("primary", {}),
        ("invalid key id", {"invalid key id": b"a" * 32}),
        ("x" * 65, {"x" * 65: b"a" * 32}),
    ],
)
def test_invalid_key_configuration_has_a_safe_error(active: str, keys: dict[str, bytes]) -> None:
    with pytest.raises(ValueError, match="^Invalid GitLab credential cipher configuration$"):
        CredentialCipher(active_key_id=active, keys=keys)


@pytest.mark.parametrize("token", ["", "x" * 4097, "contains\nnewline", "non-ascii-å"])
def test_invalid_plaintext_is_rejected_without_echoing_it(token: str) -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    with pytest.raises(ValueError, match="^GitLab credential operation failed$"):
        cipher.encrypt(token, context=_CONTEXT)


def test_safe_representations_exclude_key_material_plaintext_and_envelope() -> None:
    cipher = CredentialCipher(
        active_key_id="primary", keys={"primary": b"sensitive-key" * 2 + b"x" * 6}
    )
    encrypted = cipher.encrypt("synthetic-pat-for-test", context=_CONTEXT)
    assert "AES-256-GCM" in repr(cipher)
    assert "payload_bytes=" in repr(encrypted)
    for value in ["sensitive-key", "synthetic-pat-for-test", repr(encrypted.token_ciphertext)]:
        assert value not in repr(cipher)
        assert value not in repr(encrypted)


def test_each_encrypt_uses_fresh_twelve_byte_nonce_and_caller_cannot_supply_one() -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    envelopes = [cipher.encrypt("same-pat", context=_CONTEXT).token_ciphertext for _ in range(100)]
    assert all(envelope.startswith(b"GLC1") and len(envelope) == 40 for envelope in envelopes)
    assert len({envelope[4:16] for envelope in envelopes}) == 100
    assert all(
        cipher.decrypt(EncryptedCredential("primary", value), context=_CONTEXT) == "same-pat"
        for value in envelopes
    )


def test_key_rotation_reads_old_key_but_writes_only_active_key_and_copies_configuration() -> None:
    old = CredentialCipher(active_key_id="old", keys={"old": b"a" * 32})
    previous = old.encrypt("old-pat", context=_CONTEXT)
    keys = {"old": b"a" * 32, "new": b"b" * 32}
    rotated = CredentialCipher(active_key_id="new", keys=keys)
    keys.clear()
    assert rotated.decrypt(previous, context=_CONTEXT) == "old-pat"
    current = rotated.encrypt("new-pat", context=_CONTEXT)
    assert current.key_id == "new"
    assert rotated.decrypt(current, context=_CONTEXT) == "new-pat"
    with pytest.raises(ValueError, match="^GitLab credential operation failed$"):
        old.decrypt(current, context=_CONTEXT)


_WEBHOOK_SECRET = "abcdefghijklmnopqrstuvwxyz012345"
_WEBHOOK_DIGEST = "653bb1245e828fcda4fa53fcd5a3def5bd7654e651f54b4132b73d74e64435c4"


def test_operator_supplied_webhook_secret_matches_schema_digest_contract() -> None:
    assert credential_codec.digest_webhook_secret(_WEBHOOK_SECRET) == _WEBHOOK_DIGEST
    assert credential_codec.verify_webhook_secret(_WEBHOOK_SECRET, _WEBHOOK_DIGEST)
    assert not credential_codec.verify_webhook_secret("z" * 32, _WEBHOOK_DIGEST)


def test_generated_webhook_secrets_are_high_entropy_and_individually_verifiable() -> None:
    generated = [credential_codec.generate_webhook_secret() for _ in range(100)]
    assert len(set(generated)) == 100
    for secret in generated:
        assert len(secret) == 43
        assert secret.isascii()
        digest = credential_codec.digest_webhook_secret(secret)
        assert len(digest) == 64
        assert credential_codec.verify_webhook_secret(secret, digest)


@pytest.mark.parametrize("secret", ["", "short", "x" * 257, "x" * 31 + "\n", "å" * 32])
def test_invalid_webhook_secret_is_safe_and_verification_fails_closed(secret: str) -> None:
    with pytest.raises(ValueError, match="^Invalid GitLab webhook secret$"):
        credential_codec.digest_webhook_secret(secret)
    assert not credential_codec.verify_webhook_secret(secret, _WEBHOOK_DIGEST)


@pytest.mark.parametrize("digest", ["", "a" * 63, "a" * 65, "A" * 64, "z" * 64, "å" * 64])
def test_invalid_stored_digest_fails_closed(digest: str) -> None:
    assert not credential_codec.verify_webhook_secret(_WEBHOOK_SECRET, digest)


def test_secret_verification_compares_digests_in_constant_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import hmac

    compared: list[tuple[str, str]] = []
    original = hmac.compare_digest

    def compare(left: str, right: str) -> bool:
        compared.append((left, right))
        return original(left, right)

    monkeypatch.setattr(hmac, "compare_digest", compare)
    assert credential_codec.verify_webhook_secret(_WEBHOOK_SECRET, _WEBHOOK_DIGEST)
    assert not credential_codec.verify_webhook_secret("z" * 32, _WEBHOOK_DIGEST)
    assert len(compared) == 2
    assert compared[0] == (_WEBHOOK_DIGEST, _WEBHOOK_DIGEST)
    assert all(len(left) == 64 and right == _WEBHOOK_DIGEST for left, right in compared)


@pytest.mark.parametrize("keys", [None, b"not-a-keyring", ["not-a-keyring"]])
def test_malformed_keyring_shape_has_only_generic_configuration_error(keys: object) -> None:
    with pytest.raises(ValueError, match="^Invalid GitLab credential cipher configuration$"):
        CredentialCipher(active_key_id="primary", keys=cast(Mapping[str, bytes], keys))


def test_ipv6_scope_is_not_a_canonical_schema_origin() -> None:
    with pytest.raises(ValueError, match="^Invalid GitLab credential context$"):
        replace(_CONTEXT, instance_origin="https://[fe80::1%interface]")


@pytest.mark.parametrize("origin", ["https://gitlab.example.test:8443", "https://[2001:db8::1]"])
def test_canonical_nondefault_port_and_ipv6_origins_round_trip(origin: str) -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    context = replace(_CONTEXT, instance_origin=origin)
    encrypted = cipher.encrypt("synthetic-pat", context=context)
    assert cipher.decrypt(encrypted, context=context) == "synthetic-pat"


def test_callers_cannot_reuse_an_explicit_nonce() -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    encrypt = cast(Callable[..., EncryptedCredential], cipher.encrypt)
    with pytest.raises(TypeError, match="unexpected keyword argument 'nonce'"):
        encrypt("synthetic-pat", context=_CONTEXT, nonce=b"a" * 12)


def test_maximum_pat_and_webhook_lengths_are_supported() -> None:
    cipher = CredentialCipher(active_key_id="primary", keys={"primary": b"a" * 32})
    encrypted = cipher.encrypt("x" * 4096, context=_CONTEXT)
    assert len(encrypted.token_ciphertext) == 4128
    assert cipher.decrypt(encrypted, context=_CONTEXT) == "x" * 4096
    secret = "x" * 256
    assert credential_codec.verify_webhook_secret(
        secret, credential_codec.digest_webhook_secret(secret)
    )
    assert not credential_codec.verify_webhook_secret(None, _WEBHOOK_DIGEST)
