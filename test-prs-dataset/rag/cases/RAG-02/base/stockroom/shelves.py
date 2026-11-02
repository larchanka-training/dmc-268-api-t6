"""Shelf locations of the stock room."""

_SHELVES = {
    "00481516": "A-03",
    "00162342": "B-11",
}


def assign_shelf(sku: str) -> str:
    return _SHELVES.get(sku, "INBOX")
