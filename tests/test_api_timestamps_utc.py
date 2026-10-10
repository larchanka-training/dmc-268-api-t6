"""Every timestamp the browser API returns is in UTC, whatever zone it was read in."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, get_args, get_origin
from uuid import UUID

from pydantic import AfterValidator

import app.main  # noqa: F401  (registers every DTO of the composed app)
from app.modules.reviews.api.dtos import (
    ApiDto,
    PullRequestDto,
    PullRequestSummaryDto,
    ReviewCommentDto,
    RunActionDto,
    RunSessionDto,
    _to_utc,
)

# One instant, written the way a database session in UTC+2 returns it.
LOCAL = datetime(2026, 9, 25, 1, 59, 30, tzinfo=timezone(timedelta(hours=2)))
IN_UTC = "2026-09-24T23:59:30Z"
ID = UUID("00000000-0000-0000-0000-000000000001")


def _json(dto: ApiDto) -> dict[str, Any]:
    return dto.model_dump(by_alias=True, mode="json")


def test_run_session_timestamps_are_serialized_in_utc() -> None:
    dto = RunSessionDto(
        id=ID,
        status="succeeded",
        engine="fast",
        attempt=1,
        cancel_requested=False,
        trigger="webhook",
        created_at=LOCAL,
        started_at=LOCAL,
        finished_at=LOCAL,
        error_code=None,
        model=None,
        action_count=0,
        summary_only=False,
        pull_request=PullRequestDto(
            repo="org/repo",
            number=1,
            title="Title",
            url="https://example.test/pr/1",
            head_sha="a" * 40,
        ),
    )

    body = _json(dto)
    assert (body["createdAt"], body["startedAt"], body["finishedAt"]) == (IN_UTC, IN_UTC, IN_UTC)


def test_review_comment_timestamp_is_serialized_in_utc() -> None:
    dto = ReviewCommentDto(
        id=ID,
        file="app/service.py",
        old_line=None,
        new_line=1,
        end_line=None,
        severity="high",
        category="correctness",
        title="Title",
        body="Body",
        rule_name=None,
        created_at=LOCAL,
    )

    assert _json(dto)["createdAt"] == IN_UTC


def test_run_action_timestamp_is_serialized_in_utc() -> None:
    dto = RunActionDto(
        id=ID,
        run_id=ID,
        index=0,
        tool="review",
        request={},
        response=None,
        response_ref=None,
        started_at=LOCAL,
        duration_ms=1,
    )

    assert _json(dto)["startedAt"] == IN_UTC


def test_pull_request_summary_timestamp_is_serialized_in_utc() -> None:
    dto = PullRequestSummaryDto(
        number=1,
        title="Title",
        url="https://example.test/pr/1",
        author=None,
        head_sha="a" * 40,
        updated_at=LOCAL,
        latest_run=None,
    )

    assert _json(dto)["updatedAt"] == IN_UTC


def _has_bare_datetime(annotation: Any, metadata: tuple[Any, ...] = ()) -> bool:
    """Whether the annotation holds a ``datetime`` that is not converted to UTC."""
    if annotation is datetime:
        return not any(
            isinstance(item, AfterValidator) and item.func is _to_utc for item in metadata
        )
    if get_origin(annotation) is Annotated:
        base, *inner = get_args(annotation)
        return _has_bare_datetime(base, tuple(inner))
    return any(_has_bare_datetime(argument) for argument in get_args(annotation))


def _api_dtos(base: type[ApiDto] = ApiDto) -> list[type[ApiDto]]:
    found = []
    for dto in base.__subclasses__():
        found += [dto, *_api_dtos(dto)]
    return found


def test_no_api_dto_declares_a_timestamp_outside_utc() -> None:
    # A new timestamp field must use UtcDatetime, or the ui Zod contract rejects its offset.
    bare = [
        f"{dto.__name__}.{name}"
        for dto in _api_dtos()
        for name, field in dto.model_fields.items()
        if _has_bare_datetime(field.annotation, tuple(field.metadata))
    ]

    assert _api_dtos()
    assert bare == []
