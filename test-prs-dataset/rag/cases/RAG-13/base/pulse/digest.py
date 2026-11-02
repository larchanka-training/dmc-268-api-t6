"""Weekly digest of activity."""

from .feed import recent
from .labels import kind_label


def digest_lines(user_id: str) -> list[str]:
    return [
        f"{kind_label(event.kind)} at {event.at:%a %H:%M}" for event in recent(user_id, limit=5)
    ]
