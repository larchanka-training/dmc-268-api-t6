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

# ``owner/repo`` as GitHub allows it: one slash; an owner without dots (``_`` is
# valid for Enterprise Managed User logins); a repo that is never only dots
# (``.``/``..`` would walk the request path) but may start with one (``.github``).
# pydantic compiles patterns with the Rust regex engine: no lookaround, and ``$``
# matches only at the very end of the text, so a trailing newline is rejected.
REPOSITORY_FULL_NAME_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9._-]*[A-Za-z0-9_-][A-Za-z0-9._-]*$"
)
_REPOSITORY_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)
# The database column and the webhook DTOs allow 512 characters. The check comes first:
# the pattern backtracks quadratically on a crafted long name such as "o/a.a.a...!".
_MAX_FULL_NAME_LENGTH = 512
_COMMIT_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")


class InvalidGitHubPathSegment(ValueError):
    """A name or SHA cannot be placed in a GitHub request path; the text omits the value."""


def repository_path_from_full_name(full_name: str) -> str:
    """Return ``/repos/{owner}/{repo}``; a name of another shape raises before any request."""
    if len(full_name) > _MAX_FULL_NAME_LENGTH or _REPOSITORY_FULL_NAME.fullmatch(full_name) is None:
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
