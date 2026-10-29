from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from types import UnionType
from typing import Annotated, Any, Literal, NoReturn, Union, cast, get_args, get_origin
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Path, Query, Request
from fastapi.dependencies.models import Dependant
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel, ValidationError

from app.bootstrap.gitlab import gitlab_webhook_lifespan
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import (
    ReviewsApiResources,
    get_cancel_run,
    get_file_blob_cache,
    get_github_webhook_receipt_uow_factory,
    get_pull_requests,
    get_repository_settings,
    get_rerun_uow_factory,
    get_rules_status,
    get_run_publisher,
    get_run_repository,
    reviews_api_lifespan,
)
from app.bootstrap.reviews_api import (
    get_run_event_hub as get_run_event_hub,
)
from app.bootstrap.run_step_listener import run_step_listener
from app.bootstrap.run_update_listener import run_update_listener
from app.common.application.github_repository_name import REPOSITORY_FULL_NAME_MAX_LENGTH
from app.common.infrastructure.db.enums import RunState
from app.modules.auth.api.router import auth_router
from app.modules.auth.application.scope import AuthScope
from app.modules.integrations.bitbucket.api.connections import (
    router as bitbucket_connections_router,
)
from app.modules.integrations.bitbucket.api.webhook import router as bitbucket_webhook_router
from app.modules.integrations.gitlab.api.webhook import router as gitlab_webhook_router
from app.modules.integrations.gitlab.infrastructure.settings import GitLabApiSettings
from app.modules.integrations.webhooks.api.dtos import GitHubWebhookPayloadDto
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    GitHubWebhookReceiptUnitOfWork,
    ReceiveGitHubDelivery,
)
from app.modules.repositories.api.dtos import (
    InvalidRulesStatusDto,
    MissingRulesStatusDto,
    PendingRulesStatusDto,
    PresentRulesStatusDto,
    RepositoryDto,
    RepositoryUpdateDto,
    RulesDiagnosticDto,
    RulesStatusDto,
    UnavailableRulesStatusDto,
)
from app.modules.repositories.application.repository_settings import (
    GetRepository,
    ListRepositories,
    RepositorySettings,
    RepositorySettingsChange,
    RepositorySettingsUowFactory,
    UnsupportedRepositorySetting,
    UpdateRepository,
)
from app.modules.repositories.application.rules_status import (
    GetRulesStatus,
    RulesStatus,
    RulesStatusKind,
)
from app.modules.reviews.api.dtos import (
    DiffFileDto,
    FileLinesDto,
    FindingViewDto,
    LatestRunDto,
    PullRequestDetailDto,
    PullRequestDto,
    PullRequestPageDto,
    PullRequestSummaryDto,
    ReviewCommentDto,
    ReviewSummaryDto,
    RunActionDto,
    RunBudgetDto,
    RunDetailDto,
    RunListDto,
    RunSessionDto,
    SeverityCountsDto,
)
from app.modules.reviews.application.cancel_run import (
    CancelRun,
)
from app.modules.reviews.application.get_run import (
    GetRunDetail,
    RunDetail,
    RunDetailRepository,
    RunReviewRepository,
)
from app.modules.reviews.application.get_run_actions import (
    GetRunActionResponse,
    GetRunActions,
    RunActionsRepository,
    RunActionTrace,
)
from app.modules.reviews.application.get_run_comments import (
    GetRunComments,
    PublishedComment,
    RunCommentsRepository,
)
from app.modules.reviews.application.get_run_diff import (
    DiffSnapshot,
    GetRunDiff,
    RunDiffRepository,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCache,
    FileLinesExpired,
    FileLinesNotFound,
    FileLinesPage,
    GetRunFileLines,
    RunFileRepository,
)
from app.modules.reviews.application.list_pulls import (
    ListRepositoryPulls,
    PullRequestRepository,
    PullRequestSummary,
)
from app.modules.reviews.application.list_runs import ListRuns, RunListItem, RunRepository
from app.modules.reviews.application.rerun_run import (
    RerunConflict,
    RerunNotConfigured,
    RerunRun,
    RerunUnitOfWork,
)
from app.modules.reviews.application.run_events import (
    InMemoryRunStepHub,
    InMemoryRunUpdateHub,
    RunAccessRepository,
    RunChange,
    RunStep,
    RunStepStream,
    RunUpdateStream,
    StreamKeepalive,
    iter_visible_run_events,
    parse_run_event_id,
    run_event_id,
    utc_rfc3339,
)
from app.modules.reviews.application.try_enqueue_webhook_run import RunMessagePublisher

