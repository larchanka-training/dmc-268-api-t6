"""The bounded PR commit-message prefix: its limits, tail rule and value type (#177).

Production retrieval (``github_vcs``) and the eval validator of frozen prefix evidence
(``review/scripts/commit_context.py``) must share one definition, so a changed limit cannot
make the eval accept a prefix that production would never read. ``CommitMessagePrefix`` is
the value both sides produce; ``vcs_diff`` re-exports it for the production ports.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

type CommitEncoding = Literal["identity"]

COMMIT_PAGE_SIZE = 10
MAX_COMMIT_PAGES = 2
# Commit messages are a fixed cost outside the diff budget: keep them bounded.
MAX_COMMIT_MESSAGES = 20
MAX_COMMIT_MESSAGE_CHARS = 500
# Checked before strict parsing; identity encoding keeps decompression under the cap.
MAX_COMMIT_RESPONSE_BYTES = 1_048_576
COMMIT_ACCEPT_ENCODING: CommitEncoding = "identity"
# Numeric pages read in API order: enough for the message cap, never above the page cap.
COMMIT_PAGE_COUNT = min(
    MAX_COMMIT_PAGES, (MAX_COMMIT_MESSAGES + COMMIT_PAGE_SIZE - 1) // COMMIT_PAGE_SIZE
)


@dataclass(frozen=True)
class CommitMessagePrefix:
    """Retained API-order messages; incomplete means an additional tail may exist."""

    messages: tuple[str, ...]
    incomplete: bool = False


def commit_prefix_incomplete(
    *, has_next: bool, page: int, batch_length: int, remaining: int
) -> bool:
    """Whether commits may follow the prefix, decided by the last numeric page read.

    ``remaining`` is the message capacity left before this page's ``batch_length`` entries.
    A full last page is incomplete even without ``rel="next"``: the page cap stops reading.
    """
    return (
        has_next
        or batch_length > remaining
        or (page == COMMIT_PAGE_COUNT and batch_length == COMMIT_PAGE_SIZE)
    )
