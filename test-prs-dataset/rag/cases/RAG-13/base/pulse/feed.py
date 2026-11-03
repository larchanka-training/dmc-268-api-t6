"""Storage of the activity feed."""

from .events import Event

_FEED: list[Event] = []


def append(event: Event) -> None:
    _FEED.append(event)


def recent(user_id: str, limit: int = 20) -> list[Event]:
    """Return the latest events of the user, newest first."""
    own = [event for event in _FEED if event.user_id == user_id]
    return list(reversed(own))[:limit]
