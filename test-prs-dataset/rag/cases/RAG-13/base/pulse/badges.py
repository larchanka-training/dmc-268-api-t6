"""Badges shown next to a user name."""


def badge_for(event_count: int) -> str:
    if event_count >= 100:
        return "veteran"
    if event_count >= 10:
        return "regular"
    return "newcomer"
