"""Every timestamp the browser API returns is in UTC, whatever zone it was read in."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, TypeAliasType, get_args, get_origin
from uuid import UUID

from fastapi import FastAPI
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    FutureDatetime,
    NaiveDatetime,
    PastDatetime,
)
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
    RunUsageDto,
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
        usage=RunUsageDto(tokens_in=0, tokens_out=0, cost_usd=0.0),
        triggered_by=None,
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


def _parts(annotation: Any) -> tuple[Any, ...]:
    """The arguments of an annotation, led by the alias it subscripts (``type Page[T] = ...``)."""
    # The body may hold a model or a datetime that no argument names: the alias leads,
    # unsubscripted, so the walk unwraps it like a plain alias.
    origin = get_origin(annotation)
    if isinstance(origin, TypeAliasType):
        return (origin, *get_args(annotation))
    return get_args(annotation)


# pydantic's datetime types are classes of their own at runtime, not datetime: each is named.
_DATETIMES = (datetime, AwareDatetime, PastDatetime, FutureDatetime, NaiveDatetime)


def _has_bare_datetime(
    annotation: Any,
    metadata: tuple[Any, ...] = (),
    expanding: frozenset[TypeAliasType] = frozenset(),
) -> bool:
    """Whether the annotation holds a ``datetime`` that is not converted to UTC."""
    if annotation in _DATETIMES:
        return not any(
            isinstance(item, AfterValidator) and item.func is _to_utc for item in metadata
        )
    if isinstance(annotation, TypeAliasType):
        if annotation in expanding:  # a recursive alias (type T = X | list[T]) meets itself
            return False
        return _has_bare_datetime(annotation.__value__, metadata, expanding | {annotation})
    if get_origin(annotation) is Annotated:
        base, *inner = get_args(annotation)
        return _has_bare_datetime(base, tuple(inner), expanding)
    return any(_has_bare_datetime(argument, (), expanding) for argument in _parts(annotation))


def _api_dtos(base: type[ApiDto] = ApiDto) -> list[type[ApiDto]]:
    found = []
    for dto in base.__subclasses__():
        found += [dto, *_api_dtos(dto)]
    return found


def _bare_timestamps(models: Sequence[type[BaseModel]]) -> list[str]:
    return [
        f"{model.__name__}.{name}"
        for model in models
        for name, field in model.model_fields.items()
        if _has_bare_datetime(field.annotation, tuple(field.metadata))
    ]


def test_no_api_dto_declares_a_timestamp_outside_utc() -> None:
    # A new timestamp field must use UtcDatetime, or the ui Zod contract rejects its offset.
    bare = _bare_timestamps(_api_dtos())

    assert _api_dtos()
    assert bare == []


def _models(
    annotation: Any, expanding: frozenset[TypeAliasType] = frozenset()
) -> list[type[BaseModel]]:
    """The models an annotation holds, unpacking list, unions, Annotated, dict values, aliases."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return [annotation]
    if isinstance(annotation, TypeAliasType):
        if annotation in expanding:  # a recursive alias (type T = X | list[T]) meets itself
            return []
        return _models(annotation.__value__, expanding | {annotation})
    # Annotated metadata and dict keys are never models, so walking every argument is enough.
    # None (a streamed or empty response, or a responses entry without "model") and object (a
    # raw stored payload) have no arguments: they yield no model to walk.
    return [model for argument in _parts(annotation) for model in _models(argument, expanding)]


def _route_annotations(routes: Sequence[BaseRoute]) -> list[tuple[str, Any]]:
    """Each annotation an ``/api/`` route declares, with the route path."""
    # app.routes holds each included router as a wrapper, not an APIRoute: unwrap it first.
    # A Mount is not descended into: the app mounts nothing, so no /api/ route hides in one.
    return [
        (route.path_format, annotation)
        for route in iter_route_contexts(routes)
        if route.path_format is not None
        and route.path_format.startswith("/api/")
        and isinstance(route.original_route, APIRoute)
        # The declared response model, then the model of each status in responses={...}.
        for annotation in (
            route.response_model,
            *(response.get("model") for response in route.responses.values()),
        )
    ]


