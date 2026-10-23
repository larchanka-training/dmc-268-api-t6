"""Stop worker queue intake without interrupting active deliveries (#185)."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self, cast
from uuid import UUID, uuid4

import aio_pika
import pytest
from aio_pika.abc import AbstractIncomingMessage, AbstractQueue, AbstractQueueIterator
from aio_pika.queue import QueueIterator

import app.worker as worker
from app.bootstrap.code_index_worker import IndexConsumer
from app.modules.code_index.application.build_index import (
    BuildOutcome,
    BuildRepositoryIndex,
    BuildStatus,
)
from app.modules.code_index.infrastructure.amqp import IndexQueuePublisher
from app.modules.reviews.application.handle_review_run import ClaimedAttempt, HandleReviewRun
from app.modules.reviews.infrastructure.amqp import consume


@dataclass
class IdleQueue:
    name: str = "review.run.fast"
    entered: asyncio.Event = field(default_factory=asyncio.Event)
    waiting: asyncio.Event = field(default_factory=asyncio.Event)
    closed: bool = False
    closed_event: asyncio.Event = field(default_factory=asyncio.Event)

    def iterator(self) -> IdleQueue:
        return self

    async def __aenter__(self) -> Self:
        self.entered.set()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    def __aiter__(self) -> AsyncIterator[AbstractIncomingMessage]:
        return self

    async def __anext__(self) -> AbstractIncomingMessage:
        self.waiting.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await self.close()
            raise
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True
        self.closed_event.set()


def test_idle_review_intake_stops_without_needing_a_delivery() -> None:
    async def scenario() -> None:
        queue, stop = IdleQueue(), asyncio.Event()

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("idle consumer must not dispatch work")

        task = asyncio.create_task(consume(cast(AbstractQueue, queue), handle, stop=stop))
        await asyncio.wait_for(queue.waiting.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        assert queue.closed

    asyncio.run(scenario())


@pytest.fixture
def rabbitmq_url() -> str:
    url = os.environ.get("TEST_RABBITMQ_URL") or (
        os.environ.get("RABBITMQ_URL") if os.environ.get("GITHUB_ACTIONS") == "true" else None
    )
    if url is None:
        pytest.skip("set TEST_RABBITMQ_URL to a disposable RabbitMQ for iterator proof")
    return url


@pytest.mark.integration
@pytest.mark.parametrize(
    "stop_case", ["drain_after_close", "finish_during_cancel", "cancel_during_close"]
)
def test_real_broker_busy_stop_cancels_intake_without_settling_active_work_or_resubscribing(
    rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch, stop_case: str
) -> None:
    async def scenario() -> None:
        connection = await aio_pika.connect_robust(rabbitmq_url)
        channel = await connection.channel()
        await channel.set_qos(prefetch_count=2)
        observer = await connection.channel()
        queue = await channel.declare_queue(f"test_shutdown_{uuid4().hex}", durable=True)
        reader = await observer.get_queue(queue.name)
        stop, started, buffered = asyncio.Event(), asyncio.Event(), asyncio.Event()
        cancel_started, allow_cancel, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        close_completed, handler_cancelled = asyncio.Event(), asyncio.Event()
        cancel_interrupted = False
        handler_cancellations = 0
        seen: list[bytes] = []
        received: list[AbstractIncomingMessage] = []
        subscriptions: list[str] = []
        queue_type = type(queue)
        original_iterator, original_consume, original_cancel = (
            queue_type.iterator,
            queue_type.consume,
            queue_type.cancel,
        )

        def iterator(current_queue: Any) -> AbstractQueueIterator:
            current = original_iterator(current_queue)
            if current_queue is not queue:
                return current
            sdk_iterator = cast(QueueIterator, current)
            on_message = sdk_iterator.on_message
            close = sdk_iterator.close

            async def observed_close() -> None:
                await close()
                close_completed.set()

            async def observed(message: AbstractIncomingMessage) -> None:
                await on_message(message)
                received.append(message)
                if len(received) == 2:
                    buffered.set()

            monkeypatch.setattr(sdk_iterator, "on_message", observed)
            monkeypatch.setattr(sdk_iterator, "close", observed_close)
            return current

        async def subscribed(current_queue: Any, *args: Any, **kwargs: Any) -> str:
            tag = await original_consume(current_queue, *args, **kwargs)
            if current_queue is queue:
                subscriptions.append(tag)
            return tag

        async def blocked_cancel(current_queue: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal cancel_interrupted
            if current_queue is queue:
                cancel_started.set()
                try:
                    await allow_cancel.wait()
                except asyncio.CancelledError:
                    cancel_interrupted = True
                    raise
            return await original_cancel(current_queue, *args, **kwargs)

        monkeypatch.setattr(queue_type, "iterator", iterator)
        monkeypatch.setattr(queue_type, "consume", subscribed)
        monkeypatch.setattr(queue_type, "cancel", blocked_cancel)

        async def handle(message: AbstractIncomingMessage) -> None:
            nonlocal handler_cancellations
            seen.append(message.body)
            started.set()
            try:
                await release.wait()
                await message.ack()
            except asyncio.CancelledError:
                handler_cancellations += 1
                handler_cancelled.set()
                raise

        task: asyncio.Task[None] | None = None
        try:
            await observer.default_exchange.publish(aio_pika.Message(b"active"), queue.name)
            await observer.default_exchange.publish(aio_pika.Message(b"buffered"), queue.name)
            task = asyncio.create_task(consume(queue, handle, stop=stop))
            await asyncio.wait_for(started.wait(), 2)
            await asyncio.wait_for(buffered.wait(), 2)
            stop.set()
            await asyncio.wait_for(cancel_started.wait(), 1)
            assert not received[0].processed
            assert not task.done()
            if stop_case == "cancel_during_close":
                task.cancel()
                await asyncio.wait_for(handler_cancelled.wait(), 2)
                allow_cancel.set()
            elif stop_case == "finish_during_cancel":
                # Finishing during basic.cancel must not start anext again.
                release.set()
                allow_cancel.set()
            else:
                # Actual SDK close returns its buffer while active work remains unsettled.
                allow_cancel.set()
                await asyncio.wait_for(close_completed.wait(), 2)
                assert not received[0].processed and received[1].processed
                assert not task.done()
                release.set()
            if stop_case == "cancel_during_close":
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                assert handler_cancellations == 1
                assert not received[0].processed
            else:
                await asyncio.wait_for(task, 2)
                assert received[0].processed
            assert not cancel_interrupted
            assert seen == [b"active"]
            assert len(subscriptions) == 1
            assert received[1].processed
            returned = await reader.get(timeout=2)
            assert returned is not None and returned.body == b"buffered"
            await returned.ack()
            await queue.declare()
            assert queue.declaration_result.consumer_count == 0
            assert not channel.is_closed and not connection.is_closed
        finally:
            release.set()
            allow_cancel.set()
            if task is not None:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            await reader.delete(if_unused=False, if_empty=False)
            await connection.close()

    asyncio.run(scenario())


@dataclass
class Message:
    body: bytes
    message_id: str = "shutdown"
    processed: bool = False
    acked: bool = False
    nacked: list[bool] = field(default_factory=list)

    async def ack(self) -> None:
        self.acked = self.processed = True

    async def nack(self, requeue: bool = True) -> None:
        self.nacked.append(requeue)
        self.processed = True


@dataclass
class BufferedQueue(IdleQueue):
    deliveries: list[Message] = field(default_factory=list)

    async def __anext__(self) -> AbstractIncomingMessage:
        if self.deliveries and not self.closed:
            return cast(AbstractIncomingMessage, self.deliveries.pop(0))
        return await super().__anext__()

    async def close(self) -> None:
        for message in self.deliveries:
            await message.nack(requeue=True)
        self.deliveries.clear()
        await super().close()


def test_busy_review_stops_intake_and_returns_buffer_without_interrupting_handler() -> None:
    async def scenario() -> None:
        first, second = Message(b"active"), Message(b"buffered")
        queue = BufferedQueue(deliveries=[first, second])
        stop, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen: list[bytes] = []

        async def handle(message: AbstractIncomingMessage) -> None:
            seen.append(message.body)
            started.set()
            await release.wait()
            await message.ack()

        task = asyncio.create_task(consume(cast(AbstractQueue, queue), handle, stop=stop))
        try:
            await asyncio.wait_for(started.wait(), 1)
            stop.set()
            await asyncio.wait_for(queue.closed_event.wait(), 1)
            assert not first.processed and first.nacked == []
            assert second.nacked == [True]
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 1)
            assert seen == [b"active"] and first.acked
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_stop_aware_iterator_exhaustion_returns_normally() -> None:
    class EmptyQueue(IdleQueue):
        async def __anext__(self) -> AbstractIncomingMessage:
            raise StopAsyncIteration

    async def scenario() -> None:
        queue = EmptyQueue()

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("exhausted queue must not dispatch")

        await consume(cast(AbstractQueue, queue), handle, stop=asyncio.Event())
        assert queue.closed

    asyncio.run(scenario())


def test_idle_index_intake_accepts_the_same_stop_event() -> None:
    class Jobs:
        async def execute(self, repository_id: UUID, generation: int) -> BuildOutcome:
            raise AssertionError("idle index consumer must not dispatch work")

    async def scenario() -> None:
        queue, stop = IdleQueue(name="index.repo"), asyncio.Event()
        consumer = IndexConsumer(
            cast(AbstractQueue, queue),
            cast(BuildRepositoryIndex, Jobs()),
            cast(IndexQueuePublisher, object()),
        )
        task = asyncio.create_task(consumer.consume(stop=stop))
        await asyncio.wait_for(queue.waiting.wait(), 1)
        stop.set()
        await asyncio.wait_for(task, 1)
        assert queue.closed

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("cancel_runner", [False, True])
def test_real_broker_idle_stop_owns_receive_cleanup_without_resubscribing_or_double_cancel(
    rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch, cancel_runner: bool
) -> None:
    async def scenario() -> None:
        connection = await aio_pika.connect_robust(rabbitmq_url)
        channel = await connection.channel()
        observer = await connection.channel()
        queue = await channel.declare_queue(f"test_shutdown_{uuid4().hex}", durable=True)
        reader = await observer.get_queue(queue.name)
        ready, stop = asyncio.Event(), asyncio.Event()
        cancel_started, allow_cancel = asyncio.Event(), asyncio.Event()
        cancel_interrupted = False
        subscriptions = 0
        queue_type = type(queue)
        original_consume, original_cancel = queue_type.consume, queue_type.cancel

        async def subscribed(current: Any, *args: Any, **kwargs: Any) -> str:
            nonlocal subscriptions
            tag = await original_consume(current, *args, **kwargs)
            if current is queue:
                subscriptions += 1
                ready.set()
            return tag

        async def blocked_cancel(current: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal cancel_interrupted
            if current is queue:
                cancel_started.set()
                try:
                    await allow_cancel.wait()
                except asyncio.CancelledError:
                    cancel_interrupted = True
                    raise
            return await original_cancel(current, *args, **kwargs)

        monkeypatch.setattr(queue_type, "consume", subscribed)
        monkeypatch.setattr(queue_type, "cancel", blocked_cancel)

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("stopped idle consumer must not dispatch")

        task = asyncio.create_task(consume(queue, handle, stop=stop))
        try:
            await asyncio.wait_for(ready.wait(), 2)
            stop.set()
            await asyncio.wait_for(cancel_started.wait(), 2)
            if cancel_runner:
                task.cancel()
            allow_cancel.set()
            if cancel_runner:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            else:
                await asyncio.wait_for(task, 2)
            assert not cancel_interrupted
            assert subscriptions == 1
            await queue.declare()
            assert queue.declaration_result.consumer_count == 0
            await observer.default_exchange.publish(aio_pika.Message(b"after-stop"), queue.name)
            available = await reader.get(timeout=2)
            assert available is not None and available.body == b"after-stop"
            await available.ack()
            assert not channel.is_closed
        finally:
            allow_cancel.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await reader.delete(if_unused=False, if_empty=False)
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_real_broker_fetched_stop_tie_is_unprocessed_until_its_channel_closes(
    rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        connection = await aio_pika.connect_robust(rabbitmq_url)
        channel = await connection.channel()
        observer = await connection.channel()
        queue = await channel.declare_queue(f"test_shutdown_{uuid4().hex}", durable=True)
        reader = await observer.get_queue(queue.name)
        stop = asyncio.Event()
        fetched: list[AbstractIncomingMessage] = []
        handled: list[bytes] = []
        queue_type = type(queue)
        original_iterator = queue_type.iterator

        class StopAfterFetch:
            def __init__(self, inner: AbstractQueueIterator) -> None:
                self.inner = inner

            async def __aenter__(self) -> Self:
                await self.inner.__aenter__()
                return self

            async def __aexit__(self, *exc: Any) -> None:
                await self.inner.__aexit__(*exc)

            def __aiter__(self) -> Self:
                return self

            async def __anext__(self) -> AbstractIncomingMessage:
                message = await anext(self.inner)
                fetched.append(message)
                stop.set()
                return message

            async def close(self) -> None:
                await self.inner.close()

        def iterator(current: Any) -> AbstractQueueIterator:
            inner = original_iterator(current)
            if current is queue:
                return cast(AbstractQueueIterator, StopAfterFetch(inner))
            return inner

        monkeypatch.setattr(queue_type, "iterator", iterator)

        async def handle(message: AbstractIncomingMessage) -> None:
            handled.append(message.body)
            await message.ack()

        try:
            await observer.default_exchange.publish(aio_pika.Message(b"stop-tie"), queue.name)
            await asyncio.wait_for(consume(queue, handle, stop=stop), 2)
            assert handled == []
            assert len(fetched) == 1 and not fetched[0].processed
            assert await reader.get(fail=False) is None
            assert not channel.is_closed
            await channel.close()
            async with asyncio.timeout(2):
                returned = await reader.get(fail=False)
                while returned is None:
                    await asyncio.sleep(0.01)
                    returned = await reader.get(fail=False)
            assert returned.body == b"stop-tie" and returned.redelivered
            await returned.ack()
        finally:
            await reader.delete(if_unused=False, if_empty=False)
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("during_entry", [False, True])
@pytest.mark.parametrize("index", [False, True])
def test_stop_before_or_during_subscription_never_starts_receive(
    during_entry: bool, index: bool
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        class StartupQueue(IdleQueue):
            async def __aenter__(self) -> Self:
                stop.set()
                return await super().__aenter__()

        class Jobs:
            async def execute(self, repository_id: UUID, generation: int) -> BuildOutcome:
                raise AssertionError("stopped startup must not dispatch index work")

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("stopped startup must not dispatch review work")

        queue = StartupQueue()
        if not during_entry:
            stop.set()
        if index:
            consumer = IndexConsumer(
                cast(AbstractQueue, queue),
                cast(BuildRepositoryIndex, Jobs()),
                cast(IndexQueuePublisher, object()),
            )
            await consumer.consume(stop=stop)
        else:
            await consume(cast(AbstractQueue, queue), handle, stop=stop)
        assert queue.entered.is_set() is during_entry
        assert queue.closed is during_entry
        assert not queue.waiting.is_set()

    asyncio.run(scenario())


def test_busy_index_stops_intake_and_preserves_active_job_settlement() -> None:
    async def scenario() -> None:
        body = (
            Path(__file__).resolve().parents[1] / "contracts/examples/index.repo.v1.json"
        ).read_bytes()
        first, second = Message(body), Message(body)
        queue = BufferedQueue(name="index.repo", deliveries=[first, second])
        stop, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        seen: list[UUID] = []

        class Jobs:
            async def execute(self, repository_id: UUID, generation: int) -> BuildOutcome:
                seen.append(repository_id)
                started.set()
                await release.wait()
                return BuildOutcome(BuildStatus.PUBLISHED)

        consumer = IndexConsumer(
            cast(AbstractQueue, queue),
            cast(BuildRepositoryIndex, Jobs()),
            cast(IndexQueuePublisher, object()),
        )
        task = asyncio.create_task(consumer.consume(stop=stop))
        try:
            await asyncio.wait_for(started.wait(), 1)
            stop.set()
            await asyncio.wait_for(queue.closed_event.wait(), 1)
            assert not first.processed and first.nacked == []
            assert second.nacked == [True]
            assert not task.done()
            release.set()
            await asyncio.wait_for(task, 1)
            assert seen == [UUID("8ef5d61b-30d8-4872-ac00-903cf4327c81")]
            assert first.acked
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_iterator_failure_stays_visible_and_does_not_leave_stop_waiter() -> None:
    class FailedQueue(IdleQueue):
        async def __anext__(self) -> AbstractIncomingMessage:
            raise RuntimeError("iterator failed")

    async def scenario() -> None:
        before = asyncio.all_tasks()
        queue = FailedQueue()

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("failed iterator must not dispatch")

        with pytest.raises(RuntimeError, match="iterator failed"):
            await consume(cast(AbstractQueue, queue), handle, stop=asyncio.Event())
        assert queue.closed
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


def test_close_failure_drains_active_handler_then_propagates_without_next_receive() -> None:
    async def scenario() -> None:
        first = Message(b"active")
        stop, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closed = asyncio.Event()
        receives = 0

        class FailedCloseQueue(BufferedQueue):
            async def __anext__(self) -> AbstractIncomingMessage:
                nonlocal receives
                receives += 1
                return await super().__anext__()

            async def close(self) -> None:
                closed.set()
                raise RuntimeError("cancel failed")

        queue = FailedCloseQueue(deliveries=[first])

        async def handle(message: AbstractIncomingMessage) -> None:
            started.set()
            await release.wait()
            await message.ack()

        task = asyncio.create_task(consume(cast(AbstractQueue, queue), handle, stop=stop))
        try:
            await asyncio.wait_for(started.wait(), 1)
            stop.set()
            await asyncio.wait_for(closed.wait(), 1)
            assert not first.processed and not task.done()
            release.set()
            with pytest.raises(RuntimeError, match="cancel failed"):
                await asyncio.wait_for(task, 1)
            assert first.acked and receives == 1
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_external_cancellation_still_cancels_active_handler_and_cleans_owned_tasks() -> None:
    async def scenario() -> None:
        before = asyncio.all_tasks()
        queue = BufferedQueue(deliveries=[Message(b"active")])
        started, cancelled = asyncio.Event(), asyncio.Event()

        async def handle(message: AbstractIncomingMessage) -> None:
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        task = asyncio.create_task(
            consume(cast(AbstractQueue, queue), handle, stop=asyncio.Event())
        )
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set() and queue.closed
        assert asyncio.all_tasks() == before

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("stage", ["after_entry", "fetched_tie", "idle_explicit"])
def test_real_broker_every_stop_close_survives_caller_cancellation(
    rabbitmq_url: str, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    async def scenario() -> None:
        connection = await aio_pika.connect_robust(rabbitmq_url)
        channel = await connection.channel()
        observer = await connection.channel()
        queue = await channel.declare_queue(f"test_shutdown_{uuid4().hex}", durable=True)
        reader = await observer.get_queue(queue.name)
        stop, waiting = asyncio.Event(), asyncio.Event()
        cancel_started, allow_cancel = asyncio.Event(), asyncio.Event()
        interrupted = False
        fetched: list[AbstractIncomingMessage] = []
        queue_type = type(queue)
        original_iterator, original_cancel = queue_type.iterator, queue_type.cancel

        class StopIterator:
            def __init__(self, inner: AbstractQueueIterator) -> None:
                self.inner = inner

            async def __aenter__(self) -> Self:
                await self.inner.__aenter__()
                if stage == "after_entry":
                    stop.set()
                return self

            async def __aexit__(self, *exc: Any) -> None:
                await self.inner.__aexit__(*exc)

            def __aiter__(self) -> Self:
                return self

            async def __anext__(self) -> AbstractIncomingMessage:
                if stage == "idle_explicit":
                    # Cancellation before the SDK's receive-wait cleanup needs explicit close.
                    waiting.set()
                    await asyncio.Event().wait()
                message = await anext(self.inner)
                fetched.append(message)
                stop.set()
                return message

            async def close(self) -> None:
                await self.inner.close()

        def iterator(current: Any) -> AbstractQueueIterator:
            inner = original_iterator(current)
            return cast(AbstractQueueIterator, StopIterator(inner)) if current is queue else inner

        async def blocked_cancel(current: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal interrupted
            if current is queue:
                cancel_started.set()
                try:
                    await allow_cancel.wait()
                except asyncio.CancelledError:
                    interrupted = True
                    raise
            return await original_cancel(current, *args, **kwargs)

        monkeypatch.setattr(queue_type, "iterator", iterator)
        monkeypatch.setattr(queue_type, "cancel", blocked_cancel)

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("stop-close cancellation must not dispatch work")

        task = asyncio.create_task(consume(queue, handle, stop=stop))
        try:
            if stage == "fetched_tie":
                await observer.default_exchange.publish(aio_pika.Message(b"stop-tie"), queue.name)
            elif stage == "idle_explicit":
                await asyncio.wait_for(waiting.wait(), 2)
                stop.set()
            await asyncio.wait_for(cancel_started.wait(), 2)
            task.cancel()
            allow_cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert not interrupted
            await queue.declare()
            assert queue.declaration_result.consumer_count == 0
            assert not channel.is_closed
            assert all(not message.processed for message in fetched)
        finally:
            allow_cancel.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await reader.delete(if_unused=False, if_empty=False)
            await connection.close()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize("fatal", [False, True])
def test_real_broker_caller_cancellation_during_final_cleanup_waits_for_close(
    rabbitmq_url: str,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    fatal: bool,
) -> None:
    async def scenario() -> None:
        connection = await aio_pika.connect_robust(rabbitmq_url)
        channel = await connection.channel()
        observer = await connection.channel()
        queue = await channel.declare_queue(f"test_shutdown_{uuid4().hex}", durable=True)
        reader = await observer.get_queue(queue.name)
        cancel_started, allow_cancel, cancel_completed = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        queue_type = type(queue)
        original_iterator, original_cancel = queue_type.iterator, queue_type.cancel

        class FinishedIterator:
            def __init__(self, inner: AbstractQueueIterator) -> None:
                self.inner = inner

            async def __aenter__(self) -> Self:
                await self.inner.__aenter__()
                return self

            async def __aexit__(self, *exc: Any) -> None:
                await self.inner.__aexit__(*exc)

            def __aiter__(self) -> Self:
                return self

            async def __anext__(self) -> AbstractIncomingMessage:
                if fatal:
                    raise RuntimeError("iterator exit failed")
                raise StopAsyncIteration

            async def close(self) -> None:
                await self.inner.close()

        def iterator(current: Any) -> AbstractQueueIterator:
            inner = original_iterator(current)
            return (
                cast(AbstractQueueIterator, FinishedIterator(inner)) if current is queue else inner
            )

        async def blocked_cancel(current: Any, *args: Any, **kwargs: Any) -> Any:
            if current is queue:
                cancel_started.set()
                await allow_cancel.wait()
            result = await original_cancel(current, *args, **kwargs)
            if current is queue:
                cancel_completed.set()
            return result

        monkeypatch.setattr(queue_type, "iterator", iterator)
        monkeypatch.setattr(queue_type, "cancel", blocked_cancel)

        async def handle(message: AbstractIncomingMessage) -> None:
            raise AssertionError("finished iterator must not dispatch work")

        task = asyncio.create_task(consume(queue, handle, stop=asyncio.Event()))
        try:
            await asyncio.wait_for(cancel_started.wait(), 2)
            task.cancel()
            # A broker round trip gives the cancelled runner a scheduling turn.
            await queue.declare()
            assert not task.done()
            task.cancel()
            await queue.declare()
            assert not task.done()
            allow_cancel.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
            assert cancel_completed.is_set()
            await queue.declare()
            assert queue.declaration_result.consumer_count == 0
            if fatal:
                assert "iterator exit failed" in caplog.text
        finally:
            allow_cancel.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            await reader.delete(if_unused=False, if_empty=False)
            await connection.close()

    asyncio.run(scenario())


@dataclass
class WorkerHarness:
    resources_closed: list[str] = field(default_factory=list)
    startup_entered: asyncio.Event = field(default_factory=asyncio.Event)
    leader_stopped: asyncio.Event = field(default_factory=asyncio.Event)
    leader_started: asyncio.Event = field(default_factory=asyncio.Event)
    consumer_names: list[str] = field(default_factory=list)


def install_worker_harness(
    monkeypatch: pytest.MonkeyPatch,
    consumer: Callable[[str, asyncio.Event | None], Awaitable[None]],
    *,
    pause_startup: bool = False,
    stop_after_subscription: asyncio.Event | None = None,
    review_handler: HandleReviewRun | None = None,
) -> WorkerHarness:
    harness = WorkerHarness()

    class Engine:
        async def dispose(self) -> None:
            harness.resources_closed.append("database")

    async def consumer_queue(name: str) -> str:
        return name

    @asynccontextmanager
    async def channels(*args: object) -> AsyncIterator[SimpleNamespace]:
        harness.startup_entered.set()
        try:
            if pause_startup:
                await asyncio.Event().wait()
            yield SimpleNamespace(
                connection=object(), publisher=object(), consumer_queue=consumer_queue
            )
        finally:
            harness.resources_closed.append("amqp")

    async def consume(queue: str, handle: object, *, stop: asyncio.Event | None = None) -> None:
        harness.consumer_names.append(queue)
        await consumer(queue, stop)

    class Index:
        async def consume(self, *, stop: asyncio.Event | None = None) -> None:
            await consume("index.repo", None, stop=stop)

    @asynccontextmanager
    async def index(*args: object) -> AsyncIterator[Index]:
        try:
            if stop_after_subscription is not None:
                stop_after_subscription.set()
            yield Index()
        finally:
            harness.resources_closed.append("index")

    async def leader(*args: object, **kwargs: object) -> None:
        harness.leader_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            harness.leader_stopped.set()

    async def wait_for_quiescence() -> None:
        return None

    process = SimpleNamespace(
        handle_run=review_handler
        or SimpleNamespace(execute=None, wait_for_quiescence=wait_for_quiescence),
        handle_delivery=SimpleNamespace(execute=None),
        publish_review=SimpleNamespace(execute=None),
        leader_tick=None,
    )
    monkeypatch.setattr(worker, "create_async_engine", lambda *args, **kwargs: Engine())
    monkeypatch.setattr(worker, "amqp_channels", channels)
    monkeypatch.setattr(worker, "index_consumer", index)
    monkeypatch.setattr(worker, "consume", consume)
    monkeypatch.setattr(worker, "run_as_leader", leader)
    monkeypatch.setattr(worker, "compose_worker_process", lambda **kwargs: process)
    return harness


def worker_settings() -> worker.WorkerSettings:
    return worker.WorkerSettings.from_environment(
        {"DATABASE_URL": "postgresql+psycopg://test", "RABBITMQ_URL": "amqp://test"}
    )


def test_worker_composition_wires_same_stop_to_all_intakes_and_stops_leader(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stop, attached = asyncio.Event(), asyncio.Event()
        seen: list[asyncio.Event | None] = []

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            seen.append(consumer_stop)
            if len(seen) == 3:
                attached.set()
            if consumer_stop is None:
                await asyncio.Event().wait()
            else:
                await consumer_stop.wait()

        harness = install_worker_harness(monkeypatch, consumer)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        async with asyncio.timeout(1):
            await attached.wait()
            stop.set()
            await task
        assert seen == [stop, stop, stop]
        assert harness.consumer_names == ["review.run.fast", "review.publish", "index.repo"]
        assert harness.leader_stopped.is_set()
        assert harness.resources_closed == ["amqp", "index", "database"]

    asyncio.run(scenario())


def test_stop_interrupts_broker_startup_before_consumers_exist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            pytest.fail("consumer must not start after startup stop")

        harness = install_worker_harness(monkeypatch, consumer, pause_startup=True)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        async with asyncio.timeout(1):
            await harness.startup_entered.wait()
            stop.set()
            await task
        assert harness.consumer_names == []
        assert harness.resources_closed == ["amqp", "database"]

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["drain", "cancel", "cleanup_overrun"])
def test_worker_drain_then_cancel_once_keeps_resources_open_until_cleanup(
    monkeypatch: pytest.MonkeyPatch, phase: str, caplog: pytest.LogCaptureFixture
) -> None:
    import app.bootstrap.worker_shutdown as shutdown

    monkeypatch.setattr(shutdown, "_DRAIN_TIMEOUT", 0.03)
    monkeypatch.setattr(shutdown, "_CLEANUP_TIMEOUT", 0.03)

    async def scenario() -> None:
        stop, attached = asyncio.Event(), asyncio.Event()
        release, cleanup_release = asyncio.Event(), asyncio.Event()
        all_cleaning = asyncio.Event()
        started: list[str] = []
        cancelled: list[str] = []
        finished: list[str] = []

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            assert consumer_stop is stop
            started.append(name)
            if len(started) == 3:
                attached.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.append(name)
                if len(cancelled) == 3:
                    all_cleaning.set()
                await cleanup_release.wait()
            finished.append(name)

        harness = install_worker_harness(monkeypatch, consumer)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        try:
            async with asyncio.timeout(2):
                await attached.wait()
                stop.set()
                await harness.leader_stopped.wait()
                assert harness.resources_closed == []
                if phase == "drain":
                    release.set()
                else:
                    await all_cleaning.wait()
                    assert harness.resources_closed == []
                    assert len(cancelled) == 3
                    if phase == "cleanup_overrun":
                        while "cleanup deadline exceeded" not in caplog.text:
                            await asyncio.sleep(0.001)
                        assert not task.done()
                        assert harness.resources_closed == []
                    cleanup_release.set()
                await task
            assert sorted(finished) == sorted(started)
            assert len(cancelled) == (0 if phase == "drain" else 3)
            assert harness.resources_closed == ["amqp", "index", "database"]
        finally:
            release.set()
            cleanup_release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


def test_preexisting_stop_does_not_open_resources_or_start_consumers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        stop.set()

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            pytest.fail("pre-stopped worker must not dispatch")

        harness = install_worker_harness(monkeypatch, consumer)
        await worker.run_worker(worker_settings(), stop=stop)
        assert not harness.startup_entered.is_set()
        assert harness.resources_closed == []
        assert harness.consumer_names == []

    asyncio.run(scenario())


@pytest.mark.parametrize("with_stop", [False, True])
def test_external_startup_cancellation_remains_cancellation(
    monkeypatch: pytest.MonkeyPatch, with_stop: bool
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            pytest.fail("startup interrupted before consumer creation")

        harness = install_worker_harness(monkeypatch, consumer, pause_startup=True)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        async with asyncio.timeout(2):
            await harness.startup_entered.wait()
            if with_stop:
                stop.set()
            task.cancel("external caller")
            with pytest.raises(asyncio.CancelledError, match="external caller"):
                await task
        assert harness.resources_closed == ["amqp", "database"]
        assert harness.consumer_names == []

    asyncio.run(scenario())


def test_stop_at_startup_transition_never_creates_consumer_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            pytest.fail("stop at subscription boundary must prevent dispatch")

        harness = install_worker_harness(monkeypatch, consumer, stop_after_subscription=stop)
        await worker.run_worker(worker_settings(), stop=stop)
        assert harness.consumer_names == []
        assert not harness.leader_stopped.is_set()
        assert harness.resources_closed == ["amqp", "index", "database"]

    asyncio.run(scenario())


@pytest.mark.parametrize("caller_cancel", [False, True])
def test_background_heartbeat_is_owned_and_external_runtime_cancel_propagates(
    monkeypatch: pytest.MonkeyPatch, caller_cancel: bool
) -> None:
    async def scenario() -> None:
        stop, attached = asyncio.Event(), asyncio.Event()
        heartbeat_started = asyncio.Event()
        heartbeat_cancel_counts: list[int] = []
        consumer_cancelled: list[str] = []

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            if len(harness.consumer_names) == 3:
                attached.set()
            assert consumer_stop is stop
            try:
                await stop.wait()
            except asyncio.CancelledError:
                consumer_cancelled.append(name)
                raise

        async def heartbeat(path: Path) -> None:
            heartbeat_started.set()
            try:
                await asyncio.Event().wait()
            finally:
                current = asyncio.current_task()
                assert current is not None
                heartbeat_cancel_counts.append(current.cancelling())

        harness = install_worker_harness(monkeypatch, consumer)
        monkeypatch.setattr(worker, "beat", heartbeat)
        monkeypatch.setattr(worker, "reset", lambda path: None)
        settings = replace(worker_settings(), heartbeat_file=Path("/tmp/unused-185-heartbeat"))
        task = asyncio.create_task(worker.run_worker(settings, stop=stop))
        async with asyncio.timeout(2):
            await attached.wait()
            await heartbeat_started.wait()
            if caller_cancel:
                task.cancel("runtime caller")
                with pytest.raises(asyncio.CancelledError, match="runtime caller"):
                    await task
            else:
                stop.set()
                await task
        assert heartbeat_cancel_counts == [1]
        assert len(consumer_cancelled) == (3 if caller_cancel else 0)
        assert harness.leader_stopped.is_set()
        assert harness.resources_closed == ["amqp", "index", "database"]

    asyncio.run(scenario())


def test_fatal_startup_error_is_not_translated_to_normal_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            pytest.fail("fatal startup cannot dispatch")

        harness = install_worker_harness(monkeypatch, consumer)

        def failed_composition(**kwargs: object) -> None:
            raise RuntimeError("fatal startup adapter")

        monkeypatch.setattr(worker, "compose_worker_process", failed_composition)
        with pytest.raises(RuntimeError, match="fatal startup adapter"):
            await worker.run_worker(worker_settings(), stop=asyncio.Event())
        assert harness.resources_closed == ["amqp", "database"]
        assert harness.consumer_names == []

    asyncio.run(scenario())


def test_stop_before_registered_tasks_run_prevents_leader_tick_and_heartbeat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        stop = asyncio.Event()
        heartbeat_started = False

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            assert consumer_stop is stop and stop.is_set()

        async def heartbeat(path: Path) -> None:
            nonlocal heartbeat_started
            heartbeat_started = True
            await asyncio.Event().wait()

        harness = install_worker_harness(monkeypatch, consumer)
        compose = worker.compose_worker_process

        def schedule_stop(**kwargs: Any) -> Any:
            asyncio.get_running_loop().call_soon(stop.set)
            return compose(**kwargs)

        monkeypatch.setattr(worker, "compose_worker_process", schedule_stop)
        monkeypatch.setattr(worker, "beat", heartbeat)
        monkeypatch.setattr(worker, "reset", lambda path: None)
        settings = replace(worker_settings(), heartbeat_file=Path("/tmp/unused-185-heartbeat"))
        async with asyncio.timeout(2):
            await worker.run_worker(settings, stop=stop)
        assert not harness.leader_started.is_set()
        assert not heartbeat_started
        assert harness.resources_closed == ["amqp", "index", "database"]

    asyncio.run(scenario())


def test_review_pipeline_outliving_watchdog_keeps_consumer_and_resources_owned(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import app.bootstrap.worker_shutdown as shutdown
    from tests.test_review_run_worker import NOW, RUN
    from tests.test_review_shutdown import Database, make_handler

    monkeypatch.setattr(shutdown, "_DRAIN_TIMEOUT", 0.01)
    monkeypatch.setattr(shutdown, "_CLEANUP_TIMEOUT", 6.0)

    async def scenario() -> None:
        stop, started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        database = Database()
        pipeline_cancel_count = 0

        async def pipeline(claimed: Any) -> bool:
            nonlocal pipeline_cancel_count
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pipeline_cancel_count += 1
                await release.wait()
            return True

        handler = make_handler(database, pipeline)

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            assert consumer_stop is stop
            if name == "review.run.fast":
                await handler.execute(RUN)
            else:
                await stop.wait()

        harness = install_worker_harness(monkeypatch, consumer)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        try:
            async with asyncio.timeout(7):
                await started.wait()
                stop.set()
                while "pipeline is still active" not in caplog.text:
                    await asyncio.sleep(0.001)
                # Observe the worker without cancelling its retained cleanup owner.
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 0.05)
                assert not task.done()
                assert harness.resources_closed == []
                assert database.row.lease_until is not None and database.row.lease_until > NOW
                assert pipeline_cancel_count == 1
                release.set()
                await task
            assert database.row.lease_until is not None and database.row.lease_until < NOW
            assert pipeline_cancel_count == 1
            assert harness.resources_closed == ["amqp", "index", "database"]
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "exit_path",
    ["stop", "external_cancel", "fatal_child", "stop_cancel_join", "fatal_cancel_join"],
)
def test_watchdog_orphan_is_joined_before_resources_close_on_every_worker_exit(
    monkeypatch: pytest.MonkeyPatch, exit_path: str, caplog: pytest.LogCaptureFixture
) -> None:
    from datetime import timedelta

    import app.bootstrap.worker_shutdown as shutdown
    import app.modules.reviews.application.handle_review_run as review
    from tests.test_review_run_worker import NOW, RUN, RetryQueue
    from tests.test_review_shutdown import Database

    monkeypatch.setattr(shutdown, "_DRAIN_TIMEOUT", 0.2)
    monkeypatch.setattr(shutdown, "_CLEANUP_TIMEOUT", 0.1)
    monkeypatch.setattr(review, "_PIPELINE_CANCEL_WAIT", 0.01)

    async def scenario() -> None:
        stop, started, release, outcome = (asyncio.Event() for _ in range(4))
        database, retry = Database(), RetryQueue()
        clock = NOW
        child: asyncio.Task[bool] | None = None
        cancel_count = 0

        async def pipeline(claimed: ClaimedAttempt) -> bool:
            nonlocal child, cancel_count
            child = asyncio.current_task()
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancel_count += 1
                await release.wait()
            return True

        handler = HandleReviewRun(
            uow_factory=database.uow,
            pipeline=pipeline,
            retry_queue=retry,
            check_runs=None,
            worker_id="worker-185",
            heartbeat_interval=timedelta(milliseconds=1),
            attempt_deadline=timedelta(seconds=1),
            now=lambda: clock,
        )

        async def consumer(name: str, consumer_stop: asyncio.Event | None) -> None:
            if name == "review.run.fast":
                assert await handler.execute(RUN) == "ack"
                outcome.set()
            elif name == "index.repo" and exit_path in {"fatal_child", "fatal_cancel_join"}:
                await outcome.wait()
                raise RuntimeError("fatal sibling after watchdog")
            else:
                await stop.wait()

        harness = install_worker_harness(monkeypatch, consumer, review_handler=handler)
        task = asyncio.create_task(worker.run_worker(worker_settings(), stop=stop))
        try:
            async with asyncio.timeout(2):
                await started.wait()
                clock += timedelta(seconds=2)
                if exit_path in {"stop", "stop_cancel_join"}:
                    stop.set()
                await outcome.wait()
                if exit_path == "external_cancel":
                    task.cancel("worker caller")
                # Ordinary watchdog failure completed on schedule, but its child still writes.
                assert database.row.state == "failed"
                assert database.row.error_code == "deadline_exceeded"
                assert database.row.attempt == 1 and retry.published == []
                with pytest.raises(TimeoutError):
                    await asyncio.wait_for(asyncio.shield(task), 0.05)
                assert harness.resources_closed == []
                assert child is not None and not child.done()
                assert cancel_count == 1
                if exit_path in {"stop_cancel_join", "fatal_cancel_join"}:
                    task.cancel("first caller during final join")
                    await asyncio.sleep(0)
                    task.cancel("repeated caller during final join")
                elif exit_path == "external_cancel":
                    task.cancel("repeated worker caller")
                if exit_path in {"external_cancel", "stop_cancel_join", "fatal_cancel_join"}:
                    await asyncio.sleep(0)
                    assert not task.done() and cancel_count == 1
                release.set()
                if exit_path in {"external_cancel", "stop_cancel_join", "fatal_cancel_join"}:
                    with pytest.raises(asyncio.CancelledError):
                        await task
                elif exit_path == "fatal_child":
                    with pytest.raises(ExceptionGroup) as failed:
                        await task
                    assert failed.group_contains(RuntimeError, match="fatal sibling after watchdog")
                else:
                    await task
            assert harness.resources_closed == ["amqp", "index", "database"]
            assert database.row.state == "failed" and database.row.attempt == 1
            if exit_path == "fatal_cancel_join":
                assert "fatal sibling after watchdog" in caplog.text
                assert "failed before caller cancellation" in caplog.text
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            if child is not None:
                await asyncio.gather(child, return_exceptions=True)

    asyncio.run(scenario())
