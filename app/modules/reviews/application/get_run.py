"""Application service and read-model contract for a single review run."""

from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.get_run_comments import PublishedComment
from app.modules.reviews.application.list_pulls import run_verdict
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.verdict import SEVERITIES, Verdict, severity_counts


class RunDetailRepository(Protocol):
    async def get_run(self, run_id: UUID) -> RunListItem | None: ...


class GetRun:
    def __init__(self, repository: RunDetailRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> RunListItem | None:
        return await self._repository.get_run(run_id)


@dataclass(frozen=True)
class FindingView:
    """A published finding; anchor mapping as in ``PublishedComment`` (D6)."""

    comment: PublishedComment
    side: str
    suggestion: str | None
    confidence: float


@dataclass(frozen=True)
class RunReview:
    """Review results of one run as stored in PostgreSQL."""

    author: str | None
    head_ref: str | None
    base_ref: str | None
    findings: list[FindingView]
    summary: dict[str, str] | None
    usage_calls: int
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal


@dataclass(frozen=True)
class RunBudget:
    tokens_in: int
    tokens_out: int
    cost_usd: Decimal
    token_limit: int
    cost_limit_usd: Decimal


@dataclass(frozen=True)
class RunDetail:
    run: RunListItem
    review: RunReview
    verdict: Verdict | None
    severity_counts: dict[str, int]
    budget: RunBudget | None


# SD §13 caps per engine: input tokens and run cost.
ENGINE_LIMITS = {
    "fast": (60_000, Decimal("0.50")),
    "deep": (150_000, Decimal("3.00")),
}


class RunReviewRepository(RunDetailRepository, Protocol):
    async def get_run_review(self, run_id: UUID) -> RunReview | None: ...


class GetRunDetail:
    """RunSession plus findings, summary, verdict, severity counts and budget (D3)."""

    def __init__(self, repository: RunReviewRepository) -> None:
        self._repository = repository

    async def execute(self, run_id: UUID) -> RunDetail | None:
        run = await self._repository.get_run(run_id)
        review = await self._repository.get_run_review(run_id)
        if run is None or review is None:
            return None
        severities = tuple(item.comment.severity for item in review.findings)
        token_limit, cost_limit = ENGINE_LIMITS[run.engine]
        return RunDetail(
            run=run,
            review=replace(
                review,
                findings=sorted(
                    review.findings, key=lambda item: SEVERITIES.index(item.comment.severity)
                ),
            ),
            verdict=run_verdict(run.status, run.summary_only, severities),
            severity_counts=severity_counts(severities),
            budget=(
                RunBudget(
                    review.tokens_in, review.tokens_out, review.cost_usd, token_limit, cost_limit
                )
                if review.usage_calls
                else None
            ),
        )