api_router = APIRouter(prefix="/api", dependencies=[Depends(get_auth_scope)])
github_webhook_router = APIRouter(prefix="/webhooks/github")

run_update_hub = InMemoryRunUpdateHub()
run_step_hub = InMemoryRunStepHub()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Process resources plus the NOTIFY bridges from worker processes into ``/api/stream``."""
    gitlab_api = GitLabApiSettings.from_environment(os.environ)
    async with reviews_api_lifespan(app):
        resources = getattr(app.state, "reviews_api_resources", None)
        async with (
            gitlab_webhook_lifespan(
                app,
                resources.session_factory if isinstance(resources, ReviewsApiResources) else None,
                intake_enabled=gitlab_api.intake_enabled,
            ),
            run_update_listener(os.environ.get("DATABASE_URL"), run_update_hub),
            run_step_listener(os.environ.get("DATABASE_URL"), run_step_hub),
        ):
            yield


KEEPALIVE_INTERVAL_SECONDS = 15.0
# `Last-Event-ID` replay: reaches back this far before the id, because `updated_at` is the
# transaction start time and commits can land out of order; and sends at most this many runs.
REPLAY_OVERLAP_SECONDS = 30.0
REPLAY_LIMIT = 500

app = FastAPI(title="AI Code Reviewer browser API", lifespan=lifespan)
app.state.run_update_hub = run_update_hub
app.state.run_step_hub = run_step_hub

__all__ = [
    "KEEPALIVE_INTERVAL_SECONDS",
    "REPLAY_LIMIT",
    "REPLAY_OVERLAP_SECONDS",
    "api_router",
    "app",
    "get_cancel_run",
    "get_file_blob_cache",
    "get_repository_settings",
    "get_run_repository",
]


def _verifies_bearer(dependant: Dependant) -> bool:
    """Whether the dependency tree, nested dependencies included, calls ``get_auth_scope``."""
    return dependant.call is get_auth_scope or any(
        _verifies_bearer(dependency) for dependency in dependant.dependencies
    )


def build_openapi(application: FastAPI) -> dict[str, Any]:
    """Describe ``application``; bearer security follows each route's dependencies, not its URL."""
    openapi_schema = get_openapi(
        title="AI Code Reviewer browser API",
        version="0.1.0",
        description="Browser-facing HTTP surface of the AI Code Reviewer service",
        routes=application.routes,
    )
    openapi_schema["components"] = openapi_schema.get("components", {})
    openapi_schema["components"]["securitySchemes"] = {
        "bearerAuth": {
            "type": "http",
            "scheme": "bearer",
            "bearerFormat": "JWT",
            "description": "Access JWT issued by auth-api, valid for 15 minutes.",
        }
    }
    paths = openapi_schema.get("paths", {})
    for route in iter_route_contexts(application.routes):
        if not isinstance(route.original_route, APIRoute) or not _verifies_bearer(route.dependant):
            continue
        path_item = paths.get(route.path_format, {})
        for method in route.methods or ():
            operation = path_item.get(method.lower())
            if operation is None:
                continue
            if "security" not in operation:
                operation["security"] = [{"bearerAuth": []}]
            operation.setdefault("responses", {})
            operation["responses"].setdefault(
                "503", {"description": "Authentication is not configured"}
            )
            if "401" not in operation["responses"]:
                operation["responses"]["401"] = {
                    "description": "Missing or invalid Bearer access token"
                }
    return openapi_schema


def custom_openapi() -> dict[str, Any]:
    if app.openapi_schema:
        return app.openapi_schema
    app.openapi_schema = build_openapi(app)
    return app.openapi_schema


app.openapi = custom_openapi  # type: ignore[method-assign]


_SAFE_VALIDATION_MESSAGES = frozenset(
    {"bitbucket_wait_for_ci_unsupported", "bitbucket_review_event_unsupported"}
)


def _safe_validation_message(error: dict[str, Any]) -> str:
    if error["type"] == "missing":
        return "Field required"
    message = error["msg"]
    if (
        error["type"] == "value_error"
        and isinstance(message, str)
        and message in _SAFE_VALIDATION_MESSAGES
    ):
        return message
    return "Invalid input"


