"""Who gets a pull request: the least loaded team member, ties broken round-robin."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence

from review_bot.config import Member


def candidates(
    team: Sequence[Member],
    author: str,
    load: Mapping[str, int],
    last: str | None,
    excluded: Collection[str] = (),
) -> list[Member]:
    """Team members in the order to try: lowest load first; among equals, the member after the
    last assigned one (team order, wrapping) first. The author and excluded members are skipped."""
    excluded_keys = {key.lower() for key in excluded} | {author.lower()}
    pool = [member for member in team if member.key not in excluded_keys]
    if not pool:
        return []
    keys = [member.key for member in team]
    start = (keys.index(last) + 1) if last in keys else 0

    def order(member: Member) -> tuple[int, int]:
        distance = (keys.index(member.key) - start) % len(keys)
        return load.get(member.key, 0), distance

    return sorted(pool, key=order)
