"""Shared urllib opener type and default HTTPS transport for review_bot clients."""

from __future__ import annotations

import urllib.request
from collections.abc import Callable
from typing import Any

Opener = Callable[[urllib.request.Request, float], Any]


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 (fixed https host)
