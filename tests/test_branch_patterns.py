"""Repository branch filter patterns: validation and matching (#149, decision (b))."""

from __future__ import annotations

import time

import pytest

from app.common.application.branch_patterns import branch_matches, validate_branch_patterns

# One pattern per rejection rule of the #149 decision (b); the API tests reuse them as 422 bodies.
INVALID_BRANCH_PATTERNS = {
    "empty": "",
    "longer-than-255": "a" * 256,
    "space": "release x",
    "tab": "release\tx",
    "newline": "release\n",
    "control": "release\x01x",
    "delete": "release\x7fx",
    "three-stars": "release/***",
    "question-mark": "release?",
    "open-bracket": "release[0",
    "close-bracket": "release]0",
    "open-brace": "release{0",
    "close-brace": "release}0",
    "exclamation-mark": "!release",
    "backslash": "release\\x",
    "tilde": "release~1",
    "caret": "release^1",
    "colon": "release:x",
    "double-dot": "release..x",
    "double-slash": "release//x",
    "dot-segment-start": ".release",
    "dot-inner-segment-start": "release/.x",
    "leading-slash": "/release",
    "leading-dash": "-release",
    "trailing-slash": "release/",
    "trailing-dot": "release.",
    "lock-suffix": "release.lock",
    "at-sign": "@",
}


@pytest.mark.parametrize(
    "pattern", INVALID_BRANCH_PATTERNS.values(), ids=INVALID_BRANCH_PATTERNS.keys()
)
def test_invalid_pattern_is_rejected(pattern: str) -> None:
    with pytest.raises(ValueError):
        validate_branch_patterns([pattern])


@pytest.mark.parametrize(
    "patterns",
    [
        [],
        ["*"],
        ["**"],
        ["main", "release/*", "feature/**/x"],
        ["v1.2", "main.locked", "user@x", "release-*", "a" * 255],
        [f"release/{number}" for number in range(20)],
    ],
)
def test_valid_patterns_are_accepted(patterns: list[str]) -> None:
    validate_branch_patterns(patterns)


def test_more_than_twenty_patterns_are_rejected() -> None:
    with pytest.raises(ValueError):
        validate_branch_patterns([f"release/{number}" for number in range(21)])


def test_duplicate_patterns_are_rejected() -> None:
    with pytest.raises(ValueError):
        validate_branch_patterns(["main", "release/*", "main"])


@pytest.mark.parametrize(
    ("patterns", "branch", "matches"),
    [
        ([], "anything/at/all", True),
        (["main"], "main", True),
        (["main"], "main2", False),
        (["main"], "feature/main", False),
        (["v1.2"], "v1x2", False),
        (["fix+(x)"], "fix+(x)", True),
        (["*"], "main", True),
        (["*"], "feature/x", False),
        (["release/*"], "release/1.2", True),
        (["release/*"], "release/1/2", False),
        (["release-*"], "release-1", True),
        (["**"], "feature/a/b", True),
        (["release/**"], "release/1/2", True),
        (["feature/**/fix"], "feature/a/b/fix", True),
        (["main", "release/*"], "release/1.2", True),
        (["main", "release/*"], "feature/x", False),
    ],
)
def test_branch_matches_segment_and_any_depth_wildcards(
    patterns: list[str], branch: str, matches: bool
) -> None:
    assert branch_matches(branch, patterns) is matches


def test_branch_matches_does_not_backtrack_on_a_worst_case_filter() -> None:
    # A user-supplied filter is matched at every claim: backtracking on stars would hang it.
    pattern = "*a" * 126 + "*b"
    patterns = ["*a" * 126 + f"*{last}" for last in "bcdefghijklmnopqrstu"]
    validate_branch_patterns([pattern])
    validate_branch_patterns(patterns)
    started = time.perf_counter()

    single = branch_matches("a" * 255, [pattern])
    many = branch_matches("a" * 255, patterns)

    assert (single, many) == (False, False)
    assert time.perf_counter() - started < 1
