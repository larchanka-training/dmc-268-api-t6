"""Library families behind a failed webhook delivery, for the worker's failure line."""

from __future__ import annotations

import httpx
import psycopg
from sqlalchemy.exc import SQLAlchemyError

from app.modules.integrations.webhooks.application.receive_github_delivery import FailureCategory


def classify_failure(exc: BaseException) -> FailureCategory:
    """The library family of ``exc`` (docs/WEBHOOK_WORKER.md, failure log).

    ``httpx.HTTPError`` covers transport errors, request timeouts and ``HTTPStatusError``;
    httpx maps its transport's errors to its own, so they need no case of their own.
    """
    if isinstance(exc, httpx.HTTPError):
        return FailureCategory.GITHUB_REQUEST
    if isinstance(exc, SQLAlchemyError | psycopg.Error):
        return FailureCategory.DATABASE
    return FailureCategory.INTERNAL
