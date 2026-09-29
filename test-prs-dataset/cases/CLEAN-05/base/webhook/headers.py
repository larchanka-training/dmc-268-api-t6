from __future__ import annotations

from collections.abc import Mapping


def canonicalize_headers(headers: Mapping[str, str]) -> dict[str, str]:
    canonical: dict[str, str] = {}
    for name, value in headers.items():
        canonical[name.lower()] = value.strip()
    return canonical


def delivery_metadata(headers: Mapping[str, str]) -> tuple[str | None, str | None]:
    canonical = canonicalize_headers(headers)
    return canonical.get("x-github-delivery"), canonical.get("x-github-event")
