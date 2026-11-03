"""Order of the postings on a statement."""

from collections.abc import Iterable

from .models import Entry


def sort_entries(entries: Iterable[Entry]) -> list[Entry]:
    """Postings of an account in the order the statement lists them."""
    return sorted(entries, key=lambda entry: entry.at)