def _validation_parameter_annotation(dependant: Dependant, source: str, name: str) -> object | None:
    fields = {
        "path": dependant.path_params,
        "query": dependant.query_params,
        "header": dependant.header_params,
        "cookie": dependant.cookie_params,
    }[source]
    for field in fields:
        if field.alias == name:
            return field.field_info.annotation
    for dependency in dependant.dependencies:
        annotation = _validation_parameter_annotation(dependency, source, name)
        if annotation is not None:
            return annotation
    return None


def _safe_validation_segment(annotation: object, segment: object) -> tuple[str | int, object]:
    origin = get_origin(annotation)
    if origin is Annotated:
        return _safe_validation_segment(get_args(annotation)[0], segment)
    if origin in (Union, UnionType):
        for variant in get_args(annotation):
            safe, next_annotation = _safe_validation_segment(variant, segment)
            if safe == segment or next_annotation is not None:
                return safe, next_annotation
        return "<unknown field>", None
    if origin in (list, tuple) and type(segment) is int and segment >= 0:
        args = get_args(annotation)
        return segment, args[0] if args else Any
    if origin in (dict, Mapping):
        args = get_args(annotation)
        return "<unknown field>", args[1] if len(args) > 1 else Any
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        for name, field in annotation.model_fields.items():
            if segment in (name, field.alias, field.validation_alias):
                return cast(str, segment), field.annotation
    return "<unknown field>", None


def _safe_validation_location(request: Request, error: dict[str, Any]) -> tuple[str | int, ...]:
    location = error.get("loc")
    if not isinstance(location, (tuple, list)) or not location:
        return ("body",)
    source = location[0]
    if source not in {"body", "path", "query", "header", "cookie"}:
        return ("body",)
    route = request.scope.get("route")
    if not isinstance(route, APIRoute):
        return (source,)
    annotation: object = (
        route.body_field.field_info.annotation
        if source == "body" and route.body_field is not None
        else None
    )
    safe_location: list[str | int] = [source]
    for segment in location[1:]:
        if source != "body" and len(safe_location) == 1 and isinstance(segment, str):
            annotation = _validation_parameter_annotation(route.dependant, source, segment)
            if annotation is not None:
                safe_location.append(segment)
                continue
        safe_segment, annotation = _safe_validation_segment(annotation, segment)
        safe_location.append(safe_segment)
    return tuple(safe_location)


@app.exception_handler(RequestValidationError)
async def render_validation_error(request: Request, exc: RequestValidationError) -> Response:
    """Preserve FastAPI errors outside Bitbucket; redact workspace and webhook routes."""
    path = request.url.path
    segments = path.split("/")
    is_bitbucket_route = (
        (
            len(segments) >= 5
            and segments[1:3] == ["api", "workspaces"]
            and segments[4] == "bitbucket"
        )
        or path == "/webhooks/bitbucket"
        or path.startswith("/webhooks/bitbucket/")
    )
    if not is_bitbucket_route:
        try:
            return await request_validation_exception_handler(request, exc)
        except UnicodeEncodeError:
            # JSONResponse cannot encode a lone surrogate in a rejected value (#149).
            # Keep FastAPI's error fields on other providers, with escaped JSON text.
            content = json.dumps({"detail": jsonable_encoder(exc.errors())}, ensure_ascii=True)
            return Response(content=content, status_code=422, media_type="application/json")
    detail = [
        {
            "type": error["type"],
            "loc": _safe_validation_location(request, error),
            "msg": _safe_validation_message(error),
        }
        for error in exc.errors()
    ]
    return JSONResponse({"detail": detail}, status_code=422)


@app.get(
    "/healthcheck",
    summary="Service healthcheck",
    description=(
        "Verify application liveness and basic process availability "
        "without touching downstream resources."
    ),
)
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def get_github_webhook_secret(request: Request) -> str:
    """Return the configured shared secret for GitHub's raw-body signature."""
    secret = getattr(request.app.state, "github_webhook_secret", None)
    if not isinstance(secret, str) or not secret:
        raise HTTPException(status_code=503, detail="GitHub webhook is not configured")
    return secret


def _has_valid_github_signature(*, raw_body: bytes, signature: str | None, secret: str) -> bool:
    if signature is None or not signature.startswith("sha256="):
        return False
    received = signature.removeprefix("sha256=")
    if len(received) != 64 or any(character not in "0123456789abcdef" for character in received):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(received, expected)


