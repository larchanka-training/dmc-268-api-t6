from __future__ import annotations

import os
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Path, Query
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.common.infrastructure.db.enums import RunState
from app.common.infrastructure.db.session import create_session_factory
from app.modules.reviews.api.dtos import (
    PullRequestDto,
    ReviewCommentDto,
    RunActionDto,
    RunListDto,
    RunSessionDto,
)
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
from app.modules.reviews.application.list_runs import ListRuns, RunListItem, RunRepository
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository

api_router = APIRouter(prefix="/api")

app = FastAPI(title="Backend")


@app.get("/healthcheck")
async def healthcheck() -> dict[str, str]:
    return {"status": "ok"}


@lru_cache
def session_factory_for(database_url: str) -> async_sessionmaker[AsyncSession]:
    return create_session_factory(database_url)


def get_run_repository() -> (
    RunRepository | RunDetailRepository | RunCommentsRepository | RunActionsRepository
):
    database_url = os.environ.get("DATABASE_URL")
    if database_url is None:
        raise RuntimeError("DATABASE_URL must be configured to list runs")
    return SqlAlchemyRunRepository(session_factory_for(database_url))


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
        path=item.path,
        old_line=item.old_line,
        new_line=item.new_line,
        severity=item.severity,
        category=item.category,
        confidence=float(item.confidence),
        title=item.title,
        body=item.body,
        suggestion=item.suggestion,
        rule_name=item.rule_name,
    )


def to_run_action_dto(item: RunActionTrace) -> RunActionDto:
    return RunActionDto(
        index=item.index,
        tool=item.tool,
        request=item.request,
        response=item.response,
        response_ref=item.response_ref,
        started_at=item.started_at,
        duration_ms=item.duration_ms,
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


app.include_router(api_router)
