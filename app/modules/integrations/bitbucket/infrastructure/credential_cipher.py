"""Authenticated encryption of Bitbucket credentials bound to a portal owner."""

from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.modules.integrations.bitbucket.application.credential_input import valid_bitbucket_token


class CredentialPurpose(StrEnum):
    TOKEN = "api-token"
    WEBHOOK_SECRET = "webhook-secret"


def _unique_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for label, value in pairs:
        if label in values:
            raise ValueError
        values[label] = value
    return values


def optional_bitbucket_cipher_from_environment(
    env: Mapping[str, str],
) -> BitbucketCredentialCipher | None:
    raw = env.get("BITBUCKET_CREDENTIAL_KEYS")
    active = env.get("BITBUCKET_ACTIVE_KEY_ID")
    if raw is None and active is None:
        return None
    try:
        if not raw or not active or len(raw) > 16384:
            raise ValueError
        parsed = json.loads(raw, object_pairs_hook=_unique_json_keys)
        if not isinstance(parsed, dict) or not all(
            isinstance(label, str) and isinstance(value, str) for label, value in parsed.items()
        ):
            raise ValueError
        keys: dict[str, bytes] = {}
        for label, value in parsed.items():
            key = base64.b64decode(value, validate=True)
            if base64.b64encode(key).decode("ascii") != value:
                raise ValueError
            keys[label] = key
        return BitbucketCredentialCipher(active_key_id=active, keys=keys)
    except (ValueError, TypeError, binascii.Error):
        raise ValueError("invalid_bitbucket_credential_keys") from None


@dataclass(frozen=True)
class CredentialContext:
    installation_id: UUID
    workspace_id: UUID
    repository_id: UUID | None = None

    def __post_init__(self) -> None:
        if (
            type(self.installation_id) is not UUID
            or type(self.workspace_id) is not UUID
            or (self.repository_id is not None and type(self.repository_id) is not UUID)
        ):
            raise ValueError("Invalid Bitbucket credential context")


@dataclass(frozen=True, repr=False)
class EncryptedCredential:
    key_id: str = field(repr=False)
    ciphertext: bytes = field(repr=False)

    def __repr__(self) -> str:
        size = len(self.ciphertext) if isinstance(self.ciphertext, bytes) else 0
        return f"EncryptedCredential(payload_bytes={size}, key_id=<redacted>)"


class BitbucketCredentialCipher:
    def __init__(self, *, active_key_id: str, keys: Mapping[str, bytes]) -> None:
        if (
            not isinstance(keys, Mapping)
            or not _valid_key_id(active_key_id)
            or not 1 <= len(keys) <= 32
            or active_key_id not in keys
            or any(
                not _valid_key_id(label) or not isinstance(key, bytes) or len(key) != 32
                for label, key in keys.items()
            )
        ):
            raise ValueError("Invalid Bitbucket credential cipher configuration")
        self._active_key_id = active_key_id
        self._keys = dict(keys)

    def __repr__(self) -> str:
        return f"BitbucketCredentialCipher(algorithm='AES-256-GCM', key_count={len(self._keys)})"

    def encrypt(
        self, value: str, *, context: CredentialContext, purpose: CredentialPurpose
    ) -> EncryptedCredential:
        if not _valid_value(value, purpose) or not isinstance(context, CredentialContext):
            raise ValueError("Bitbucket credential operation failed")
        _validate_purpose_context(context, purpose)
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce, value.encode("ascii"), self._aad(context, purpose, self._active_key_id)
        )
        return EncryptedCredential(self._active_key_id, b"BBC1" + nonce + ciphertext)

    def decrypt(
        self,
        credential: EncryptedCredential,
        *,
        context: CredentialContext,
        purpose: CredentialPurpose,
    ) -> str:
        if not isinstance(credential, EncryptedCredential) or not isinstance(
            context, CredentialContext
        ):
            raise ValueError("Bitbucket credential operation failed")
        _validate_purpose_context(context, purpose)
        envelope = credential.ciphertext
        try:
            if (
                not _valid_key_id(credential.key_id)
                or not isinstance(envelope, bytes)
                or not 33 <= len(envelope) <= 4128
                or not envelope.startswith(b"BBC1")
            ):
                raise ValueError
            value = (
                AESGCM(self._keys[credential.key_id])
                .decrypt(
                    envelope[4:16], envelope[16:], self._aad(context, purpose, credential.key_id)
                )
                .decode("ascii")
            )
            if not _valid_value(value, purpose):
                raise ValueError
            return value
        except (InvalidTag, KeyError, ValueError):
            raise ValueError("Bitbucket credential operation failed") from None

    @staticmethod
    def _aad(context: CredentialContext, purpose: CredentialPurpose, key_id: str) -> bytes:
        if not isinstance(purpose, CredentialPurpose):
            raise ValueError("Bitbucket credential operation failed")
        return json.dumps(
            [
                "bitbucket-credential",
                1,
                purpose.value,
                str(context.installation_id),
                str(context.workspace_id),
                str(context.repository_id) if context.repository_id is not None else "",
                key_id,
            ],
            separators=(",", ":"),
        ).encode("ascii")


def _valid_key_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is not None
    )


def _validate_purpose_context(context: CredentialContext, purpose: CredentialPurpose) -> None:
    if (purpose is CredentialPurpose.TOKEN and context.repository_id is not None) or (
        purpose is CredentialPurpose.WEBHOOK_SECRET and context.repository_id is None
    ):
        raise ValueError("Bitbucket credential operation failed")


def _valid_value(value: str, purpose: CredentialPurpose) -> bool:
    if not isinstance(value, str) or not isinstance(purpose, CredentialPurpose):
        return False
    if purpose is CredentialPurpose.TOKEN:
        return valid_bitbucket_token(value)
    size = len(value)
    if purpose is CredentialPurpose.WEBHOOK_SECRET and not 32 <= size <= 256:
        return False
    return all(33 <= ord(character) <= 126 for character in value)
