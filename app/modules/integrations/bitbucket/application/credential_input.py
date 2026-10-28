"""Bitbucket credential and repository identifiers accepted before provider I/O."""

from __future__ import annotations

import re

_SLUG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def valid_bitbucket_token(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 4096
        and all(33 <= ord(character) <= 126 for character in value)
    )


def canonical_bitbucket_slug(value: object) -> str:
    if not isinstance(value, str) or _SLUG.fullmatch(value) is None or value in {".", ".."}:
        raise ValueError("invalid Bitbucket slug")
    return value.lower()
