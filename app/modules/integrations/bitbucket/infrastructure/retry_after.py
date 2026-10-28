"""Bounded Retry-After parsing for Bitbucket HTTP adapters."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from math import ceil


def parse_retry_after(value: str | None) -> timedelta | None:
    if value is None or len(value) > 64 or not value.isascii():
        return None
    if value.isdecimal():
        seconds = int(value)
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = ceil((date - datetime.now(UTC)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return None
    return timedelta(seconds=min(seconds, 86400)) if seconds > 0 else None
