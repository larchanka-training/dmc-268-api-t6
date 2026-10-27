"""Run worker emits Bitbucket status transitions without GitHub check runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.handle_review_run import HandleReviewRun
from tests.test_review_run_worker import RUN, FakeRun, FakeStore, FakeUow, RetryQueue


def test_worker_emits_running_and_cancelled_status_without_github() -> None:
    async def scenario() -> None:
        run = FakeRun()
        store = FakeStore(run)
        observed: list[str] = []

        class Statuses:
            def __init__(self, active: FakeRun) -> None:
                self.active = active

            async def upsert(self, run_id: UUID, run_url: str | None) -> None:
                assert run_id == RUN
                assert run_url == "https://review.example/runs/one"
                observed.append(self.active.state)

        async def pipeline(_claimed: object) -> bool:
            return True

        worker = HandleReviewRun(
            uow_factory=lambda: FakeUow(store),
            pipeline=pipeline,
            retry_queue=RetryQueue(),
            check_runs=None,
            bitbucket_statuses=Statuses(run),
            worker_id="worker-1",
            run_url=lambda _: "https://review.example/runs/one",
            now=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        )
        await worker.execute(RUN)
        assert observed == ["running"]

        cancelled = FakeRun(cancel_requested=True)
        cancelled_store = FakeStore(cancelled)
        worker = HandleReviewRun(
            uow_factory=lambda: FakeUow(cancelled_store),
            pipeline=pipeline,
            retry_queue=RetryQueue(),
            check_runs=None,
            bitbucket_statuses=Statuses(cancelled),
            worker_id="worker-1",
            run_url=lambda _: "https://review.example/runs/one",
            now=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        )
        await worker.execute(RUN)
        assert observed == ["running", "cancelled"]

    asyncio.run(scenario())