MAX_WEBHOOK_PAYLOAD_BYTES: int = 10 * 1024 * 1024  # 10 MB


@github_webhook_router.post(
    "",
    status_code=202,
    responses={
        400: {"description": "Invalid webhook headers or payload"},
        401: {"description": "Invalid GitHub webhook signature"},
        413: {"description": "Webhook payload exceeds the size limit"},
        503: {"description": "GitHub webhook is not configured"},
    },
)
async def receive_github_webhook(
    request: Request,
    secret: Annotated[str, Depends(get_github_webhook_secret)],
    receipt_uow_factory: Annotated[
        Callable[[], GitHubWebhookReceiptUnitOfWork],
        Depends(get_github_webhook_receipt_uow_factory),
    ],
) -> JSONResponse:
    """Verify and persist a GitHub delivery before acknowledging it."""
    content_length = request.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_WEBHOOK_PAYLOAD_BYTES:
                raise HTTPException(status_code=413, detail="payload too large")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid Content-Length header") from error

    body_chunks: list[bytes] = []
    total_bytes = 0
    async for chunk in request.stream():
        total_bytes += len(chunk)
        if total_bytes > MAX_WEBHOOK_PAYLOAD_BYTES:
            raise HTTPException(status_code=413, detail="payload too large")
        body_chunks.append(chunk)
    raw_body = b"".join(body_chunks)

    if not _has_valid_github_signature(
        raw_body=raw_body,
        signature=request.headers.get("X-Hub-Signature-256"),
        secret=secret,
    ):
        raise HTTPException(status_code=401, detail="invalid GitHub webhook signature")

    event_name = request.headers.get("X-GitHub-Event")
    delivery_id = request.headers.get("X-GitHub-Delivery")
    if not event_name or not delivery_id or len(event_name) > 100 or len(delivery_id) > 255:
        raise HTTPException(status_code=400, detail="missing GitHub delivery headers")
    if not _is_github_delivery_id(delivery_id):
        raise HTTPException(status_code=400, detail="malformed GitHub delivery id")

    try:
        raw_payload = json.loads(
            raw_body,
            parse_float=_finite_json_float,
            parse_constant=_reject_nonfinite_json_constant,
        )
        _validate_jsonb_unicode(raw_payload)
        payload = GitHubWebhookPayloadDto.model_validate(raw_payload)
        stored_payload = payload.model_dump(mode="json", exclude_unset=True)
    except (ValueError, UnicodeDecodeError, ValidationError, RecursionError) as error:
        raise HTTPException(status_code=400, detail="malformed GitHub webhook payload") from error

    status = await ReceiveGitHubDelivery(uow_factory=receipt_uow_factory).execute(
        VerifiedGitHubDelivery(
            delivery_id=delivery_id,
            event_name=event_name,
            payload=stored_payload,
        ).to_receipt()
    )
    return JSONResponse(status_code=202, content={"status": status})


def _is_github_delivery_id(value: str) -> bool:
    """Whether ``value`` uses only GitHub's delivery GUID alphabet: ASCII letters, digits, ``-``.

    The stored id goes into webhook-worker log lines as is, so a space or ``key=value`` in it
    could forge a field there (docs/WEBHOOK_WORKER.md, outcome log).
    """
    return all(
        character.isascii() and (character.isalnum() or character == "-") for character in value
    )


def _reject_nonfinite_json_constant(value: str) -> NoReturn:
    raise ValueError(f"nonfinite JSON constant: {value}")


def _finite_json_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("nonfinite JSON number")
    return parsed


def _validate_jsonb_unicode(value: object) -> None:
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if any(character == "\x00" or 0xD800 <= ord(character) <= 0xDFFF for character in item):
                raise ValueError("invalid Unicode in PostgreSQL JSONB string")
        elif isinstance(item, dict):
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def to_run_session_dto(item: RunListItem) -> RunSessionDto:
    return RunSessionDto(
        id=item.id,
        provider=item.provider,
        status=item.status,
        engine=item.engine,
        attempt=item.attempt,
        cancel_requested=item.cancel_requested,
        trigger=item.trigger,
        created_at=item.created_at,
        started_at=item.started_at,
        finished_at=item.finished_at,
        error_code=item.error_code,
        model=item.model,
        action_count=item.action_count,
        summary_only=item.summary_only,
        pull_request=PullRequestDto(
            provider=item.provider,
            repo=item.repo,
            number=item.number,
            title=item.title,
            url=item.url,
            head_sha=item.head_sha,
        ),
    )


