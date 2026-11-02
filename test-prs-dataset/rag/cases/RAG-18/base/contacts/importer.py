"""Bulk import of contacts from partner exports, where blank cells are routine."""

from collections.abc import Iterable, Mapping

from .keys import contact_key
from .models import Contact
from .store import save


def import_rows(rows: Iterable[Mapping[str, str]]) -> int:
    """Store every row and return how many were stored."""
    count = 0
    for row in rows:
        save(Contact(contact_key(row), row.get("name", "")))
        count += 1
    return count
