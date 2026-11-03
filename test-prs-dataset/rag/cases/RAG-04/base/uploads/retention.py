"""Retention of stored uploads."""

from .storage import delete, list_older_than


def purge_older_than(days: int) -> int:
    """Delete uploads older than the given number of days and count them."""
    removed = 0
    for name in list_older_than(days):
        delete(name)
        removed += 1
    return removed
