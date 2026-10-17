"""`index.repo` topology, wake-ups and the outcome → queue action table (AC1, #142).

One test per row of the plan's table, against fake exchanges: the main `code_index`
exchange and `code_index.retry` are separate fakes, and one ordered event log across
publish and ack proves that a copy is confirmed before the original is acked.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel, AbstractExchange, AbstractIncomingMessage

import app.modules.code_index.infrastructure.amqp as index_amqp
import app.modules.reviews.infrastructure.amqp as review_amqp
from app.modules.code_index.application.build_index import BuildOutcome, BuildStatus
from app.modules.reviews.application.run_failures import RetryDelays

# SD §7.2: uuid5 of `repository_id:generation:requested_at` in this namespace.
NAMESPACE = UUID("e03f49a6-717b-4702-886d-5b6805fe7d1a")
REQUESTED_AT = "2026-10-16T10:00:00.000000Z"
EXAMPLE = Path(__file__).resolve().parents[1] / "contracts/examples/index.repo.v1.json"


@pytest.fixture(autouse=True)
def _no_requeue_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.0)


@dataclass
class FakeExchange:
    name: str
    events: list[tuple[str, ...]]
    published: list[tuple[aio_pika.Message, str]] = field(default_factory=list)
    fail: bool = False

    async def publish(self, message: aio_pika.Message, routing_key: str = "") -> None:
        if self.fail:
            raise ConnectionError("broker refused the publish")
        self.published.append((message, routing_key))
        self.events.append(("confirmed", self.name, routing_key))


@dataclass
class Delivery:
    body: bytes
    events: list[tuple[str, ...]]
    message_id: str | None = None
    redelivered: bool = False
    headers: dict[str, Any] = field(default_factory=dict)
    routing_key: str = "index.repo"
    priority: int = 0
    acked: bool = False
    nacked: list[bool] = field(default_factory=list)

    async def ack(self) -> None:
        self.acked = True
        self.events.append(("ack",))

    async def nack(self, requeue: bool = True) -> None:
        self.nacked.append(requeue)
        self.events.append(("nack", str(requeue)))


@dataclass
class Queue:
    events: list[tuple[str, ...]] = field(default_factory=list)
    calls: list[tuple[UUID, int]] = field(default_factory=list)
    lease_waits: int = 2

    def __post_init__(self) -> None:
        self.main = FakeExchange("code_index", self.events)
        self.retry = FakeExchange("code_index.retry", self.events)
        self.publisher = index_amqp.IndexQueuePublisher(
            cast(AbstractExchange, self.main),
            cast(AbstractExchange, self.retry),
            RetryDelays(),
            lease_waits=self.lease_waits,
        )

    def deliver(
        self, delivery: Delivery, outcome: BuildOutcome | BaseException
    ) -> tuple[Delivery, list[tuple[UUID, int]]]:
        async def handler(repository_id: UUID, generation: int) -> BuildOutcome:
            self.calls.append((repository_id, generation))
            if isinstance(outcome, BaseException):
                raise outcome
            return outcome

        asyncio.run(
            index_amqp.handle_index_delivery(
                cast(AbstractIncomingMessage, delivery), handler, self.publisher
            )
        )
        return delivery, self.calls


def body(
    *,
    repository_id: UUID | None = None,
    generation: int = 3,
    attempt: int = 1,
    requested_at: str = REQUESTED_AT,
    **change: Any,
) -> dict[str, Any]:
    repository = repository_id or uuid4()
    message = {
        "schema": "index.repo/v1",
        "message_id": str(uuid5(NAMESPACE, f"{repository}:{generation}:{requested_at}")),
        "repository_id": str(repository),
        "generation": generation,
        "attempt": attempt,
        "requested_at": requested_at,
    }
    message.update(change)
    return message


def delivery(queue: Queue, message: dict[str, Any], **change: Any) -> Delivery:
    return Delivery(
        json.dumps(message).encode(), queue.events, message_id=message.get("message_id"), **change
    )


def wire(message: aio_pika.Message) -> dict[str, Any]:
    decoded = json.loads(message.body)
    assert isinstance(decoded, dict)
    return decoded


# --- topology -------------------------------------------------------------------


@dataclass
class FakeQueue:
    name: str
    binds: list[tuple[str, str, str | None]]

    async def bind(self, exchange: Any, routing_key: str | None = None) -> None:
        self.binds.append((self.name, exchange.name, routing_key))


@dataclass
class DeclaredExchange:
    name: str


@dataclass
class FakeChannel:
    exchanges: list[tuple[str, str, bool]] = field(default_factory=list)
    queues: dict[str, tuple[bool, dict[str, Any]]] = field(default_factory=dict)
    binds: list[tuple[str, str, str | None]] = field(default_factory=list)

    async def declare_exchange(
        self, name: str, type: aio_pika.ExchangeType, *, durable: bool
    ) -> DeclaredExchange:
        self.exchanges.append((name, type.value, durable))
        return DeclaredExchange(name)

    async def declare_queue(
        self, name: str, *, durable: bool, arguments: dict[str, Any]
    ) -> FakeQueue:
        self.queues[name] = (durable, arguments)
        return FakeQueue(name, self.binds)


def test_the_topology_is_the_plan_table() -> None:
    channel = FakeChannel()

    asyncio.run(index_amqp.declare_topology(cast(AbstractChannel, channel), RetryDelays()))

    assert sorted(channel.exchanges) == [
        ("code_index", "direct", True),
        ("code_index.dlx", "fanout", True),
        ("code_index.retry", "direct", True),
    ]
    retry = {"x-dead-letter-exchange": "code_index", "x-dead-letter-routing-key": "index.repo"}
    assert channel.queues == {
        "code_index.dlq": (True, {"x-message-ttl": 604_800_000}),
        "index.repo": (True, {"x-dead-letter-exchange": "code_index.dlx"}),
        "index.repo.retry.30s": (True, {"x-message-ttl": 30_000, **retry}),
        "index.repo.retry.2m": (True, {"x-message-ttl": 120_000, **retry}),
        "index.repo.retry.10m": (True, {"x-message-ttl": 600_000, **retry}),
    }
    assert sorted(channel.binds, key=str) == sorted(
        [
            ("code_index.dlq", "code_index.dlx", None),
            ("index.repo", "code_index", "index.repo"),
            ("index.repo.retry.30s", "code_index.retry", "index.repo.retry.30s"),
            ("index.repo.retry.2m", "code_index.retry", "index.repo.retry.2m"),
            ("index.repo.retry.10m", "code_index.retry", "index.repo.retry.10m"),
        ],
        key=str,
    )


# --- wake-ups ---------------------------------------------------------------------


def test_a_wake_up_is_attempt_1_with_the_derived_message_id_on_index_repo() -> None:
    events: list[tuple[str, ...]] = []
    exchange = FakeExchange("code_index", events)
    wakeups = index_amqp.AmqpIndexWakeups(
        cast(AbstractExchange, exchange),
        now=lambda: datetime(2026, 10, 16, 12, 0, 0, 250_000, tzinfo=UTC),
    )
    repository = UUID("8ef5d61b-30d8-4872-ac00-903cf4327c81")

    asyncio.run(wakeups.wake(repository, 4))

    [(message, routing_key)] = exchange.published
    expected_id = str(uuid5(NAMESPACE, f"{repository}:4:2026-10-16T12:00:00.250000Z"))
    assert routing_key == "index.repo"
    assert wire(message) == {
        "schema": "index.repo/v1",
        "message_id": expected_id,
        "repository_id": "8ef5d61b-30d8-4872-ac00-903cf4327c81",
        "generation": 4,
        "attempt": 1,
        "requested_at": "2026-10-16T12:00:00.250000Z",
    }
    assert message.message_id == expected_id
    assert message.delivery_mode is aio_pika.DeliveryMode.PERSISTENT
    assert message.content_type == "application/json"


def test_every_wake_up_has_an_id_of_its_own() -> None:
    events: list[tuple[str, ...]] = []
    exchange = FakeExchange("code_index", events)
    moments = iter(
        [datetime(2026, 10, 16, 12, 0, tzinfo=UTC), datetime(2026, 10, 16, 12, 1, tzinfo=UTC)]
    )
    wakeups = index_amqp.AmqpIndexWakeups(
        cast(AbstractExchange, exchange), now=lambda: next(moments)
    )
    repository = uuid4()

    async def wake_twice() -> None:
        await wakeups.wake(repository, 1)
        await wakeups.wake(repository, 1)

    asyncio.run(wake_twice())

    first, second = (message.message_id for message, _ in exchange.published)
    assert first != second


# --- decode -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        {**body(), "schema": "index.repo/v2"},
        {key: value for key, value in body().items() if key != "generation"},
        {**body(), "extra": 1},
        body(attempt=4),
        body(generation=0),
        # A foreign message_id: valid shape, not derived from the fields.
        {**body(), "message_id": str(uuid4())},
        # The id of another wake-up of the same repository.
        {**body(), "requested_at": "2026-10-16T10:00:01.000000Z"},
    ],
    ids=[
        "foreign-schema",
        "missing-key",
        "extra-key",
        "attempt-4",
        "generation-0",
        "foreign-id",
        "other-wake-up",
    ],
)
def test_an_invalid_message_or_a_foreign_message_id_goes_to_the_dlq_unprocessed(
    message: dict[str, Any],
) -> None:
    queue = Queue()

    received, calls = queue.deliver(delivery(queue, message), BuildOutcome(BuildStatus.PUBLISHED))

    assert calls == []
    assert received.nacked == [False] and not received.acked
    assert queue.main.published == [] and queue.retry.published == []


def test_a_body_that_is_not_json_goes_to_the_dlq() -> None:
    queue = Queue()
    received = Delivery(b"not json", queue.events, message_id="m")

    queue.deliver(received, BuildOutcome(BuildStatus.PUBLISHED))

    assert received.nacked == [False] and not received.acked


def test_the_handler_gets_the_repository_and_the_generation() -> None:
    queue = Queue()
    repository = uuid4()

    _, calls = queue.deliver(
        delivery(queue, body(repository_id=repository, generation=7)),
        BuildOutcome(BuildStatus.PUBLISHED),
    )

    assert calls == [(repository, 7)]


def test_the_contract_example_is_a_wake_up_the_consumer_accepts() -> None:
    example = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    queue = Queue()

    received, calls = queue.deliver(delivery(queue, example), BuildOutcome(BuildStatus.PUBLISHED))

    assert calls == [(UUID("8ef5d61b-30d8-4872-ac00-903cf4327c81"), 3)]
    assert received.acked


# --- outcomes ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [
        BuildStatus.PUBLISHED,
        BuildStatus.UNCHANGED,
        BuildStatus.EMPTY,
        BuildStatus.STALE,
        # AC10: a limit or an unsafe archive is acked with no retry and no DLQ.
        BuildStatus.FAILED,
        # A terminal access or policy error released the lease with its code.
        BuildStatus.RELEASED,
        # AC12: a lost lease stops the job without a write and without a retry.
        BuildStatus.LEASE_LOST,
    ],
)
def test_a_final_outcome_is_acked_with_no_retry_and_no_dlq(status: BuildStatus) -> None:
    queue = Queue()

    received, _ = queue.deliver(delivery(queue, body()), BuildOutcome(status, error_code="x"))

    assert received.acked and received.nacked == []
    assert queue.main.published == [] and queue.retry.published == []


def test_rewake_is_only_acked_because_the_job_woke_the_repository_itself() -> None:
    queue = Queue()

    received, _ = queue.deliver(delivery(queue, body()), BuildOutcome(BuildStatus.REWAKE))

    assert received.acked and received.nacked == []
    assert queue.main.published == [] and queue.retry.published == []


def test_busy_goes_to_the_30s_queue_with_the_same_attempt_confirmed_before_the_ack() -> None:
    queue = Queue()
    message = body(attempt=2)

    received, _ = queue.deliver(delivery(queue, message), BuildOutcome(BuildStatus.BUSY))

    [(copy, routing_key)] = queue.retry.published
    assert routing_key == "index.repo.retry.30s"
    assert wire(copy) == message
    assert copy.message_id == message["message_id"]
    assert queue.main.published == []
    assert queue.events == [("confirmed", "code_index.retry", "index.repo.retry.30s"), ("ack",)]
    assert received.nacked == []


@pytest.mark.parametrize(
    ("attempt", "outcome", "key"),
    [
        (1, BuildOutcome(BuildStatus.TRANSIENT, error_code="vcs_unavailable"), "30s"),
        (2, BuildOutcome(BuildStatus.TRANSIENT, error_code="vcs_unavailable"), "2m"),
        # A rate limit waits at least 2 minutes.
        (1, BuildOutcome(BuildStatus.TRANSIENT, error_code="vcs_rate_limited"), "2m"),
        (
            1,
            BuildOutcome(
                BuildStatus.TRANSIENT,
                error_code="vcs_rate_limited",
                retry_after=timedelta(seconds=120),
            ),
            "2m",
        ),
        (
            1,
            BuildOutcome(
                BuildStatus.TRANSIENT,
                error_code="vcs_rate_limited",
                retry_after=timedelta(seconds=121),
            ),
            "10m",
        ),
        # Any Retry-After waits at least 2 minutes, a rate limit or not.
        (
            1,
            BuildOutcome(
                BuildStatus.TRANSIENT,
                error_code="vcs_unavailable",
                retry_after=timedelta(seconds=5),
            ),
            "2m",
        ),
        # Never a sleep: a delay of one day takes the 10m queue too.
        (
            2,
            BuildOutcome(
                BuildStatus.TRANSIENT,
                error_code="vcs_unavailable",
                retry_after=timedelta(days=1),
            ),
            "10m",
        ),
    ],
    ids=[
        "first",
        "second",
        "rate-limit",
        "retry-after-2m",
        "retry-after-121s",
        "retry-after-5s-not-limited",
        "one-day",
    ],
)
def test_a_transient_outcome_is_retried_with_the_next_attempt(
    attempt: int, outcome: BuildOutcome, key: str
) -> None:
    queue = Queue()
    message = body(attempt=attempt)

    received, _ = queue.deliver(delivery(queue, message), outcome)

    [(copy, routing_key)] = queue.retry.published
    assert routing_key == f"index.repo.retry.{key}"
    assert wire(copy) == {**message, "attempt": attempt + 1}
    assert copy.message_id == message["message_id"]
    assert queue.events == [("confirmed", "code_index.retry", f"index.repo.retry.{key}"), ("ack",)]


def test_a_transient_outcome_on_attempt_3_is_acked_without_a_retry() -> None:
    queue = Queue()

    received, _ = queue.deliver(
        delivery(queue, body(attempt=3)),
        BuildOutcome(BuildStatus.TRANSIENT, error_code="vcs_unavailable"),
    )

    assert received.acked and received.nacked == []
    assert queue.retry.published == [] and queue.main.published == []


def test_leased_is_acked_when_another_job_holds_the_lease() -> None:
    queue = Queue()

    received, _ = queue.deliver(delivery(queue, body()), BuildOutcome(BuildStatus.LEASED))

    assert received.acked
    assert queue.retry.published == [] and queue.main.published == []


def test_leased_on_a_redelivery_waits_twice_in_the_10m_queue_then_is_acked() -> None:
    # A redelivery means that the holder of the lease died with this very message; its
    # lease stays until it expires, so the message waits for it at most twice.
    message = body()
    queue = Queue()
    first, _ = queue.deliver(
        delivery(queue, message, redelivered=True), BuildOutcome(BuildStatus.LEASED)
    )
    [(copy, routing_key)] = queue.retry.published
    assert routing_key == "index.repo.retry.10m"
    assert wire(copy) == message
    assert copy.headers == {"x-lease-wait": 1}
    assert queue.events == [("confirmed", "code_index.retry", "index.repo.retry.10m"), ("ack",)]

    # Back from the 10m queue the message is not a redelivery, but it still waits.
    queue = Queue()
    queue.deliver(
        delivery(queue, message, headers={"x-lease-wait": 1}), BuildOutcome(BuildStatus.LEASED)
    )
    [(copy, routing_key)] = queue.retry.published
    assert routing_key == "index.repo.retry.10m"
    assert copy.headers == {"x-lease-wait": 2}

    queue = Queue()
    last, _ = queue.deliver(
        delivery(queue, message, headers={"x-lease-wait": 2}), BuildOutcome(BuildStatus.LEASED)
    )
    assert last.acked and queue.retry.published == []


def test_the_lease_waits_follow_the_lease_of_the_settings() -> None:
    # A lease of 1560 s needs three waits of 10 minutes.
    message = body()
    queue = Queue(lease_waits=3)
    queue.deliver(
        delivery(queue, message, headers={"x-lease-wait": 2}), BuildOutcome(BuildStatus.LEASED)
    )
    [(copy, routing_key)] = queue.retry.published
    assert routing_key == "index.repo.retry.10m"
    assert copy.headers == {"x-lease-wait": 3}

    queue = Queue(lease_waits=3)
    last, _ = queue.deliver(
        delivery(queue, message, headers={"x-lease-wait": 3}), BuildOutcome(BuildStatus.LEASED)
    )
    assert last.acked and queue.retry.published == []


@pytest.mark.parametrize("value", [float("inf"), float("-inf")], ids=["inf", "-inf"])
def test_an_infinite_lease_wait_header_counts_as_the_last_wait(value: float) -> None:
    queue = Queue()

    received, _ = queue.deliver(
        delivery(queue, body(), redelivered=True, headers={"x-lease-wait": value}),
        BuildOutcome(BuildStatus.LEASED),
    )

    assert received.acked and received.nacked == []
    assert queue.retry.published == []


def test_a_failed_retry_publish_leaves_the_delivery_to_the_broker() -> None:
    queue = Queue()
    queue.retry.fail = True

    received, _ = queue.deliver(delivery(queue, body()), BuildOutcome(BuildStatus.BUSY))

    assert not received.acked
    assert received.nacked == [True]


@pytest.mark.parametrize(
    ("outcome", "change"),
    [
        (BuildOutcome(BuildStatus.BUSY), {}),
        (BuildOutcome(BuildStatus.TRANSIENT, error_code="vcs_unavailable"), {}),
        (BuildOutcome(BuildStatus.LEASED), {"redelivered": True}),
    ],
    ids=["busy", "transient", "leased"],
)
def test_a_copy_that_fails_three_times_goes_to_the_dlq(
    outcome: BuildOutcome, change: dict[str, Any], caplog: pytest.LogCaptureFixture
) -> None:
    # A deleted retry queue or exchange: each pass runs the job again, so it is bounded.
    message = body()
    queue = Queue()
    queue.retry.fail = True

    settled = []
    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            received, _ = queue.deliver(delivery(queue, message, **change), outcome)
            assert not received.acked
            settled.append(received.nacked)

    assert settled == [[True], [True], [False]]
    assert "routing to DLQ code_index.dlq" in caplog.text
    assert message["message_id"] not in review_amqp._unexpected_delivery_attempts


def test_a_failed_copy_publish_pauses_before_the_redelivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.modules.reviews.infrastructure.amqp.REQUEUE_ERROR_DELAY_SECONDS", 0.2)
    queue = Queue()
    queue.retry.fail = True

    started = time.monotonic()
    received, _ = queue.deliver(delivery(queue, body()), BuildOutcome(BuildStatus.BUSY))

    assert time.monotonic() - started >= 0.2
    assert received.nacked == [True]


def _fail_once(message: dict[str, Any]) -> None:
    queue = Queue()
    queue.retry.fail = True
    received, _ = queue.deliver(delivery(queue, message), BuildOutcome(BuildStatus.BUSY))
    assert received.nacked == [True]
    assert message["message_id"] in review_amqp._unexpected_delivery_attempts


def test_a_confirmed_copy_forgets_the_failed_passes() -> None:
    message = body()
    _fail_once(message)
    queue = Queue()

    received, _ = queue.deliver(delivery(queue, message), BuildOutcome(BuildStatus.BUSY))

    assert received.acked
    assert message["message_id"] not in review_amqp._unexpected_delivery_attempts


def test_an_acked_outcome_forgets_the_failed_passes() -> None:
    message = body()
    _fail_once(message)
    queue = Queue()

    received, _ = queue.deliver(delivery(queue, message), BuildOutcome(BuildStatus.PUBLISHED))

    assert received.acked
    assert message["message_id"] not in review_amqp._unexpected_delivery_attempts


def test_a_rejected_body_forgets_the_failed_passes() -> None:
    message = body()
    _fail_once(message)
    queue = Queue()
    poison = Delivery(b"not json", queue.events, message_id=message["message_id"])

    queue.deliver(poison, BuildOutcome(BuildStatus.PUBLISHED))

    assert poison.nacked == [False]
    assert message["message_id"] not in review_amqp._unexpected_delivery_attempts


# --- unexpected errors --------------------------------------------------------------


def test_an_unexpected_error_republishes_with_x_attempt_and_the_third_goes_to_the_dlq(
    caplog: pytest.LogCaptureFixture,
) -> None:
    message = body()
    queue = Queue()

    first, _ = queue.deliver(delivery(queue, message), RuntimeError("database is down"))
    [(copy, routing_key)] = queue.main.published
    assert routing_key == "index.repo"
    assert copy.headers["x-attempt"] == 1
    assert wire(copy) == message
    assert queue.events == [("confirmed", "code_index", "index.repo"), ("ack",)]

    second, _ = queue.deliver(
        delivery(queue, message, headers=dict(copy.headers)), RuntimeError("database is down")
    )
    assert second.acked and queue.main.published[-1][0].headers["x-attempt"] == 2

    with caplog.at_level(logging.ERROR):
        third, _ = queue.deliver(
            delivery(queue, message, headers=dict(queue.main.published[-1][0].headers)),
            RuntimeError("database is down"),
        )
    assert third.nacked == [False] and not third.acked
    assert len(queue.main.published) == 2
    assert "routing to DLQ code_index.dlq" in caplog.text
