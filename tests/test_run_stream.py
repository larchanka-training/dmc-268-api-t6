from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

from fastapi.testclient import TestClient

from app.main import app, get_run_event_hub
from app.modules.reviews.application.cancel_run import CancelRequestResult, CancelRun
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated

RUN_ID = UUID("00000000-0000-0000-0000-000000000100")


def make_item(status: str = "running") -> RunListItem:
    created_at = datetime(2026, 9, 25, tzinfo=UTC)
    return RunListItem(
        id=RUN_ID,
        status=status,
        engine="fast",
        attempt=0,
        cancel_requested=status == "running",
        started_at=created_at,
        finished_at=None,
        error_code=None,
        model=None,
        action_count=0,
        repo="org/repo",
        number=1,
        title="PR 1",
        url="https://example.test/1",
        head_sha="a" * 40,
        created_at=created_at,
    )


class CancelRepository:
    def __init__(
        self, item: RunListItem, failure: Exception | None = None, changed: bool = True
    ) -> None:
        self.item = item
        self.failure = failure
        self.changed = changed
        self.committed = False

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        assert run_id == RUN_ID
        if self.failure is not None:
            raise self.failure
        self.committed = True
        return CancelRequestResult(found=True, changed=self.changed)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        assert run_id == RUN_ID
        assert self.committed
        return self.item


def test_hub_delivers_only_to_live_subscribers_and_cleans_up_after_disconnect() -> None:
    hub = InMemoryRunUpdateHub()

    async def receive_one() -> RunUpdated:
        async with hub.subscribe() as events:
            assert hub.subscriber_count == 1
            await hub.publish(RunUpdated(RUN_ID, "cancelled"))
            return await anext(events)

    assert asyncio.run(receive_one()) == RunUpdated(RUN_ID, "cancelled")
    assert hub.subscriber_count == 0


def test_hub_coalesces_stale_events_for_a_slow_subscriber() -> None:
    hub = InMemoryRunUpdateHub(queue_size=1)

    async def receive_latest() -> RunUpdated:
        async with hub.subscribe() as events:
            await hub.publish(RunUpdated(RUN_ID, "running"))
            await hub.publish(RunUpdated(RUN_ID, "cancelled"))
            return await anext(events)

    assert asyncio.run(receive_latest()) == RunUpdated(RUN_ID, "cancelled")
    assert hub.subscriber_count == 0

    async def reconnect() -> RunUpdated:
        await hub.publish(RunUpdated(RUN_ID, "failed"))
        async with hub.subscribe() as events:
            await hub.publish(RunUpdated(RUN_ID, "succeeded"))
            return await anext(events)

    assert asyncio.run(reconnect()) == RunUpdated(RUN_ID, "succeeded")
    assert hub.subscriber_count == 0


def test_cancel_publishes_a_durable_run_update_only_after_repository_commit() -> None:
    hub = InMemoryRunUpdateHub()
    repository = CancelRepository(make_item())

    async def cancel_and_receive() -> tuple[RunListItem | None, RunUpdated]:
        async with hub.subscribe() as events:
            result = await CancelRun(repository, hub).execute(RUN_ID)
            return result, await anext(events)

    result, event = asyncio.run(cancel_and_receive())

    assert result == repository.item
    assert repository.committed is True
    assert event == RunUpdated(RUN_ID, "running")


def test_failed_cancellation_transaction_publishes_nothing() -> None:
    hub = InMemoryRunUpdateHub()
    repository = CancelRepository(make_item(), failure=RuntimeError("rollback"))

    async def cancel_and_assert_empty() -> None:
        async with hub.subscribe() as events:
            try:
                await CancelRun(repository, hub).execute(RUN_ID)
            except RuntimeError as error:
                assert str(error) == "rollback"
            else:
                raise AssertionError("failed transaction must propagate")
            try:
                await asyncio.wait_for(anext(events), timeout=0.01)
            except TimeoutError:
                pass
            else:
                raise AssertionError("failed transaction must not publish an event")

    asyncio.run(cancel_and_assert_empty())


def test_repeated_or_terminal_cancellation_does_not_publish_an_update() -> None:
    hub = InMemoryRunUpdateHub()
    repository = CancelRepository(make_item("cancelled"), changed=False)

    async def cancel_and_assert_empty() -> None:
        async with hub.subscribe() as events:
            result = await CancelRun(repository, hub).execute(RUN_ID)
            assert result == repository.item
            try:
                await asyncio.wait_for(anext(events), timeout=0.01)
            except TimeoutError:
                pass
            else:
                raise AssertionError("an idempotent cancellation must not publish an event")

    asyncio.run(cancel_and_assert_empty())


def test_stream_endpoint_uses_sse_event_and_camel_case_payload() -> None:
    async def single_event() -> AsyncIterator[RunUpdated]:
        yield RunUpdated(RUN_ID, "cancelled")

    class StreamHub:
        def subscribe(self) -> object:
            return _Subscription(single_event())

    class _Subscription:
        def __init__(self, events: AsyncIterator[RunUpdated]) -> None:
            self._events = events

        async def __aenter__(self) -> AsyncIterator[RunUpdated]:
            return self._events

        async def __aexit__(self, *args: object) -> None:
            return None

    app.dependency_overrides[get_run_event_hub] = StreamHub
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.text == (
        "event: run.updated\\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"cancelled"}\\n\\n'
    )
