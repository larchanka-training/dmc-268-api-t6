"""Fail-fast assertions for SQLAlchemy adapters requiring an active Unit of Work."""

from __future__ import annotations

import asyncio
from typing import Any, cast
from uuid import uuid4

import pytest

from app.modules.reviews.application.get_run_file_lines import BlobCacheKey
from app.modules.reviews.application.sweep_no_ci import DueNoCiCandidate
from app.modules.reviews.application.trigger_from_delivery import CiTriggerEvent
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.no_ci_sweep_candidates import (
    SqlAlchemyDueNoCiCandidates,
)
from app.modules.reviews.infrastructure.webhook_run_targets import (
    SqlAlchemyWebhookRunTargets,
)


def test_sqlalchemy_due_no_ci_candidates_exclude_fails_without_session() -> None:
    candidates = SqlAlchemyDueNoCiCandidates(session=None)
    candidate = DueNoCiCandidate(uuid4(), "a" * 40)
    with pytest.raises(
        RuntimeError, match=r"exclude\(\) requires an active session within a Unit of Work"
    ):
        asyncio.run(candidates.exclude(candidate))


def test_sqlalchemy_webhook_run_targets_for_ci_fails_without_session() -> None:
    targets = SqlAlchemyWebhookRunTargets(session=None)
    event = CiTriggerEvent(
        installation_external_id=1,
        repository_external_id=2,
        head_sha="a" * 40,
        event_name="check_suite",
    )
    with pytest.raises(
        RuntimeError, match=r"for_ci\(\) requires an active session within a Unit of Work"
    ):
        asyncio.run(targets.for_ci(event))


def test_sqlalchemy_blob_cache_put_fails_without_session() -> None:
    cache = SqlAlchemyBlobCache(session_or_factory=cast(Any, lambda: None))
    key = BlobCacheKey(uuid4(), "a" * 40)
    with pytest.raises(
        RuntimeError,
        match=r"SqlAlchemyBlobCache\.put requires an active session within a Unit of Work",
    ):
        asyncio.run(cache.put(key, "file content"))
