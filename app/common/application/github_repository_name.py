"""The shape of a GitHub repository name.

Shared by the webhook DTOs, the review worker's pull request DTO (``github_vcs``), the
request path builder, the ``repositories.full_name`` column and the ``repo`` filter of
``GET /api/runs``. Code outside pydantic checks a name with
``is_github_repository_full_name``: the path builder, the public badge and the visibility
re-read (#454).
"""

from __future__ import annotations

import re

# ``owner/repo`` as GitHub allows it: one slash; an owner without dots (``_`` is
# valid for Enterprise Managed User logins); a repo that is never only dots
# (``.``/``..`` would walk the request path) but may start with one (``.github``).
# pydantic compiles patterns with the Rust regex engine: no lookaround, and ``$``
# matches only at the very end of the text, so a trailing newline is rejected.
REPOSITORY_FULL_NAME_PATTERN = (
    r"^[A-Za-z0-9][A-Za-z0-9_-]*/[A-Za-z0-9._-]*[A-Za-z0-9_-][A-Za-z0-9._-]*$"
)
# The ``repositories.full_name`` column, the webhook DTOs, the review worker's DTO, the
# path builder and the runs list's ``repo`` filter allow 512 characters.
REPOSITORY_FULL_NAME_MAX_LENGTH = 512

_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)


def is_github_repository_full_name(value: str) -> bool:
    """Whether GitHub could have this ``owner/repo``."""
    # The length check comes first: the pattern backtracks quadratically on a crafted long
    # name such as "o/a.a.a...!".
    return len(value) <= REPOSITORY_FULL_NAME_MAX_LENGTH and _FULL_NAME.fullmatch(value) is not None
