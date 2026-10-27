"""Shared full commit identity guards, independent of transport paths."""

from __future__ import annotations

import re

_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


def is_commit_sha(value: object) -> bool:
    """Accept the full SHA-1/SHA-256 hexadecimal identities supported by GitHub."""
    return isinstance(value, str) and _COMMIT_SHA.fullmatch(value) is not None


def is_canonical_sha1(value: object) -> bool:
    """Durable coverage pins retain the narrower lowercase SHA-1 contract."""
    return (
        is_commit_sha(value)
        and isinstance(value, str)
        and len(value) == 40
        and value == value.lower()
    )