def to_run_detail_dto(detail: RunDetail) -> RunDetailDto:
    session = to_run_session_dto(detail.run)
    review = detail.review
    return RunDetailDto(
        **session.model_dump(exclude={"pull_request"}),
        pull_request=PullRequestDetailDto(
            **session.pull_request.model_dump(),
            author=review.author,
            head_ref=review.head_ref,
            base_ref=review.base_ref,
        ),
        findings=[
            FindingViewDto(
                id=item.comment.id,
                file=item.comment.file,
                old_line=item.comment.old_line,
                new_line=item.comment.new_line,
                end_line=item.comment.end_line,
                side=item.side,
                severity=item.comment.severity,
                category=item.comment.category,
                title=item.comment.title,
                body=item.comment.body,
                suggestion=item.suggestion,
                confidence=item.confidence,
                rule_name=item.comment.rule_name,
            )
            for item in review.findings
        ],
        summary=ReviewSummaryDto(**review.summary) if review.summary is not None else None,
        verdict=detail.verdict,
        severity_counts=SeverityCountsDto(**detail.severity_counts),
        budget=(
            RunBudgetDto(
                tokens_in=detail.budget.tokens_in,
                tokens_out=detail.budget.tokens_out,
                cost_usd=float(detail.budget.cost_usd),
                token_limit=detail.budget.token_limit,
                cost_limit_usd=float(detail.budget.cost_limit_usd),
            )
            if detail.budget is not None
            else None
        ),
    )


def to_repository_dto(item: RepositorySettings) -> RepositoryDto:
    return RepositoryDto.model_validate(item, from_attributes=True)


_RULES_STATUS_DTOS: dict[RulesStatusKind, type[BaseModel]] = {
    RulesStatusKind.PENDING: PendingRulesStatusDto,
    RulesStatusKind.PRESENT: PresentRulesStatusDto,
    RulesStatusKind.MISSING: MissingRulesStatusDto,
    RulesStatusKind.INVALID: InvalidRulesStatusDto,
    RulesStatusKind.UNAVAILABLE: UnavailableRulesStatusDto,
}


def to_rules_status_dto(item: RulesStatus) -> RulesStatusDto:
    """The variant of ``item.status``; a stored row that misses a field of it fails loudly."""
    values: dict[str, Any] = {
        "status": item.status.value,
        "branch": item.branch,
        "path": item.path,
        "sha": item.sha,
        "checked_at": item.checked_at,
        "text": item.text,
        "checksum": item.checksum,
        "ignore_masks": None if item.ignore_masks is None else list(item.ignore_masks),
        "diagnostics": [
            RulesDiagnosticDto.model_validate(diagnostic, from_attributes=True)
            for diagnostic in item.diagnostics
        ],
        "defaults_version": item.defaults_version,
    }
    dto = _RULES_STATUS_DTOS[item.status]
    return cast(RulesStatusDto, dto.model_validate({k: values[k] for k in dto.model_fields}))


def to_pull_request_summary_dto(item: PullRequestSummary) -> PullRequestSummaryDto:
    return PullRequestSummaryDto(
        number=item.number,
        title=item.title,
        url=item.url,
        author=item.author,
        head_sha=item.head_sha,
        updated_at=item.updated_at,
        latest_run=(
            LatestRunDto(
                id=item.latest_run.id,
                status=item.latest_run.status,
                verdict=item.latest_run.verdict,
            )
            if item.latest_run is not None
            else None
        ),
    )


def to_review_comment_dto(item: PublishedComment) -> ReviewCommentDto:
    return ReviewCommentDto(
        id=item.id,
        file=item.file,
        old_line=item.old_line,
        new_line=item.new_line,
        end_line=item.end_line,
        severity=item.severity,
        category=item.category,
        title=item.title,
        body=item.body,
        rule_name=item.rule_name,
        created_at=item.created_at,
    )


def to_run_action_dto(item: RunActionTrace) -> RunActionDto:
    return RunActionDto(
        id=item.id,
        run_id=item.run_id,
        index=item.index,
        tool=item.tool,
        request=item.request,
        response=item.response,
        response_ref=item.response_ref,
        started_at=item.started_at,
        duration_ms=item.duration_ms,
    )


