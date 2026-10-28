"""Worker settings and Bitbucket run routing are explicit and fail closed."""

from __future__ import annotations

import asyncio
import base64
import json
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker as worker_module
from app.bootstrap.bitbucket_reviews import BitbucketAttemptBundle
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RETRYABLE_ERROR_CODES, RunFailure
from app.modules.reviews.infrastructure.run_repository import SqlAlchemyRunRepository
from app.worker import WorkerSettings, compose_worker_process
from tests.test_bitbucket_vcs import BASE, HEAD, TARGET, _locator

INPUT = RunVcsInput(UUID(int=30), TARGET.repository_id, HEAD, BASE, _locator())
CLAIMED = ClaimedAttempt(
    UUID(int=40),
    TARGET.workspace_id,
    1,
    "fast",
    datetime.now(UTC) + timedelta(minutes=1),
    UUID(int=50),
    UUID(int=60),
    "worker",
)
BASE_ENV = {
    "DATABASE_URL": "postgresql+psycopg://localhost/test",
    "RABBITMQ_URL": "amqp://localhost",
}


def test_worker_bitbucket_cipher_opt_in_hides_keys_and_rejects_partial() -> None:
    assert WorkerSettings.from_environment(BASE_ENV).bitbucket_cipher is None
    with pytest.raises(RuntimeError, match="Bitbucket review worker configuration is invalid"):
        WorkerSettings.from_environment({**BASE_ENV, "BITBUCKET_ACTIVE_KEY_ID": "test"})
    settings = WorkerSettings.from_environment(
        {
            **BASE_ENV,
            "BITBUCKET_ACTIVE_KEY_ID": "test",
            "BITBUCKET_CREDENTIAL_KEYS": json.dumps({"test": base64.b64encode(b"k" * 32).decode()}),
        }
    )
    assert settings.bitbucket_cipher is not None
    assert base64.b64encode(b"k" * 32).decode() not in repr(settings)


def test_bitbucket_run_routes_to_bound_resources_without_github_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []
    sentinel_vcs, sentinel_source, sentinel_stage = object(), object(), object()

    async def get_input(self: SqlAlchemyRunRepository, run_id: UUID) -> RunVcsInput:
        assert run_id == CLAIMED.run_id
        return INPUT

    async def process(*args: Any, **kwargs: Any) -> bool:
        assert args[0] == CLAIMED.run_id
        assert args[3] is sentinel_vcs
        assert kwargs["context_stage"] is sentinel_stage
        calls.append("processed")
        return True

    class Resources:
        publications = None
        statuses = object()
        status_reconciler = None

        @asynccontextmanager
        async def attempt(self, **kwargs: Any):  # type: ignore[no-untyped-def]
            assert kwargs["claimed"] == CLAIMED
            assert kwargs["expected_input"] == INPUT
            calls.append("admitted")
            yield BitbucketAttemptBundle(
                cast(Any, sentinel_vcs), cast(Any, sentinel_source), cast(Any, sentinel_stage)
            )

    monkeypatch.setattr(SqlAlchemyRunRepository, "get_run_vcs_input", get_input)
    monkeypatch.setattr(worker_module, "process_review_run", process)
    settings = WorkerSettings.from_environment(BASE_ENV)
    process_root = compose_worker_process(
        settings=settings,
        session_factory=async_sessionmaker[AsyncSession](),
        queue=cast(Any, object()),
        github=None,
        bitbucket=cast(Any, Resources()),
        provider_factory=lambda _: cast(Any, object()),
    )
    assert asyncio.run(process_root.handle_run._pipeline(CLAIMED))
    assert calls == ["admitted", "processed"]
    assert process_root.handle_run._bitbucket_statuses is Resources.statuses
    assert process_root.publish_review._bitbucket_statuses is Resources.statuses


def test_bitbucket_run_without_configured_resources_fails_without_github(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_input(self: SqlAlchemyRunRepository, run_id: UUID) -> RunVcsInput:
        return INPUT

    monkeypatch.setattr(SqlAlchemyRunRepository, "get_run_vcs_input", get_input)
    process_root = compose_worker_process(
        settings=WorkerSettings.from_environment(BASE_ENV),
        session_factory=async_sessionmaker[AsyncSession](),
        queue=cast(Any, object()),
        github=None,
    )
    with pytest.raises(RunFailure, match="Bitbucket review capability is unavailable"):
        asyncio.run(process_root.handle_run._pipeline(CLAIMED))


def test_unsupported_bitbucket_engine_fails_terminally(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def get_input(self: SqlAlchemyRunRepository, run_id: UUID) -> RunVcsInput:
        return INPUT

    monkeypatch.setattr(SqlAlchemyRunRepository, "get_run_vcs_input", get_input)
    process_root = compose_worker_process(
        settings=WorkerSettings.from_environment(BASE_ENV),
        session_factory=async_sessionmaker[AsyncSession](),
        queue=cast(Any, object()),
        github=None,
    )
    with pytest.raises(RunFailure) as error:
        asyncio.run(process_root.handle_run._pipeline(replace(CLAIMED, engine="deep")))
    assert error.value.error_code == "bitbucket_engine_unsupported"
    assert error.value.error_code not in RETRYABLE_ERROR_CODES
