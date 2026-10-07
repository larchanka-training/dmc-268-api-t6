"""The plain-token rule for GitHub-supplied values that go into log lines."""

from __future__ import annotations

import re

# GitHub's event names, actions, statuses and conclusions are lowercase words joined by ``_``.
# They go into log lines, so anything else (free text, line breaks) is not a token; each caller
# logs its own placeholder instead.
_LOG_TOKEN = re.compile(r"[a-z_]{1,40}")


def log_token(value: object) -> str | None:
    """``value`` when it is a plain token that is safe to log, otherwise None."""
    return value if isinstance(value, str) and _LOG_TOKEN.fullmatch(value) else None