def to_diff_file_dto(item: DiffSnapshot) -> DiffFileDto:
    return DiffFileDto(filename=item.filename, patch=item.patch)


def to_file_lines_dto(item: FileLinesPage) -> FileLinesDto:
    return FileLinesDto(
        path=item.path,
        start_line=item.start_line,
        lines=item.lines,
        total_lines=item.total_lines,
        next_offset=item.next_offset,
    )


@api_router.get(
    "/runs",
    response_model=RunListDto,
    summary="List review runs",
    description="Paginated list of review runs for visible repositories.",
    responses={
        401: {"description": "Unauthorized"},
        422: {"description": "Invalid query parameters"},
    },
)
async def list_runs(
    repository: Annotated[RunRepository, Depends(get_run_repository)],
    status: RunState | None = None,
    repo: Annotated[
        str | None, Query(min_length=1, max_length=REPOSITORY_FULL_NAME_MAX_LENGTH)
    ] = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> RunListDto:
    try:
        page = await ListRuns(repository).execute(
            status=status.value if status is not None else None,
            repository=repo,
            cursor=cursor,
            limit=limit,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return RunListDto(
        items=[to_run_session_dto(item) for item in page.items], next_cursor=page.next_cursor
    )


@api_router.get(
    "/runs/{run_id}",
    response_model=RunDetailDto,
    summary="Get review run details",
    description="Detailed information about a specific review run.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
    },
)
async def get_run(
    run_id: UUID,
    repository: Annotated[RunReviewRepository, Depends(get_run_repository)],
) -> RunDetailDto:
    detail = await GetRunDetail(repository).execute(run_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="run not found")
    return to_run_detail_dto(detail)


@api_router.post(
    "/runs/{run_id}/rerun",
    status_code=202,
    response_model=RunSessionDto,
    summary="Rerun a review",
    description="Trigger a new review run for the same pull request.",
    responses={
        202: {"description": "Rerun enqueued"},
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
        409: {
            "description": (
                "Conflict: Active run, closed PR, or repository reviews paused or unavailable"
            )
        },
        422: {"description": "Repository configuration missing"},
    },
)
async def rerun_run(
    run_id: UUID,
    uow_factory: Annotated[Callable[[], RerunUnitOfWork], Depends(get_rerun_uow_factory)],
    runs: Annotated[RunDetailRepository, Depends(get_run_repository)],
    publisher: Annotated[RunMessagePublisher | None, Depends(get_run_publisher)],
) -> RunSessionDto:
    try:
        item = await RerunRun(uow_factory, runs, publisher).execute(run_id)
    except RerunConflict as error:
        raise HTTPException(
            status_code=409,
            detail=(
                "the pull request has an active run or is closed, "
                "or repository reviews are paused or unavailable"
            ),
        ) from error
    except RerunNotConfigured as error:
        raise HTTPException(
            status_code=422, detail="the repository has no active rule or prompt version"
        ) from error
    if item is None:
        raise HTTPException(status_code=404, detail="run not found")
    return to_run_session_dto(item)


@api_router.post(
    "/runs/{run_id}/cancel",
    response_model=RunSessionDto,
    summary="Cancel review run",
    description="Request cancellation of an active or queued review run.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
    },
)
async def cancel_run(
    run_id: UUID,
    use_case: Annotated[CancelRun, Depends(get_cancel_run)],
) -> RunSessionDto:
    item = await use_case.execute(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="run not found")
    return to_run_session_dto(item)


def _run_updated_frame(run_id: UUID, status: str, updated_at: datetime) -> str:
    return (
        f"id: {run_event_id(updated_at)}\n"
        "event: run.updated\n"
        f'data: {{"runId":"{run_id}","status":"{status}"}}\n\n'
    )


def _run_step_frame(step: RunStep) -> str:
    payload: dict[str, object] = {
        "runId": str(step.run_id),
        "tool": step.tool,
        "index": step.index,
        "ok": step.ok,
        "startedAt": utc_rfc3339(step.started_at),
        "durationMs": step.duration_ms,
        "attempt": step.attempt,
    }
    if step.llm_operation is not None:
        payload["llmOperation"] = step.llm_operation
    return f"event: run.step\ndata: {json.dumps(payload)}\n\n"


