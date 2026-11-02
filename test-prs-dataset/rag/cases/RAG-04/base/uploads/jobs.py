"""Scheduled jobs of the upload service."""

from .retention import purge_older_than


def nightly_cleanup() -> str:
    ttl_days = 7
    removed = purge_older_than(ttl_days)
    return f"purged {removed} uploads"
