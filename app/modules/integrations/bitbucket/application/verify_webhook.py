"""Verify Bitbucket Cloud HMAC over the original request body."""

from __future__ import annotations

import hashlib
import hmac
import re

_SHA256_SIGNATURE = re.compile(r"sha256=[0-9a-f]{64}\Z")


def verify_bitbucket_signature(body: bytes, signature: str | None, secret: str) -> bool:
    if (
        not isinstance(body, bytes)
        or not isinstance(signature, str)
        or _SHA256_SIGNATURE.fullmatch(signature) is None
        or not isinstance(secret, str)
    ):
        return False
    expected = "sha256=" + hmac.new(secret.encode("ascii"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)
