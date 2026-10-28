"""Validate abbreviated PR hashes against resolved Bitbucket commit objects."""

from __future__ import annotations

import re

_PR_HASH = re.compile(r"[0-9a-f]{12}(?:[0-9a-f]{28})?\Z")
_FULL_HASH = re.compile(r"[0-9a-f]{40}\Z")


def pr_commit_hash(value: object) -> str:
    if not isinstance(value, str) or _PR_HASH.fullmatch(value) is None:
        raise ValueError("invalid Bitbucket PR commit")
    return value


def resolved_commit_hash(value: object, prefix: str) -> str:
    if (
        not isinstance(value, str)
        or _FULL_HASH.fullmatch(value) is None
        or not value.startswith(prefix)
    ):
        raise ValueError("invalid Bitbucket resolved commit")
    return value
