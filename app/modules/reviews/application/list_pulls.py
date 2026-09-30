"""Pull requests of one repository with their latest run (api#20 D11)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.list_runs import RunCursor
from app.modules.reviews.application.verdict import Verdict, verdict


@dataclass(frozen=True)
class LatestRunRow:
    id: UUID
    status: str
    summary_only: bool
    published_severities: tuple[str, ...]


@dataclass(frozen=True)
class PullRequestRow:
    id: UUID
    number: int
    title: str
    url: str
    author: str | None
    head_sha: str
    updated_at: datetime
    latest_run: LatestRunRow | None


@dataclass(frozen=True)
class LatestRun:
    id: UUID
    status: str
    verdict: Verdict | None


@dataclass(frozen=True)
class PullRequestSummary:
    number: int
    title: str
    url: str
    author: str | None
    head_sha: str
    updated_at: datetime
    latest_run: LatestRun | None


@dataclass(frozen=True)
class PullRequestPage:
    items: list[PullRequestSummary]
    next_cursor: str | None


class PullRequestRepository(Protocol):
    async def list_pulls(
        self, repository_id: UUID, *, state: str, cursor: RunCursor | None, limit: int
    ) -> list[PullRequestRow] | None:
        """Most recently updated first; ``None`` when the repository is not visible."""
        ...


def run_verdict(status: str, summary_only: bool, severities: tuple[str, ...]) -> Verdict | None:
    """Null until the run succeeded, and for summary-only runs (PIPELINE_SPEC §11)."""
    if status != "succeeded" or summary_only:
        return None
    return verdict(severities)


class ListRepositoryPulls:
    def __init__(self, repository: PullRequestRepository) -> None:
        self._repository = repository

    async def execute(
        self,
        repository_id: UUID,
        *,
        state: str = "open",
        cursor: str | None = None,
        limit: int = 50,
    ) -> PullRequestPage | None:
        decoded = RunCursor.decode(cursor) if cursor is not None else None
        rows = await self._repository.list_pulls(
            repository_id, state=state, cursor=decoded, limit=limit + 1
        )
        if rows is None:
            return None
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            next_cursor = RunCursor(created_at=page[-1].updated_at, id=page[-1].id).encode()
        return PullRequestPage(
            items=[
                PullRequestSummary(
                    number=row.number,
                    title=row.title,
                    url=row.url,
                    author=row.author,
                    head_sha=row.head_sha,
                    updated_at=row.updated_at,
                    latest_run=(
                        LatestRun(
                            id=row.latest_run.id,
                            status=row.latest_run.status,
                            verdict=run_verdict(
                                row.latest_run.status,
                                row.latest_run.summary_only,
                                row.latest_run.published_severities,
                            ),
                        )
                        if row.latest_run is not None
                        else None
                    ),
                )
                for row in page
            ],
            next_cursor=next_cursor,
        )
