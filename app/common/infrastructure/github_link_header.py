"""Inspect untrusted GitHub pagination metadata without following its URLs."""

from __future__ import annotations

import httpx


def has_next_relation(link_header: str | None) -> bool:
    """Recognize a next relation among case-insensitive parameter/token lists."""
    response = httpx.Response(200, headers={"Link": link_header or ""})
    return any(
        "next" in value.casefold().split()
        for link in response.links.values()
        for parameter, value in link.items()
        if parameter.casefold() == "rel"
    )
