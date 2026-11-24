from __future__ import annotations

from review_bot.config import Member
from review_bot.selection import candidates


def test_candidates_order_by_load_then_round_robin_and_skip_the_author() -> None:
    team = (Member("a", "a"), Member("b", "b"), Member("c", "c"))
    assert [m.key for m in candidates(team, "x", {}, None)] == ["a", "b", "c"]
    assert [m.key for m in candidates(team, "x", {}, "a")] == ["b", "c", "a"]
    assert [m.key for m in candidates(team, "x", {"b": 1}, "a")] == ["c", "a", "b"]
    assert [m.key for m in candidates(team, "B", {}, "a")] == ["c", "a"]
