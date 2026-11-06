"""The status a public repository badge shows, read without a signed-in user (#454)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.common.application.github_repository_name import (
    REPOSITORY_FULL_NAME_MAX_LENGTH,
    REPOSITORY_FULL_NAME_PATTERN,
)
from app.modules.reviews.application.list_pulls import run_verdict
from app.modules.reviews.application.saved_review_result import SavedReviewResult

_FULL_NAME = re.compile(REPOSITORY_FULL_NAME_PATTERN)


class BadgeStatus(StrEnum):
    PASSED = "passed"
    ATTENTION = "attention"
    BLOCKING = "blocking"
    NO_DATA = "no data"


_STATUS_OF_VERDICT = {
    "clean": BadgeStatus.PASSED,
    "attention": BadgeStatus.ATTENTION,
    "blocking": BadgeStatus.BLOCKING,
}


@dataclass(frozen=True)
class BadgeRun:
    """What the verdict of a succeeded run is derived from."""

    summary_only: bool
    published_severities: tuple[str, ...]
    saved_result: SavedReviewResult | None


class RepositoryBadgeQueries(Protocol):
    async def latest_succeeded_run(self, full_name: str) -> BadgeRun | None:
        """The last succeeded run of the one public repository with this name.

        ``None`` for every other case, so that a private repository cannot be told from an
        unknown one.
        """
        ...


class GetRepositoryBadge:
    def __init__(self, queries: RepositoryBadgeQueries) -> None:
        self._queries = queries

    async def execute(self, owner: str, repo: str) -> BadgeStatus:
        """An anonymous address: a name GitHub cannot have is simply a repository we don't know."""
        full_name = f"{owner}/{repo}"
        if len(full_name) > REPOSITORY_FULL_NAME_MAX_LENGTH or not _FULL_NAME.fullmatch(full_name):
            return BadgeStatus.NO_DATA
        run = await self._queries.latest_succeeded_run(full_name)
        if run is None:
            return BadgeStatus.NO_DATA
        verdict = run_verdict(
            "succeeded", run.summary_only, run.published_severities, run.saved_result
        )
        # No fallback to an older run: the badge shows the latest review or nothing.
        return BadgeStatus.NO_DATA if verdict is None else _STATUS_OF_VERDICT[verdict]
