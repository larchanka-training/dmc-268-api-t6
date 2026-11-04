"""Shared Bitbucket Cloud provider response, UUID, and path validation."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from uuid import UUID

import httpx

from app.modules.integrations.bitbucket.application.provider_error import BitbucketProviderError


def bitbucket_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 1024
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError("invalid Bitbucket response")
    if (
        len(value.encode("utf-8")) > 1024
        or "\\" in value
        or value.startswith("/")
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise ValueError("invalid Bitbucket path")
    return value


def parse_bitbucket_uuid(value: object) -> UUID:
    if not isinstance(value, str) or re.fullmatch(r"\{[0-9a-fA-F-]{36}\}", value) is None:
        raise BitbucketProviderError("invalid_provider_response")
    try:
        return UUID(value[1:-1])
    except ValueError:
        raise BitbucketProviderError("invalid_provider_response") from None


def check_bitbucket_response(response: httpx.Response) -> None:
    if response.status_code == 200:
        return
    if response.status_code in (401, 403, 404):
        raise BitbucketProviderError("repository_inaccessible")
    if response.status_code == 429:
        raise BitbucketProviderError(
            "provider_rate_limited", retry_after=_retry_after(response.headers.get("Retry-After"))
        )
    raise BitbucketProviderError("provider_unavailable")


def _retry_after(value: str | None) -> int | None:
    if value is None or len(value) > 64 or not value.isascii():
        return None
    if value.isdecimal():
        return min(int(value), 3600)
    try:
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            return None
        return min(max(int((parsed - datetime.now(UTC)).total_seconds()), 0), 3600)
    except (TypeError, ValueError, OverflowError):
        return None
