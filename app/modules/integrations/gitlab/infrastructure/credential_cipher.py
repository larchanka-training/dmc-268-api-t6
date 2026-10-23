"""Authenticated, installation-bound encryption for stored GitLab credentials.

The binary GLC1 envelope is version magic, a 12-byte random nonce, then AES-GCM
ciphertext with its 16-byte tag. AAD is a deterministic JSON array containing
purpose/version, installation UUID, workspace UUID, exact canonical origin and key ID.
Only this codec allocates nonces; callers cannot override them. Keep old keys in the
keyring until their stored credentials have been rotated to the active key.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


@dataclass(frozen=True)
class CredentialContext:
    installation_id: UUID
    workspace_id: UUID
    instance_origin: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.installation_id, UUID)
            or not isinstance(self.workspace_id, UUID)
            or not _canonical_origin(self.instance_origin)
        ):
            raise ValueError("Invalid GitLab credential context")


@dataclass(frozen=True, repr=False)
class EncryptedCredential:
    key_id: str = field(repr=False)
    token_ciphertext: bytes = field(repr=False)

    def __repr__(self) -> str:
        size = len(self.token_ciphertext) if isinstance(self.token_ciphertext, bytes) else 0
        return f"EncryptedCredential(payload_bytes={size}, key_id=<redacted>)"


class CredentialCipherError(ValueError):
    """Safe error for invalid or unauthentic stored credentials."""

    def __init__(self) -> None:
        super().__init__("GitLab credential operation failed")


class CredentialCipher:
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
            raise ValueError("Invalid GitLab credential cipher configuration")
        self._active_key_id = active_key_id
        self._keys = dict(keys)

    def __repr__(self) -> str:
        return f"CredentialCipher(algorithm='AES-256-GCM', key_count={len(self._keys)})"

    def encrypt(self, token: str, *, context: CredentialContext) -> EncryptedCredential:
        if not _valid_token(token) or not isinstance(context, CredentialContext):
            raise CredentialCipherError() from None
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self._keys[self._active_key_id]).encrypt(
            nonce, token.encode("ascii"), self._aad(context, self._active_key_id)
        )
        return EncryptedCredential(self._active_key_id, b"GLC1" + nonce + ciphertext)

    def decrypt(self, credential: EncryptedCredential, *, context: CredentialContext) -> str:
        if not isinstance(credential, EncryptedCredential) or not isinstance(
            context, CredentialContext
        ):
            raise CredentialCipherError() from None
        envelope = credential.token_ciphertext
        try:
            if (
                not _valid_key_id(credential.key_id)
                or not isinstance(envelope, bytes)
                or not 33 <= len(envelope) <= 4128
                or not envelope.startswith(b"GLC1")
            ):
                raise CredentialCipherError()
            token = (
                AESGCM(self._keys[credential.key_id])
                .decrypt(envelope[4:16], envelope[16:], self._aad(context, credential.key_id))
                .decode("ascii")
            )
            if not _valid_token(token):
                raise CredentialCipherError()
            return token
        except (InvalidTag, KeyError, ValueError):
            raise CredentialCipherError() from None

    @staticmethod
    def _aad(context: CredentialContext, key_id: str) -> bytes:
        return json.dumps(
            [
                "gitlab-credential",
                1,
                str(context.installation_id),
                str(context.workspace_id),
                context.instance_origin,
                key_id,
            ],
            separators=(",", ":"),
        ).encode("ascii")


def _valid_key_id(value: str) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", value) is not None
    )


def _valid_token(value: str) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and all(33 <= ord(character) <= 126 for character in value)
    )


def _canonical_origin(value: str) -> bool:
    # Validate identity, never normalize it. Allowlisting/DNS/SSRF policy belongs
    # to connection management; this boundary accepts only a canonical HTTPS origin.
    if not isinstance(value, str) or not 1 <= len(value) <= 300 or not value.isascii():
        return False
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.username or parsed.password:
            return False
        if "%" in host:
            return False
        if ":" in host:
            canonical_host = f"[{ipaddress.IPv6Address(host).compressed}]"
        else:
            if len(host) > 253 or any(
                re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None
                for label in host.split(".")
            ):
                return False
            canonical_host = host
        port = parsed.port
        if port is not None and (port == 443 or not 1 <= port <= 65535):
            return False
        suffix = f":{port}" if port is not None else ""
        return value == f"https://{canonical_host}{suffix}"
    except ValueError:
        return False


def generate_webhook_secret() -> str:
    """Generate 256 bits of entropy; return once to the trusted setup caller."""
    return secrets.token_urlsafe(32)


def digest_webhook_secret(secret: str) -> str:
    """Digest a generated or operator-supplied 32–256 character ASCII secret.

    Keep only the lowercase 64-character hex digest in gitlab_connections.
    Operator-supplied secrets must have equivalent entropy to generated secrets.
    """
    if not _valid_webhook_secret(secret):
        raise ValueError("Invalid GitLab webhook secret") from None
    return hashlib.sha256(secret.encode("ascii")).hexdigest()


def verify_webhook_secret(secret: str | None, expected_digest: str) -> bool:
    """Fail closed on malformed inputs and compare fixed-size digests with hmac."""
    if (
        not isinstance(secret, str)
        or not _valid_webhook_secret(secret)
        or not isinstance(expected_digest, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_digest) is None
    ):
        return False
    return hmac.compare_digest(digest_webhook_secret(secret), expected_digest)


def _valid_webhook_secret(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and 32 <= len(value) <= 256
        and all(33 <= ord(character) <= 126 for character in value)
    )
