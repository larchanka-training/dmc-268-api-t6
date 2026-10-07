from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from app.main import app, get_run_event_hub, get_run_repository
from app.modules.reviews.application.cancel_run import CancelRequestResult, CancelRun
from app.modules.reviews.application.list_runs import RunListItem
from app.modules.reviews.application.run_events import (
    InMemoryRunUpdateHub,
    RunChange,
    RunUpdated,
    parse_run_event_id,
    run_event_id,
)
from tests.portal_test_client import authenticated_test_client as TestClient

RUN_ID = UUID("00000000-0000-0000-0000-000000000100")
OTHER_RUN_ID = UUID("00000000-0000-0000-0000-000000000101")
# The id keeps the microseconds: 2026-10-07T12:34:56.123457Z since the Unix epoch.
UPDATED_AT = datetime(2026, 10, 7, 12, 34, 56, 123457, tzinfo=UTC)
UPDATED_AT_ID = "1791376496123457"


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


class CommitOrderCheckingHub(InMemoryRunUpdateHub):
    def __init__(self, repository: CancelRepository) -> None:
        super().__init__()
        self.repository = repository
        self.published_while_committed: list[bool] = []

    async def publish(self, event: RunUpdated) -> None:
        self.published_while_committed.append(self.repository.committed)
        await super().publish(event)


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
    repository = CancelRepository(make_item())
    hub = CommitOrderCheckingHub(repository)

    async def cancel_and_receive() -> tuple[RunListItem | None, RunUpdated]:
        async with hub.subscribe() as events:
            result = await CancelRun(event_publisher=hub, uow_factory=lambda: repository).execute(
                RUN_ID
            )
            return result, await anext(events)

    result, event = asyncio.run(cancel_and_receive())

    assert result == repository.item
    assert repository.committed is True
    assert hub.published_while_committed == [True]
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
        async def run_updated_at(self, run_id: UUID) -> datetime | None:
            return UPDATED_AT if run_id == RUN_ID else None

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
            f"id: {UPDATED_AT_ID}\n"
            "event: run.updated\n"
            'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"cancelled"}'
        ),
        "",
    ]
    id_line, event_line, data_line = frames[0].split("\n")
    assert id_line == f"id: {UPDATED_AT_ID}"
    assert event_line == "event: run.updated"
    assert data_line.startswith("data: ")
    assert json.loads(data_line.removeprefix("data: ")) == {
        "runId": str(RUN_ID),
        "status": "cancelled",
    }


def test_stream_uses_run_updated_at_for_access() -> None:
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
        async def run_updated_at(self, run_id: UUID) -> datetime | None:
            calls.append(run_id)
            return UPDATED_AT

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = LightRepository
    try:
        response = TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    expected_data = 'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"succeeded"}'
    assert expected_data in response.text
    assert calls == [RUN_ID]


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
        async def run_updated_at(self, run_id: UUID) -> datetime | None:
            return UPDATED_AT

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


def test_stream_fails_closed_if_repository_lacks_run_updated_at() -> None:
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
        with pytest.raises(AttributeError, match="does not implement run_updated_at"):
            TestClient(app).get("/api/stream")
    finally:
        app.dependency_overrides.clear()


def test_stream_drops_events_when_run_updated_at_is_none() -> None:
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
        async def run_updated_at(self, run_id: UUID) -> datetime | None:
            return None

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
        async def run_updated_at(self, run_id: UUID) -> datetime | None:
            return UPDATED_AT

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


class ReplayRepository:
    """Every run is visible with `UPDATED_AT`; records the replay query."""

    def __init__(
        self, changes: list[RunChange] | None = None, order: list[str] | None = None
    ) -> None:
        self.changes = changes or []
        self.order = order if order is not None else []
        self.replay_calls: list[tuple[datetime, int]] = []

    async def run_updated_at(self, run_id: UUID) -> datetime | None:
        return UPDATED_AT

    async def runs_updated_after(self, after: datetime, limit: int) -> list[RunChange]:
        self.order.append("replay")
        self.replay_calls.append((after, limit))
        return self.changes


def _stream(
    repository: object,
    live: list[RunUpdated] | None = None,
    headers: dict[str, str] | None = None,
    order: list[str] | None = None,
) -> str:
    async def events() -> AsyncIterator[RunUpdated]:
        for update in live or []:
            yield update

    class StreamHub:
        def subscribe(self) -> object:
            class _Sub:
                async def __aenter__(self) -> AsyncIterator[RunUpdated]:
                    if order is not None:
                        order.append("subscribe")
                    return events()

                async def __aexit__(self, *args: object) -> None:
                    pass

            return _Sub()

    app.dependency_overrides[get_run_event_hub] = StreamHub
    app.dependency_overrides[get_run_repository] = lambda: repository
    try:
        response = TestClient(app).get("/api/stream", headers=headers)
    finally:
        app.dependency_overrides.clear()
    assert response.status_code == 200
    body: str = response.text
    return body


def test_stream_live_event_id_is_the_updated_at_in_epoch_microseconds() -> None:
    text = _stream(ReplayRepository(), live=[RunUpdated(RUN_ID, "running")])

    assert text == (
        f"id: {UPDATED_AT_ID}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"running"}\n\n'
    )


