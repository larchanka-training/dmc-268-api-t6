from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class ApiDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PullRequestDto(ApiDto):
    repo: str
    number: int
    title: str
    url: str
    head_sha: str


class RunSessionDto(ApiDto):
    id: UUID
    status: str
    engine: str
    attempt: int
    cancel_requested: bool
    trigger: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    model: str | None
    action_count: int
    summary_only: bool
    pull_request: PullRequestDto


class RunListDto(ApiDto):
    items: list[RunSessionDto]
    next_cursor: str | None


class ReviewCommentDto(ApiDto):
    id: UUID
    file: str
    old_line: int | None
    new_line: int | None
    end_line: int | None
    severity: str
    category: str
    title: str
    body: str
    rule_name: str | None
    created_at: datetime


class RunActionDto(ApiDto):
    id: UUID
    run_id: UUID
    index: int
    tool: str
    request: dict[str, Any]
    response: Any | None
    response_ref: str | None
    started_at: datetime
    duration_ms: int


class DiffFileDto(ApiDto):
    filename: str
    patch: str | None


class FileLinesDto(ApiDto):
    path: str
    start_line: int
    lines: list[str]
    total_lines: int
    next_offset: int | None


class PullRequestDetailDto(PullRequestDto):
    author: str | None
    head_ref: str | None
    base_ref: str | None


class FindingViewDto(ApiDto):
    id: UUID
    file: str
    old_line: int | None
    new_line: int | None
    end_line: int | None
    side: str
    severity: str
    category: str
    title: str
    body: str
    suggestion: str | None
    confidence: float
    rule_name: str | None


class ReviewSummaryDto(ApiDto):
    problem: str
    done_well: str
    effort: str


class SeverityCountsDto(ApiDto):
    critical: int
    high: int
    medium: int
    low: int
    info: int


class RunBudgetDto(ApiDto):
    tokens_in: int
    tokens_out: int
    cost_usd: float
    token_limit: int
    cost_limit_usd: float


class RunDetailDto(RunSessionDto):
    pull_request: PullRequestDetailDto
    findings: list[FindingViewDto]
    summary: ReviewSummaryDto | None
    verdict: str | None
    severity_counts: SeverityCountsDto
    budget: RunBudgetDto | None


class LatestRunDto(ApiDto):
    id: UUID
    status: str
    verdict: str | None


class PullRequestSummaryDto(ApiDto):
    number: int
    title: str
    url: str
    author: str | None
    head_sha: str
    updated_at: datetime
    latest_run: LatestRunDto | None


class PullRequestPageDto(ApiDto):
    items: list[PullRequestSummaryDto]
    next_cursor: str | None
