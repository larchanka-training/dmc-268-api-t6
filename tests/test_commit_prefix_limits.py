"""Shared commit-prefix limits and tail rule of prod retrieval and the eval sidecar (#177)."""

from __future__ import annotations

import pytest

from app.modules.reviews.application.commit_prefix_limits import (
    COMMIT_ACCEPT_ENCODING,
    COMMIT_PAGE_COUNT,
    COMMIT_PAGE_SIZE,
    MAX_COMMIT_MESSAGE_CHARS,
    MAX_COMMIT_MESSAGES,
    MAX_COMMIT_PAGES,
    MAX_COMMIT_RESPONSE_BYTES,
    commit_prefix_incomplete,
)


def test_commit_prefix_limits_match_the_documented_budget() -> None:
    assert COMMIT_PAGE_SIZE == 10
    assert MAX_COMMIT_PAGES == 2
    assert MAX_COMMIT_MESSAGES == 20
    assert MAX_COMMIT_MESSAGE_CHARS == 500
    assert MAX_COMMIT_RESPONSE_BYTES == 1_048_576
    assert COMMIT_ACCEPT_ENCODING == "identity"
    assert COMMIT_PAGE_COUNT == 2


@pytest.mark.parametrize(
    ("has_next", "page", "batch_length", "remaining", "incomplete"),
    [
        # rel="next" marks a tail on any page, whatever its length.
        (True, 1, 0, 20, True),
        (True, 1, 3, 20, True),
        (True, 2, 3, 10, True),
        # A full last page stops at the page cap even without rel="next".
        (False, 2, 10, 10, True),
        # A short or empty last page ends the PR's commits.
        (False, 1, 0, 20, False),
        (False, 1, 3, 20, False),
        (False, 2, 0, 10, False),
        (False, 2, 9, 10, False),
        # A full first page is not the tail: the second page decides.
        (False, 1, 10, 20, False),
        # Overflow: the page holds more entries than the remaining capacity.
        (False, 1, 6, 5, True),
        (False, 1, 5, 5, False),
    ],
)
def test_commit_prefix_tail_truth_table(
    has_next: bool, page: int, batch_length: int, remaining: int, incomplete: bool
) -> None:
    assert (
        commit_prefix_incomplete(
            has_next=has_next, page=page, batch_length=batch_length, remaining=remaining
        )
        is incomplete
    )
