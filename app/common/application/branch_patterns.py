"""Target-branch filter patterns of a repository (#149, SD §6.1, §12).

Shared by the repository settings DTO, which validates them, and the review run selection
rule, which matches the pull request's target branch against them.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

BRANCH_PATTERN_MAX_LENGTH = 255
BRANCH_FILTER_MAX_PATTERNS = 20
_FORBIDDEN_CHARACTERS = frozenset("?[]{}!\\~^:")


def _pattern_error(pattern: str) -> str | None:
    """Why ``pattern`` is rejected, or ``None`` when it is valid."""
    if not pattern:
        return "must not be empty"
    if len(pattern) > BRANCH_PATTERN_MAX_LENGTH:
        return f"must be at most {BRANCH_PATTERN_MAX_LENGTH} characters"
    if any(char.isspace() or unicodedata.category(char) == "Cc" for char in pattern):
        return "must not contain whitespace or control characters"
    if "***" in pattern:
        return "must not contain three or more '*' in a row"
    if _FORBIDDEN_CHARACTERS.intersection(pattern):
        return "must not contain any of ? [ ] { } ! \\ ~ ^ :"
    if ".." in pattern or "//" in pattern:
        return "must not contain '..' or '//'"
    if any(segment.startswith(".") for segment in pattern.split("/")):
        return "must not have a segment starting with '.'"
    if pattern.startswith(("/", "-")):
        return "must not start with '/' or '-'"
    if pattern.endswith(("/", ".", ".lock")):
        return "must not end with '/', '.' or '.lock'"
    if pattern == "@":
        return "must not be '@'"
    return None


def validate_branch_patterns(patterns: Sequence[str]) -> None:
    """Raise ``ValueError`` unless ``patterns`` is a valid branch filter."""
    if len(patterns) > BRANCH_FILTER_MAX_PATTERNS:
        raise ValueError(f"at most {BRANCH_FILTER_MAX_PATTERNS} branch patterns are allowed")
    if len(set(patterns)) != len(patterns):
        raise ValueError("branch patterns must not repeat")
    for pattern in patterns:
        error = _pattern_error(pattern)
        if error is not None:
            raise ValueError(f"branch pattern {pattern!r} {error}")


def _tokens(pattern: str) -> list[str]:
    """Split ``pattern`` into ``**``, ``*`` and single literal characters, left to right."""
    tokens = []
    index = 0
    while index < len(pattern):
        token = "**" if pattern.startswith("**", index) else pattern[index]
        tokens.append(token)
        index += len(token)
    return tokens


def _pattern_matches(branch: str, pattern: str) -> bool:
    """Match without backtracking, in O(tokens x branch length).

    The filter is user input matched at every claim, so a backtracking regex engine could
    hang the worker on a pattern like ``*a*a*a*b``. ``reachable[i]`` holds whether the tokens
    consumed so far match ``branch[:i]``: ``**`` matches any characters including ``/``,
    ``*`` any characters except ``/``, both possibly none; every other character is literal.
    """
    reachable = [True] + [False] * len(branch)
    for token in _tokens(pattern):
        if token in ("*", "**"):
            # In place, ascending: a star extends every reachable prefix by allowed characters.
            for index in range(1, len(reachable)):
                if (
                    not reachable[index]
                    and reachable[index - 1]
                    and (token == "**" or branch[index - 1] != "/")
                ):
                    reachable[index] = True
        else:
            reachable = [False] + [
                reachable[index] and char == token for index, char in enumerate(branch)
            ]
        if not any(reachable):
            return False
    return reachable[-1]


def branch_matches(branch: str, patterns: Sequence[str]) -> bool:
    """Whether ``branch`` is selected by the filter; an empty filter selects every branch."""
    return not patterns or any(_pattern_matches(branch, pattern) for pattern in patterns)
