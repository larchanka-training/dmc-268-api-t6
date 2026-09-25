"""Composition root for the reviews HTTP API's database dependencies."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import cast

from fastapi import FastAPI, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.modules.reviews.application.cancel_run import CancelRunRepository
from app.modules.reviews.application.get_run import RunDetailRepository
from app.modules.reviews.application.get_run_actions import RunActionsRepository
from app.modules.reviews.application.get_run_comments import RunCommentsRepository
from app.modules.reviews.application.get_run_diff import RunDiffRepository
from app.modules.reviews.application.get_run_file_lines import BlobCache, RunFileRepository
from app.modules.reviews.application.list_runs import RunRepository
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository


class ReviewsApiResources:
    """Long-lived database resources shared by all HTTP requests."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    @classmethod
    def from_database_url(cls, database_url: str) -> ReviewsApiResources:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        return cls(engine, async_sessionmaker(engine, expire_on_commit=False))

    def run_repository(
        self,
    ) -> (
        RunRepository
        | RunDetailRepository
        | RunCommentsRepository
        | RunActionsRepository
        | RunDiffRepository
        | RunFileRepository
        | CancelRunRepository
    ):
        return SqlAlchemyRunRepository(self._session_factory)

    def file_blob_cache(self) -> BlobCache:
        return SqlAlchemyBlobCache(self._session_factory)

    async def aclose(self) -> None:
        await self._engine.dispose()


def _resources(request: Request) -> ReviewsApiResources:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if resources is None:
        raise RuntimeError("DATABASE_URL must be configured to serve review runs")
    return cast(ReviewsApiResources, resources)


def get_run_repository(
    request: Request,
) -> (
    RunRepository
    | RunDetailRepository
    | RunCommentsRepository
    | RunActionsRepository
    | RunDiffRepository
    | RunFileRepository
    | CancelRunRepository
):
    """Provide a request-scoped repository backed by the application pool."""
    return _resources(request).run_repository()


def get_file_blob_cache(request: Request) -> BlobCache:
    """Provide the file cache backed by the application pool."""
    return _resources(request).file_blob_cache()


@asynccontextmanager
async def reviews_api_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create one pool per API process and always dispose it at shutdown."""
    database_url = os.environ.get("DATABASE_URL")
    resources: ReviewsApiResources | None = None
    if database_url is not None:
        resources = ReviewsApiResources.from_database_url(database_url)
        app.state.reviews_api_resources = resources
    try:
        yield
    finally:
        if resources is not None:
            await resources.aclose()
            del app.state.reviews_api_resources
