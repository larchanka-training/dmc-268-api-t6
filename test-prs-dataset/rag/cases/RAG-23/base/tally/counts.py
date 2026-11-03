"""Counters of finished and planned work."""

_DONE: dict[str, int] = {"api": 18, "web": 7, "docs": 3}
_PLANNED: dict[str, int] = {"api": 24, "web": 21, "docs": 0}


def done(area: str) -> int:
    return _DONE.get(area, 0)


def planned(area: str) -> int:
    return _PLANNED.get(area, 0)