def get_run_step_hub(request: Request) -> InMemoryRunStepHub:
    hub = getattr(request.app.state, "run_step_hub", None)
    if not isinstance(hub, InMemoryRunStepHub):
        raise RuntimeError("run_step_hub is not configured on app.state")
    return hub


@api_router.get(
    "/stream",
    summary="Stream live run updates",
    description=(
        "Server-Sent Events stream of visible review runs. "
        "Only `run.updated` is id-addressed and replayed; a reconnect with `Last-Event-ID` "
        "replays those status events. `run.step` has no id and is not replayed."
    ),
    responses={
        200: {"description": "SSE stream", "content": {"text/event-stream": {}}},
        401: {"description": "Unauthorized"},
    },
)
async def stream_run_updates(
    event_hub: Annotated[RunUpdateStream, Depends(get_run_event_hub)],
    step_hub: Annotated[RunStepStream, Depends(get_run_step_hub)],
    repository: Annotated[RunAccessRepository, Depends(get_run_repository)],
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    since = parse_run_event_id(last_event_id)

    async def events() -> AsyncIterator[str]:
        async for item in iter_visible_run_events(
            event_hub,
            step_hub,
            repository,
            since=since,
            expires_at=scope.expires_at,
            now=time.time,
            keepalive_interval=KEEPALIVE_INTERVAL_SECONDS,
            replay_overlap=timedelta(seconds=REPLAY_OVERLAP_SECONDS),
            replay_limit=REPLAY_LIMIT,
        ):
            if isinstance(item, RunChange):
                yield _run_updated_frame(item.run_id, item.status, item.updated_at)
            elif isinstance(item, RunStep):
                yield _run_step_frame(item)
            elif isinstance(item, StreamKeepalive):
                yield ": keepalive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@api_router.get(
    "/runs/{run_id}/comments",
    response_model=list[ReviewCommentDto],
    summary="Get review run comments",
    description="List published review comments and findings for a run.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
    },
)
async def get_run_comments(
    run_id: UUID,
    repository: Annotated[RunCommentsRepository, Depends(get_run_repository)],
) -> list[ReviewCommentDto]:
    comments = await GetRunComments(repository).execute(run_id)
    if comments is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_review_comment_dto(comment) for comment in comments]


@api_router.get(
    "/runs/{run_id}/actions",
    response_model=list[RunActionDto],
    summary="Get review run execution steps",
    description="Trace of model actions and tool calls recorded during a review run.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
    },
)
async def get_run_actions(
    run_id: UUID,
    repository: Annotated[RunActionsRepository, Depends(get_run_repository)],
) -> list[RunActionDto]:
    actions = await GetRunActions(repository).execute(run_id)
    if actions is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_run_action_dto(action) for action in actions]


@api_router.get(
    "/runs/{run_id}/actions/{index}/response",
    summary="Get tool response payload",
    description="Full response body of an action tool call by step index.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run action response not found"},
    },
)
async def get_run_action_response(
    run_id: UUID,
    index: Annotated[int, Path(ge=0)],
    repository: Annotated[RunActionsRepository, Depends(get_run_repository)],
) -> object:
    response = await GetRunActionResponse(repository).execute(run_id, index)
    if response is None:
        raise HTTPException(status_code=404, detail="run action response not found")
    return response.response


@api_router.get(
    "/runs/{run_id}/diff",
    response_model=list[DiffFileDto],
    summary="Get review run diff snapshots",
    description="Diff snapshots captured and reviewed for the target commit.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run not found"},
    },
)
async def get_run_diff(
    run_id: UUID,
    repository: Annotated[RunDiffRepository, Depends(get_run_repository)],
) -> list[DiffFileDto]:
    snapshots = await GetRunDiff(repository).execute(run_id)
    if snapshots is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_diff_file_dto(snapshot) for snapshot in snapshots]


@api_router.get(
    "/runs/{run_id}/files",
    response_model=FileLinesDto,
    summary="Get file content slice",
    description="Slice of lines for a file reviewed in the run.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Run file not found"},
        410: {"description": "File blob cache entry expired"},
        422: {"description": "Invalid parameters"},
    },
)
async def get_run_file_lines(
    run_id: UUID,
    repository: Annotated[RunFileRepository, Depends(get_run_repository)],
    cache: Annotated[BlobCache, Depends(get_file_blob_cache)],
    path: Annotated[str, Query(min_length=1, max_length=1024)],
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
) -> FileLinesDto:
    try:
        page = await GetRunFileLines(repository, cache).execute(run_id, path, offset, limit)
    except FileLinesExpired as error:
        raise HTTPException(status_code=410, detail="file blob cache entry expired") from error
    except FileLinesNotFound as error:
        raise HTTPException(status_code=404, detail="run file not found") from error
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return to_file_lines_dto(page)


