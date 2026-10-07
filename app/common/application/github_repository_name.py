"""The shape of a GitHub repository name.

Shared by the webhook DTOs, the review worker's pull request DTO (``github_vcs``), the
request path builder, the ``repositories.full_name`` column and the ``repo`` filter of
``GET /api/runs``.
"""

from __future__ import annotations

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
