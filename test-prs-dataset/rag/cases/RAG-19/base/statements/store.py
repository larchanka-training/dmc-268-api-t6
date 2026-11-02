"""In-memory postings of the accounts."""

from .models import Entry

_ENTRIES: dict[str, list[Entry]] = {}


def post(account: str, entry: Entry) -> None:
    _ENTRIES.setdefault(account, []).append(entry)


def entries_of(account: str) -> list[Entry]:
    return list(_ENTRIES.get(account, []))
