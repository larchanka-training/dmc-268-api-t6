from datetime import datetime
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
    started_at: datetime | None
    finished_at: datetime | None
    error_code: str | None
    model: str | None
    action_count: int
    pull_request: PullRequestDto


class RunListDto(ApiDto):
    items: list[RunSessionDto]
    next_cursor: str | None


class ReviewCommentDto(ApiDto):
    id: UUID
    path: str
    old_line: int | None
    new_line: int | None
    severity: str
    category: str
    confidence: float
    title: str
    body: str
    suggestion: str | None
    rule_name: str | None
