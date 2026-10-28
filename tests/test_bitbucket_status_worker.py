"""Run worker emits Bitbucket status transitions without GitHub check runs."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import UUID

from app.modules.reviews.application.bitbucket_status import bitbucket_status_payload
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

        attempted_cancel = FakeRun(attempt=1, cancel_requested=True)
        worker = HandleReviewRun(
            uow_factory=lambda: FakeUow(FakeStore(attempted_cancel)),
            pipeline=pipeline,
            retry_queue=RetryQueue(),
            check_runs=None,
            bitbucket_statuses=Statuses(attempted_cancel),
            worker_id="worker-1",
            run_url=lambda _: "https://review.example/runs/one",
            now=lambda: datetime(2026, 10, 27, tzinfo=UTC),
        )
        await worker.execute(RUN)
        assert attempted_cancel.attempt == 1
        assert observed == ["running", "cancelled", "cancelled"]
        assert (
            bitbucket_status_payload(
                workspace_id=UUID(int=1),
                repository_uuid=UUID(int=2),
                pr_number=7,
                head_sha="a" * 40,
                source_branch="feature/review",
                run_url="https://review.example/runs/one",
                state=observed[-1],
            )["state"]
            == "STOPPED"
        )

    asyncio.run(scenario())