def test_event_id_is_integer_microseconds_since_the_epoch_and_round_trips() -> None:
    assert run_event_id(datetime(1970, 1, 1, tzinfo=UTC)) == "0"
    assert run_event_id(datetime(1970, 1, 1, 0, 0, 0, 1, tzinfo=UTC)) == "1"
    assert run_event_id(UPDATED_AT) == UPDATED_AT_ID
    assert parse_run_event_id(UPDATED_AT_ID) == UPDATED_AT


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "abc",
        "-5",
        "+5",
        "12.5",
        " 12",
        "12 ",
        "1_000",
        "\u0661\u0662",
        "9" * 30,
        "1\n",
        # Plain decimals the regex accepts but the datetime range does not: year 10000 onwards.
        "9" * 19,
        "253402300800000000",
    ],
)
def test_parse_event_id_rejects_anything_but_plain_decimal_microseconds(value: str | None) -> None:
    assert parse_run_event_id(value) is None


def test_stream_replays_changed_runs_in_order_before_live_events() -> None:
    first = RunChange(RUN_ID, "succeeded", UPDATED_AT - timedelta(seconds=10))
    second = RunChange(OTHER_RUN_ID, "running", UPDATED_AT + timedelta(seconds=5))
    repository = ReplayRepository([first, second])

    text = _stream(
        repository,
        live=[RunUpdated(RUN_ID, "failed")],
        headers={"Last-Event-ID": UPDATED_AT_ID},
    )

    assert text == (
        f"id: {run_event_id(first.updated_at)}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"succeeded"}\n\n'
        f"id: {run_event_id(second.updated_at)}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000101","status":"running"}\n\n'
        f"id: {UPDATED_AT_ID}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"failed"}\n\n'
    )


def test_stream_replay_starts_30_seconds_before_the_last_event_id() -> None:
    import app.main as main_mod

    repository = ReplayRepository()

    _stream(repository, headers={"Last-Event-ID": UPDATED_AT_ID})

    assert repository.replay_calls == [(UPDATED_AT - timedelta(seconds=30), main_mod.REPLAY_LIMIT)]


def test_stream_replay_is_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.main as main_mod

    assert main_mod.REPLAY_LIMIT == 500
    monkeypatch.setattr(main_mod, "REPLAY_LIMIT", 3)
    repository = ReplayRepository()

    _stream(repository, headers={"Last-Event-ID": UPDATED_AT_ID})

    assert [limit for _, limit in repository.replay_calls] == [3]


def test_stream_subscribes_before_it_replays() -> None:
    order: list[str] = []
    repository = ReplayRepository(order=order)

    _stream(repository, headers={"Last-Event-ID": UPDATED_AT_ID}, order=order)

    assert order == ["subscribe", "replay"]


def test_a_change_published_while_the_replay_query_runs_is_delivered_live() -> None:
    from app.bootstrap.portal_auth import get_auth_scope
    from app.modules.auth.application.scope import AuthScope

    class FirstEventHub(InMemoryRunUpdateHub):
        """The real hub, cut after its first event so the stream ends on its own."""

        @asynccontextmanager
        async def subscribe(self) -> AsyncIterator[AsyncIterator[RunUpdated]]:
            async with super().subscribe() as updates:

                async def first() -> AsyncIterator[RunUpdated]:
                    yield await anext(updates)

                yield first()

    hub = FirstEventHub()

    class CommitDuringReplayRepository(ReplayRepository):
        async def runs_updated_after(self, after: datetime, limit: int) -> list[RunChange]:
            await hub.publish(RunUpdated(RUN_ID, "running"))  # the change commits mid-query
            return await super().runs_updated_after(after, limit)

    repository = CommitDuringReplayRepository()
    app.dependency_overrides[get_run_event_hub] = lambda: hub
    app.dependency_overrides[get_run_repository] = lambda: repository
    # A safety net only: a stream that never gets the event ends at the token expiry, and fails.
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(
        user_id=42, workspace_ids=(), expires_at=int(time.time()) + 5
    )
    try:
        response = TestClient(app).get("/api/stream", headers={"Last-Event-ID": UPDATED_AT_ID})
    finally:
        app.dependency_overrides.clear()

    assert repository.replay_calls != []
    assert response.text == (
        f"id: {UPDATED_AT_ID}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"running"}\n\n'
    )


@pytest.mark.parametrize(
    "value", ["", "abc", "-5", "12.5", "1_000", "9" * 30, "9" * 19, "253402300800000000"]
)
def test_stream_with_an_invalid_last_event_id_does_not_replay(value: str) -> None:
    repository = ReplayRepository([RunChange(RUN_ID, "succeeded", UPDATED_AT)])

    text = _stream(
        repository, live=[RunUpdated(RUN_ID, "running")], headers={"Last-Event-ID": value}
    )

    assert repository.replay_calls == []
    assert text == (
        f"id: {UPDATED_AT_ID}\n"
        "event: run.updated\n"
        'data: {"runId":"00000000-0000-0000-0000-000000000100","status":"running"}\n\n'
    )


def test_stream_without_last_event_id_does_not_replay() -> None:
    repository = ReplayRepository([RunChange(RUN_ID, "succeeded", UPDATED_AT)])

    text = _stream(repository, live=[RunUpdated(RUN_ID, "running")])

    assert repository.replay_calls == []
    assert text.count("event: run.updated") == 1


def test_stream_with_an_expired_token_does_not_replay() -> None:
    from app.bootstrap.portal_auth import get_auth_scope
    from app.modules.auth.application.scope import AuthScope

    repository = ReplayRepository([RunChange(RUN_ID, "succeeded", UPDATED_AT)])
    app.dependency_overrides[get_auth_scope] = lambda: AuthScope(
        user_id=42, workspace_ids=(), expires_at=1
    )

    text = _stream(repository, headers={"Last-Event-ID": UPDATED_AT_ID})

    assert text == ""
    assert repository.replay_calls == []
