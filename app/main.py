from __future__ import annotations

import hashlib
import hmac
import json
import math
from collections.abc import AsyncIterator, Callable
from typing import Annotated, NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import (
    get_file_blob_cache,
    get_github_webhook_receipt_uow_factory,
    get_run_repository,
    reviews_api_lifespan,
)
from app.common.infrastructure.db.enums import RunState
from app.modules.auth.api.router import auth_router
from app.modules.integrations.webhooks.api.dtos import GitHubWebhookPayloadDto
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    GitHubWebhookReceiptUnitOfWork,
    ReceiveGitHubDelivery,
)
from app.modules.reviews.api.dtos import (
    DiffFileDto,
    FileLinesDto,
    PullRequestDto,
    ReviewCommentDto,
    RunActionDto,
    RunListDto,
    RunSessionDto,
)
from app.modules.reviews.application.cancel_run import CancelRun, CancelRunRepository
from app.modules.reviews.application.get_run import GetRun, RunDetailRepository
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
from app.modules.reviews.application.list_runs import ListRuns, RunListItem, RunRepository
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdateStream

api_router = APIRouter(prefix="/api", dependencies=[Depends(get_auth_scope)])
github_webhook_router = APIRouter(prefix="/webhooks/github")

app = FastAPI(title="Backend", lifespan=reviews_api_lifespan)
run_update_hub = InMemoryRunUpdateHub()

__all__ = ["app", "get_file_blob_cache", "get_run_repository"]


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


def get_run_event_hub() -> InMemoryRunUpdateHub:
    return run_update_hub


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


@github_webhook_router.post("")
async def receive_github_webhook(
    request: Request,
    secret: Annotated[str, Depends(get_github_webhook_secret)],
    receipt_uow_factory: Annotated[
        Callable[[], GitHubWebhookReceiptUnitOfWork],
        Depends(get_github_webhook_receipt_uow_factory),
    ],
) -> JSONResponse:
    """Verify and persist a GitHub delivery before acknowledging it."""
    raw_body = await request.body()
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
        status=item.status,
        engine=item.engine,
        attempt=item.attempt,
        cancel_requested=item.cancel_requested,
        started_at=item.started_at,
        finished_at=item.finished_at,
        error_code=item.error_code,
        model=item.model,
        action_count=item.action_count,
        summary_only=item.summary_only,
        pull_request=PullRequestDto(
            repo=item.repo,
            number=item.number,
            title=item.title,
            url=item.url,
            head_sha=item.head_sha,
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


@api_router.get("/runs", response_model=RunListDto)
async def list_runs(
    repository: Annotated[RunRepository, Depends(get_run_repository)],
    status: RunState | None = None,
    repo: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
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


@api_router.get("/runs/{run_id}", response_model=RunSessionDto)
async def get_run(
    run_id: UUID,
    repository: Annotated[RunDetailRepository, Depends(get_run_repository)],
) -> RunSessionDto:
    item = await GetRun(repository).execute(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="run not found")
    return to_run_session_dto(item)


@api_router.post("/runs/{run_id}/cancel", response_model=RunSessionDto)
async def cancel_run(
    run_id: UUID,
    repository: Annotated[CancelRunRepository, Depends(get_run_repository)],
    event_hub: Annotated[InMemoryRunUpdateHub, Depends(get_run_event_hub)],
) -> RunSessionDto:
    item = await CancelRun(repository, event_hub).execute(run_id)
    if item is None:
        raise HTTPException(status_code=404, detail="run not found")
    return to_run_session_dto(item)


@api_router.get("/stream")
async def stream_run_updates(
    event_hub: Annotated[RunUpdateStream, Depends(get_run_event_hub)],
    repository: Annotated[RunDetailRepository, Depends(get_run_repository)],
) -> StreamingResponse:
    async def events() -> AsyncIterator[str]:
        async with event_hub.subscribe() as updates:
            async for update in updates:
                if await repository.get_run(update.run_id) is None:
                    continue
                yield (
                    "event: run.updated\n"
                    f'data: {{"runId":"{update.run_id}","status":"{update.status}"}}\n\n'
                )

    return StreamingResponse(events(), media_type="text/event-stream")


@api_router.get("/runs/{run_id}/comments", response_model=list[ReviewCommentDto])
async def get_run_comments(
    run_id: UUID,
    repository: Annotated[RunCommentsRepository, Depends(get_run_repository)],
) -> list[ReviewCommentDto]:
    comments = await GetRunComments(repository).execute(run_id)
    if comments is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_review_comment_dto(comment) for comment in comments]


@api_router.get("/runs/{run_id}/actions", response_model=list[RunActionDto])
async def get_run_actions(
    run_id: UUID,
    repository: Annotated[RunActionsRepository, Depends(get_run_repository)],
) -> list[RunActionDto]:
    actions = await GetRunActions(repository).execute(run_id)
    if actions is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_run_action_dto(action) for action in actions]


@api_router.get("/runs/{run_id}/actions/{index}/response")
async def get_run_action_response(
    run_id: UUID,
    index: Annotated[int, Path(ge=0)],
    repository: Annotated[RunActionsRepository, Depends(get_run_repository)],
) -> object:
    response = await GetRunActionResponse(repository).execute(run_id, index)
    if response is None:
        raise HTTPException(status_code=404, detail="run action response not found")
    return response.response


@api_router.get("/runs/{run_id}/diff", response_model=list[DiffFileDto])
async def get_run_diff(
    run_id: UUID,
    repository: Annotated[RunDiffRepository, Depends(get_run_repository)],
) -> list[DiffFileDto]:
    snapshots = await GetRunDiff(repository).execute(run_id)
    if snapshots is None:
        raise HTTPException(status_code=404, detail="run not found")
    return [to_diff_file_dto(snapshot) for snapshot in snapshots]


@api_router.get("/runs/{run_id}/files", response_model=FileLinesDto)
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


app.include_router(api_router)
app.include_router(github_webhook_router)
app.include_router(auth_router)
