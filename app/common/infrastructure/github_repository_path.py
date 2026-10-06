"""Validated segments of GitHub request paths.

A repository name or a commit SHA that reaches a request path comes from a webhook or a
database row, so it is untrusted: httpx collapses literal ``.`` and ``..`` path segments
before the request is sent (``/repos/o/../../pulls/1`` is sent as ``/pulls/1``), which
would redirect a request that carries the installation token. Every adapter builds its
paths here, and an error never carries the rejected value.
"""

from __future__ import annotations

import re
from urllib.parse import quote

from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    REPOSITORY_FULL_NAME_PATTERN,
)

_REPOSITORY_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class InvalidGitHubPathSegment(ValueError):
    """A name or SHA cannot be placed in a GitHub request path; the text omits the value."""


def repository_path_from_full_name(full_name: str) -> str:
    """Return ``/repos/{owner}/{repo}``; a name of another shape raises before any request."""
    # The length check comes first: the pattern backtracks quadratically on a crafted long
    # name such as "o/a.a.a...!".
    if (
        len(full_name) > REPOSITORY_FULL_NAME_MAX_LENGTH
        or _REPOSITORY_FULL_NAME.fullmatch(full_name) is None
    ):
        raise InvalidGitHubPathSegment("GitHub repository must be owner/repo")
    return f"/repos/{full_name}"


def ref_segment(name: str) -> str:
    """Return a branch or tag name as one percent-encoded path segment (``/`` becomes ``%2F``)."""
    if name in {"", ".", ".."}:
        raise InvalidGitHubPathSegment("GitHub ref name is not usable as a path segment")
    return quote(name, safe="")


def commit_sha_segment(sha: str) -> str:
    """Return a full 40 or 64 digit hexadecimal commit SHA unchanged."""
    if _COMMIT_SHA.fullmatch(sha) is None:
        raise InvalidGitHubPathSegment("GitHub commit SHA must be 40 or 64 hexadecimal characters")
    return sha
