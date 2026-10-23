"""GitHub webhook signatures."""

from __future__ import annotations

import hashlib
import hmac


def expected_signature(secret: bytes, body: bytes) -> str:
    return "sha256=" + hmac.new(secret, body, hashlib.sha256).hexdigest()
