from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict

from app.modules.reviews.application.run_trigger import RunTrigger


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _to_utc(value: datetime) -> datetime:
    return value.astimezone(UTC)


# The same instant in UTC, so the JSON always ends in ``Z`` whatever zone the database
# session returned it in.
UtcDatetime = Annotated[datetime, AfterValidator(_to_utc)]


class ApiDto(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class PullRequestDto(ApiDto):
    repo: str
    number: int
    title: str
    url: str
    head_sha: str


class RunUsageDto(ApiDto):
    tokens_in: int
    tokens_out: int
    cost_usd: float


class RunSessionDto(ApiDto):
    id: UUID
    status: str
    engine: str
    attempt: int
    cancel_requested: bool
    trigger: RunTrigger
    created_at: UtcDatetime
    started_at: UtcDatetime | None
    finished_at: UtcDatetime | None
    error_code: str | None
    model: str | None
    action_count: int
    summary_only: bool
    usage: RunUsageDto
    triggered_by: str | None
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
    created_at: UtcDatetime


class RunActionDto(ApiDto):
    id: UUID
    run_id: UUID
    index: int
    tool: str
    request: dict[str, Any]
    response: Any | None
    response_ref: str | None
    started_at: UtcDatetime
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
    updated_at: UtcDatetime
    latest_run: LatestRunDto | None


class PullRequestPageDto(ApiDto):
    items: list[PullRequestSummaryDto]
    next_cursor: str | None


class RunsByStatusDto(ApiDto):
    queued: int
    running: int
    publishing: int
    succeeded: int
    failed: int
    cancelled: int
    skipped: int


class PullsByVerdictDto(ApiDto):
    blocking: int
    attention: int
    clean: int
    no_verdict: int


class FindingsByCategoryDto(ApiDto):
    security: int
    correctness: int
    performance: int
    readability: int


class MetricsTotalsDto(ApiDto):
    runs_by_status: RunsByStatusDto
    pulls_by_verdict: PullsByVerdictDto
    findings_by_severity: SeverityCountsDto
    findings_by_category: FindingsByCategoryDto
    succeeded_average_duration_seconds: float
    tokens_in: int
    tokens_out: int
    cost_usd: float


class MetricsDayDto(ApiDto):
    day: date
    findings_by_category: FindingsByCategoryDto
    run_count: int
    average_duration_seconds: float
    cost_usd: float


class WorkspaceDailyBudgetDto(ApiDto):
    name: str
    daily_budget_usd: float
    spent_today_usd: float
    paused: bool


class MetricsSummaryDto(ApiDto):
    range: str
    totals: MetricsTotalsDto
    series: list[MetricsDayDto]
    workspaces: list[WorkspaceDailyBudgetDto]
