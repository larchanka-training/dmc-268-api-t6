"""Route lengths between depots."""

ROUTE_MINUTES: dict[tuple[str, str], int] = {
    ("north", "south"): 42,
    ("north", "east"): 25,
}


def route_minutes(origin: str, destination: str) -> int:
    return ROUTE_MINUTES[(origin, destination)]