def _response_models(routes: Sequence[BaseRoute]) -> list[type[BaseModel]]:
    """Every model an ``/api/`` route returns, with the models nested in its fields."""
    pending = [
        model for _, annotation in _route_annotations(routes) for model in _models(annotation)
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


def test_no_api_response_model_returns_a_timestamp_outside_utc() -> None:
    # Wider than the ApiDto walk: nested models and plain BaseModel responses (auth) count too.
    models = _response_models(app.main.app.routes)

    assert AuthSessionDto in models
    assert RagIndexDto in models  # no route returns it: reached only via RepositoryDto.rag_index
    assert _bare_timestamps(models) == []


def _routes_declaring_a_bare_datetime(routes: Sequence[BaseRoute]) -> list[str]:
    """The ``/api/`` routes whose own annotation holds a bare datetime, each path listed once."""
    # Response[datetime] over Envelope[T] reaches the model walk as Envelope[X] (field at: X) and
    # the argument datetime, which is no model: only the route's own annotation names it.
    return list(
        dict.fromkeys(
            path
            for path, annotation in _route_annotations(routes)
            if _has_bare_datetime(annotation)
        )
    )


def test_no_api_route_declares_a_timestamp_outside_utc() -> None:
    # A type parameter only takes an argument written in some annotation: a route's, checked
    # here, or a model field's, checked by the model walk. No parameter substitution is needed.
    assert _routes_declaring_a_bare_datetime(app.main.app.routes) == []


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


# FastAPI and pydantic keep a PEP 695 alias itself as the response model or field annotation.
type BareAlias = BareStamp
type BareAt = datetime
type UtcAt = UtcDatetime
type BareForest = BareStamp | list[BareForest]
type BareTimes = list[BareTimes] | datetime  # recursion first: any() stops at the datetime
type WithBareStamp[T] = list[T] | BareStamp
type WithBareAt[T] = tuple[T, datetime]


class AliasProbe(BaseModel):
    items: list[BareAlias]


class AliasStamps(BaseModel):
    bare: BareAt
    utc: UtcAt
    converted: Annotated[BareAt, AfterValidator(_to_utc)]


class RecursiveAliasStamp(BaseModel):
    at: BareTimes


class GenericAliasStamp(BaseModel):
    at: WithBareAt[int]


class PydanticStamps(BaseModel):
    aware: AwareDatetime
    past: PastDatetime
    future: FutureDatetime
    naive: NaiveDatetime
    converted: Annotated[AwareDatetime, AfterValidator(_to_utc)]


class Envelope[T](BaseModel):
    at: T


type Response[X] = Envelope[X]
type Id[X] = X
type L[X] = list[X]
type Opt[Y] = L[Y | None]


class Holder(BaseModel):
    env: Response[datetime]


async def _probe() -> None:
    return None


def _probe_routes(
    response_model: Any,
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


def test_response_walk_unwraps_a_model_alias_returned_by_a_route() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(BareAlias))) == ["BareStamp.at"]


def test_response_walk_unwraps_a_model_alias_nested_in_a_field() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(AliasProbe))) == ["BareStamp.at"]


def test_timestamp_check_unwraps_a_datetime_alias() -> None:
    # The UtcDatetime alias still carries its AfterValidator once unwrapped, and a validator
    # put on an alias reaches the datetime in its body.
    assert _bare_timestamps([AliasStamps]) == ["AliasStamps.bare"]


def test_timestamp_check_reports_the_pydantic_datetime_types() -> None:
    # Each keeps the zone it was given, NaiveDatetime none at all; the UTC validator still
    # converts AwareDatetime.
    assert _bare_timestamps([PydanticStamps]) == [
        "PydanticStamps.aware",
        "PydanticStamps.past",
        "PydanticStamps.future",
        "PydanticStamps.naive",
    ]


def test_response_walk_ends_on_a_recursive_model_alias() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(BareForest))) == ["BareStamp.at"]


def test_timestamp_check_ends_on_a_recursive_datetime_alias() -> None:
    # The model is checked directly: the response walk would expand BareTimes in _models first.
    assert _bare_timestamps([RecursiveAliasStamp]) == ["RecursiveAliasStamp.at"]


def test_response_walk_reports_a_model_in_the_body_of_a_generic_alias() -> None:
    # UtcStamp is the only argument: BareStamp is reached through the alias body alone.
    routes = _probe_routes(WithBareStamp[UtcStamp])

    assert _bare_timestamps(_response_models(routes)) == ["BareStamp.at"]


def test_timestamp_check_reports_a_datetime_in_the_body_of_a_generic_alias() -> None:
    assert _bare_timestamps([GenericAliasStamp]) == ["GenericAliasStamp.at"]


def test_route_check_reports_a_datetime_argument_of_a_generic_model_alias() -> None:
    # The model walk sees Envelope[X], whose field is a TypeVar: only the route check sees datetime.
    routes = _probe_routes(Response[datetime])

    assert _routes_declaring_a_bare_datetime(routes) == ["/api/probe"]


def test_route_check_accepts_a_utc_datetime_argument_of_a_generic_model_alias() -> None:
    routes = _probe_routes(Response[UtcDatetime])

    assert _routes_declaring_a_bare_datetime(routes) == []


def test_route_check_reports_a_bare_datetime_outside_any_model() -> None:
    assert _routes_declaring_a_bare_datetime(_probe_routes(Id[datetime])) == ["/api/probe"]


def test_route_check_accepts_a_utc_datetime_outside_any_model() -> None:
    assert _routes_declaring_a_bare_datetime(_probe_routes(list[UtcDatetime])) == []


def test_route_check_lists_a_route_once_across_its_responses_models() -> None:
    # The response model and the 404 model each hold a bare datetime: one path, listed once.
    routes = _probe_routes(Response[datetime], responses={404: {"model": Id[datetime]}})

    assert _routes_declaring_a_bare_datetime(routes) == ["/api/probe"]


def test_response_walk_reports_a_datetime_argument_of_a_generic_alias_in_a_field() -> None:
    assert _bare_timestamps(_response_models(_probe_routes(Holder))) == ["Holder.env"]


def test_response_walk_reports_a_model_argument_passed_through_two_aliases() -> None:
    # Opt's body L[Y | None] names no model: BareStamp is reached through the argument alone.
    routes = _probe_routes(Opt[BareStamp])

    assert _bare_timestamps(_response_models(routes)) == ["BareStamp.at"]
