"""Test that worker wiring connects SubprocessSourceParser to both review publishers (#444)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import worker
from app.modules.reviews.application.handle_review_run import ClaimedAttempt
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser


def test_compose_worker_process_wires_subprocess_source_parser(monkeypatch: Any) -> None:
    captured: dict[str, object] = {}

    class Runs:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def get_run_vcs_input(self, run_id: UUID) -> object:
            return SimpleNamespace(provider="github", locator=object())

    class RecordingStore:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["parser"] = kwargs.get("parser")

    async def stop_after_store(*args: object, **kwargs: object) -> bool:
        return True

    monkeypatch.setattr(worker, "SqlAlchemyRunRepository", Runs)
    monkeypatch.setattr(worker, "StoreReviewOutput", RecordingStore)
    monkeypatch.setattr(worker, "process_review_run", stop_after_store)

    settings = worker.WorkerSettings(
        database_url="unused",
        rabbitmq_url="unused",
        worker_id="worker-1",
        github_app_id="1",
        github_private_key="unused",
        github_api_url="https://api.github.test",
        portal_url="https://portal.test/",
    )
    github = worker.GitHubAdapters(
        vcs=cast(Any, object()),
        check_runs=cast(Any, object()),
        reviews=cast(Any, object()),
        eligibility=cast(Any, object()),
        run_source=lambda _run_id: cast(Any, None),
    )
    process = worker.compose_worker_process(
        settings=settings,
        session_factory=cast(async_sessionmaker[AsyncSession], object()),
        queue=cast(Any, object()),
        github=github,
        provider_factory=lambda _claimed: cast(Any, object()),
    )
    claimed = ClaimedAttempt(
        run_id=UUID("11111111-2222-3333-4444-555555555555"),
        workspace_id=UUID("00000000-0000-0000-0000-000000000001"),
        attempt=1,
        engine="fast",
        deadline=datetime(2026, 10, 25, tzinfo=UTC),
        prompt_version_id=UUID("00000000-0000-0000-0000-000000000002"),
        rule_version_id=UUID("00000000-0000-0000-0000-000000000003"),
        worker_id="worker-1",
    )

    assert asyncio.run(process.handle_run._pipeline(claimed)) is True
    assert isinstance(captured.get("parser"), SubprocessSourceParser)


def test_process_review_run_inline_path_wires_subprocess_source_parser(
    monkeypatch: Any,
) -> None:
    captured: dict[str, object] = {}

    class RecordingPublishReviewOutput:
        def __init__(self, *args: object, **kwargs: object) -> None:
            captured["parser"] = kwargs.get("parser")

    class RecordingExecuteReviewRun:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        async def execute(self, run_id: UUID) -> bool:
            return True

    monkeypatch.setattr(worker, "PublishReviewOutput", RecordingPublishReviewOutput)
    monkeypatch.setattr(worker, "ExecuteReviewRun", RecordingExecuteReviewRun)

    result = asyncio.run(
        worker.process_review_run(
            run_id=UUID("22222222-3333-4444-5555-666666666666"),
            provider=cast(Any, object()),
            session_factory=cast(async_sessionmaker[AsyncSession], object()),
            vcs_provider=cast(Any, object()),
        )
    )
    assert result is True
    assert isinstance(captured.get("parser"), SubprocessSourceParser)