@api_router.get(
    "/repos",
    response_model=list[RepositoryDto],
    summary="List repositories",
    description="List repositories accessible in the caller's workspaces.",
    responses={
        401: {"description": "Unauthorized"},
    },
)
async def list_repositories(
    uow_factory: Annotated[RepositorySettingsUowFactory, Depends(get_repository_settings)],
) -> list[RepositoryDto]:
    return [to_repository_dto(item) for item in await ListRepositories(uow_factory).execute()]


@api_router.get(
    "/repos/{repo_id}",
    response_model=RepositoryDto,
    summary="Get repository settings",
    description="Review automation configuration and settings for a repository.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found"},
    },
)
async def get_repository(
    repo_id: UUID,
    uow_factory: Annotated[RepositorySettingsUowFactory, Depends(get_repository_settings)],
) -> RepositoryDto:
    item = await GetRepository(uow_factory).execute(repo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return to_repository_dto(item)


@api_router.patch(
    "/repos/{repo_id}",
    response_model=RepositoryDto,
    summary="Update repository settings",
    description="Update review automation settings for a repository.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found"},
    },
)
async def update_repository(
    repo_id: UUID,
    body: RepositoryUpdateDto,
    uow_factory: Annotated[RepositorySettingsUowFactory, Depends(get_repository_settings)],
) -> RepositoryDto:
    try:
        item = await UpdateRepository(uow_factory).execute(
            repo_id,
            RepositorySettingsChange(
                enabled=body.enabled,
                default_engine=body.default_engine,
                wait_for_ci=body.wait_for_ci,
                max_comments=body.max_comments,
                review_event=body.review_event,
                branch_filter=None if body.branch_filter is None else tuple(body.branch_filter),
                severity_threshold=body.severity_threshold,
                rag_enabled=body.rag_enabled,
            ),
        )
    except UnsupportedRepositorySetting as error:
        raise RequestValidationError(
            [{"loc": ("body", error.field), "msg": str(error), "type": "value_error"}]
        ) from error
    if item is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return to_repository_dto(item)


@api_router.get(
    "/repos/{repo_id}/rules-status",
    response_model=RulesStatusDto,
    summary="Get the rules status of a repository",
    description=(
        "The last read of `.review/rules.md` on the default branch. A GET that finds the "
        "read older than 10 minutes, or failed with a retryable error, requests a new one "
        "and a later GET shows it; `pending` means no read has finished yet."
    ),
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found"},
    },
)
async def get_repository_rules_status(
    repo_id: UUID,
    use_case: Annotated[GetRulesStatus, Depends(get_rules_status)],
) -> RulesStatusDto:
    item = await use_case.execute(repo_id)
    if item is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return to_rules_status_dto(item)


@api_router.get(
    "/repos/{repo_id}/pulls",
    response_model=PullRequestPageDto,
    summary="List repository pull requests",
    description="List synchronized pull requests and their latest review runs for a repository.",
    responses={
        401: {"description": "Unauthorized"},
        404: {"description": "Repository not found"},
        422: {"description": "Invalid query parameters"},
    },
)
async def list_repository_pulls(
    repo_id: UUID,
    repository: Annotated[PullRequestRepository, Depends(get_pull_requests)],
    state: Literal["open", "closed", "all"] = "open",
    cursor: str | None = None,
) -> PullRequestPageDto:
    try:
        page = await ListRepositoryPulls(repository).execute(repo_id, state=state, cursor=cursor)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if page is None:
        raise HTTPException(status_code=404, detail="repository not found")
    return PullRequestPageDto(
        items=[to_pull_request_summary_dto(item) for item in page.items],
        next_cursor=page.next_cursor,
    )


app.include_router(api_router)
app.include_router(bitbucket_connections_router)
app.include_router(bitbucket_webhook_router)
app.include_router(github_webhook_router)
app.include_router(gitlab_webhook_router)
app.include_router(auth_router)
