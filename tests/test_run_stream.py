from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import UUID

import pytest

from app.main import app, get_run_event_hub, get_run_repository
from app.modules.reviews.application.cancel_run import CancelRequestResult, CancelRun
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.run_events import InMemoryRunUpdateHub, RunUpdated
from tests.portal_test_client import authenticated_test_client as TestClient

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

    @property
    def repository(self) -> CancelRepository:
        return self

    async def __aenter__(self) -> CancelRepository:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        pass

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        pass

    async def request_cancel(self, run_id: UUID) -> CancelRequestResult:
        assert run_id == RUN_ID
        if self.failure is not None:
            raise self.failure
        return CancelRequestResult(found=True, changed=self.changed)

    async def get_run(self, run_id: UUID) -> RunListItem | None:
        assert run_id == RUN_ID
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
    hub = InMemoryRunUpdateHub()

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
            result = await CancelRun(event_publisher=hub, uow_factory=lambda: repository).execute(
                RUN_ID
            )
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
                await CancelRun(event_publisher=hub, uow_factory=lambda: repository).execute(RUN_ID)
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
            result = await CancelRun(event_publisher=hub, uow_factory=lambda: repository).execute(
                RUN_ID
            )
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
        yield RunUpdated(UUID("00000000-0000-0000-0000-000000000101"), "running")
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

    class AuthorizedRunRepository:
        async def has_run_access(self, run_id: UUID) -> bool:
            return run_id == RUN_ID

        async def get_run(self, run_id: UUID) -> RunListItem | None:
            return make_item() if run_id == RUN_ID else None

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = AuthorizedRunRepository
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = response.text.split("\n\n")
    assert frames == [
        (
            "event: run.updated\n"
            'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"cancelled"}'
        ),
        "",
    ]
    event_line, data_line = frames[0].split("\n")
    assert event_line == "event: run.updated"
    assert data_line.startswith("data: ")
    assert json.loads(data_line.removeprefix("data: ")) == {
        "runId": str(RUN_ID),
        "status": "cancelled",
    }


def test_stream_uses_has_run_access() -> None:
    calls: list[UUID] = []

    async def single_event() -> AsyncIterator[RunUpdated]:
        yield RunUpdated(RUN_ID, "succeeded")

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    return single_event()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    class LightRepository:
        async def has_run_access(self, run_id: UUID) -> bool:
            calls.append(run_id)
            return True

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = LightRepository
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    expected_data = 'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"succeeded"}'
    assert expected_data in response.text


def test_stream_emits_keepalive_when_idle(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    # Set very short timeout for test
    monkeypatch.setattr(main_mod, "KEEPALIVE_INTERVAL_SECONDS", 0.01)

    async def delayed_event() -> AsyncIterator[RunUpdated]:
        # Sleep to trigger at least one keepalive before event
        await asyncio.sleep(0.03)
        yield RunUpdated(RUN_ID, "running")

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    return delayed_event()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    class LightRepository:
        async def has_run_access(self, run_id: UUID) -> bool:
            return True

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = LightRepository
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert ": keepalive\n\n" in response.text
    expected_data = 'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"running"}'
    assert expected_data in response.text


def test_stream_fails_closed_if_repository_lacks_has_run_access() -> None:
    async def single_event() -> AsyncIterator[RunUpdated]:
        yield RunUpdated(RUN_ID, "running")

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    return single_event()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    class RepositoryWithoutAccessCheck:
        pass

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = RepositoryWithoutAccessCheck
    try:
        with pytest.raises(AttributeError, match="does not implement has_run_access"):
            TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()


def test_stream_drops_events_when_has_run_access_returns_false() -> None:
    async def single_event() -> AsyncIterator[RunUpdated]:
        yield RunUpdated(RUN_ID, "running")

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    return single_event()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    class DeniedRepository:
        async def has_run_access(self, run_id: UUID) -> bool:
            return False

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = DeniedRepository
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.text == ""


def test_stream_terminates_cleanly_when_jwt_expires() -> None:
    from app.bootstrap.portal_auth import get_auth_scope
    from app.modules.auth.application.scope import AuthScope

    # Expired token scope
    expired_scope = AuthScope(user_id=42, workspace_ids=(), expires_at=1)

    async def infinite_events() -> AsyncIterator[RunUpdated]:
        while True:
            await asyncio.sleep(0.1)
            yield RunUpdated(RUN_ID, "running")

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    return infinite_events()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    class LightRepository:
        async def has_run_access(self, run_id: UUID) -> bool:
            return True

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = LightRepository
    app.dependency_overrides[get_auth_scope] = lambda: expired_scope
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    # Stream terminates immediately without consuming infinite events
    assert response.text == ""
