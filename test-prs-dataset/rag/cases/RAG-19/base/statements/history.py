"""History of an account as the statement lists it."""

from collections.abc import Iterable

from .models import Entry
from .ordering import sort_entries


def account_history(entries: Iterable[Entry]) -> list[Entry]:
    """Postings of an account, ready to list."""
    return sort_entries(entries)
