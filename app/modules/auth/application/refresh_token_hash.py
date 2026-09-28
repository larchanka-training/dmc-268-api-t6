"""Hash opaque refresh tokens for session storage and lookup."""

from __future__ import annotations

import hashlib


def hash_refresh_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()
