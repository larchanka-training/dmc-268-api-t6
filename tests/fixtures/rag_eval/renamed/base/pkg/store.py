"""Lookup of stored values."""

_DATA = {"alpha": 1, "beta": 2}


def load(key: str) -> int:
    return _DATA[key]
