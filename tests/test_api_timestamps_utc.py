"""Every timestamp the browser API returns is in UTC, whatever zone it was read in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, get_args, get_origin
from uuid import UUID

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import AfterValidator, BaseModel
from starlette.routing import BaseRoute

import app.main  # registers every DTO of the composed app; its routes feed the response walk
from app.modules.auth.api.router import AuthSessionDto
from app.modules.repositories.api.dtos import RagIndexDto
from app.modules.reviews.api.dtos import (
    ApiDto,
    PullRequestDto,
    PullRequestSummaryDto,
    ReviewCommentDto,
    RunActionDto,
    RunSessionDto,
    UtcDatetime,
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


def _models(annotation: Any) -> list[type[BaseModel]]:
    """The models an annotation holds, unpacking list, unions, Annotated and dict values."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    # Annotated metadata and dict keys are never models, so walking every argument is enough.
    # None (a streamed or empty response, or a responses entry without "model") and object (a
    # raw stored payload) have no arguments: they yield no model to walk.
    return [model for argument in get_args(annotation) for model in _models(argument)]


def _response_models(routes: Sequence[BaseRoute]) -> list[type[BaseModel]]:
    """Every model an ``/api/`` route returns, with the models nested in its fields."""
    # app.routes holds each included router as a wrapper, not an APIRoute: unwrap it first.
    # A Mount is not descended into: the app mounts nothing, so no /api/ route hides in one.
    pending = [
        model
        for route in iter_route_contexts(routes)
        if route.path_format is not None
        and route.path_format.startswith("/api/")
        and isinstance(route.original_route, APIRoute)
        # The declared response model, then the model of each status in responses={...}.
        for annotation in (
            route.response_model,
            *(response.get("model") for response in route.responses.values()),
        )
        for model in _models(annotation)
    ]
    found: list[type[BaseModel]] = []
    seen: set[type[BaseModel]] = set()
    while pending:
        model = pending.pop(0)
        if model in seen:
            continue
        seen.add(model)
        # An unresolved forward reference leaves a field annotation that _models cannot walk.
        assert model.__pydantic_complete__, model
        found.append(model)
        pending += [
            nested for field in model.model_fields.values() for nested in _models(field.annotation)
        ]
    return found


def _bare_timestamps(models: list[type[BaseModel]]) -> list[str]:
    return [
        f"{model.__name__}.{name}"
        for model in models
        for name, field in model.model_fields.items()
        if _has_bare_datetime(field.annotation, tuple(field.metadata))
    ]


def test_no_api_response_model_returns_a_timestamp_outside_utc() -> None:
    # Wider than the ApiDto walk: nested models and plain BaseModel responses (auth) count too.
    models = _response_models(app.main.app.routes)

    assert AuthSessionDto in models
    assert RagIndexDto in models  # no route returns it: reached only via RepositoryDto.rag_index
    assert _bare_timestamps(models) == []


class BareStamp(BaseModel):
    at: datetime


class BareProbe(BaseModel):
    items: list[BareStamp]


class UtcStamp(BaseModel):
    at: UtcDatetime


class UtcProbe(BaseModel):
    items: list[UtcStamp]


class BareTree(BaseModel):
    at: datetime
    children: list[BareTree]


async def _probe() -> None:
    return None


def _probe_routes(
    response_model: type[BaseModel],
    responses: dict[int | str, dict[str, Any]] | None = None,
) -> list[BaseRoute]:
    probe = FastAPI()
    probe.add_api_route("/api/probe", _probe, response_model=response_model, responses=responses)
    return probe.routes


def test_response_walk_reports_a_bare_datetime_in_a_nested_model() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(BareProbe))) == ["BareStamp.at"]


def test_response_walk_accepts_a_utc_datetime_in_a_nested_model() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(UtcProbe))) == []


def test_response_walk_ends_on_a_self_referencing_model() -> None:
    # Returning at all proves the walk ends; one entry proves BareTree was walked once.
    assert _bare_timestamps(_response_models(_probe_routes(BareTree))) == ["BareTree.at"]


def test_response_walk_reports_a_bare_datetime_in_a_responses_model() -> None:
    routes = _probe_routes(UtcProbe, responses={404: {"model": BareStamp}})

    assert _bare_timestamps(_response_models(routes)) == ["BareStamp.at"]
