"""Readings of the weather stations."""

_READINGS = {"oslo": (4, -3), "lisbon": (17, 11), "cairo": (24, 14)}


def read_station(city: str) -> tuple[int, int]:
    return _READINGS[city.lower()]
