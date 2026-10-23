"""Public AMQP connection lifecycle, including actual subprocess shutdown."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import aio_pika
import pytest
from aio_pika.abc import AbstractChannel
from aiormq.exceptions import ChannelInvalidStateError

from app.modules.reviews.application.run_failures import RetryDelays
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
from app.modules.reviews.infrastructure import amqp

_CHILD = """
import asyncio,faulthandler,json,pathlib,sys,time
from datetime import UTC,datetime
from uuid import UUID
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher
from app.modules.reviews.application.try_enqueue_webhook_run import PendingRunMessage
phase=pathlib.Path(sys.argv[3]); records=[]
def emit(name,**values):
    records.append({"phase":name,**values})
    phase.write_text(json.dumps(records))
async def scenario():
    url="amqp://synthetic:synthetic@127.0.0.1:"+sys.argv[2]+"/unreachable"
    publisher=LazyAmqpPublisher(url)
    message=PendingRunMessage(
        UUID(int=1),UUID(int=2),17,UUID(int=3),17,"team/project",12,
        "a"*40,"b"*40,"main","fast",UUID(int=4),UUID(int=5),1,
        datetime.now(UTC),provider="gitlab",
    )
    emit("publication_started")
    task=asyncio.create_task(publisher.publish_confirmed(message))
    try:
        accepted=pathlib.Path(sys.argv[4])
        until=time.monotonic()+2
        while not accepted.exists():
            if task.done(): await task
            if time.monotonic()>until: raise AssertionError("TCP fixture never accepted")
            await asyncio.sleep(0.01)
        emit("tcp_handshake_held")
        if sys.argv[1]=="timeout":
            try: await asyncio.wait_for(task,0.2)
            except TimeoutError: emit("unavailable",error_type="TimeoutError")
            else: raise AssertionError("Stalled AMQP handshake completed publication")
        else:
            task.cancel()
            try: await task
            except asyncio.CancelledError: emit("cancelled")
            else: raise AssertionError("Initial publication was not cancelled")
    finally:
        await asyncio.wait_for(publisher.aclose(),1)
        emit("public_close_returned")
faulthandler.enable()
emit("bootstrap_ready")
faulthandler.dump_traceback_later(3,repeat=False)
asyncio.run(scenario())
faulthandler.cancel_dump_traceback_later()
emit("normal_process_exit")
"""


@contextmanager
def stalled_handshake_peer(tmp_path: Path) -> Iterator[tuple[int, Path, list[socket.socket]]]:
    """Real loopback TCP accepts and holds the AMQP handshake; no broker substitute."""
    accepted: list[socket.socket] = []
    marker = tmp_path / "tcp_accepted"
    stopped = threading.Event()
    failures: list[str] = []
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(0.05)

        def accept_connections() -> None:
            while not stopped.is_set():
                try:
                    peer, _ = listener.accept()
                except TimeoutError:
                    continue
                except OSError:
                    if not stopped.is_set():
                        failures.append("accept_failed")
                    return
                accepted.append(peer)
                marker.write_text("accepted", encoding="utf-8")

        thread = threading.Thread(target=accept_connections, name="owned-amqp-handshake")
        thread.start()
        try:
            yield listener.getsockname()[1], marker, accepted
        finally:
            stopped.set()
            listener.close()
            thread.join(timeout=2)
            for peer in accepted:
                peer.close()
            assert not thread.is_alive(), "Owned handshake thread did not stop"
            assert failures == []


@pytest.mark.parametrize("mode", ["timeout", "cancel"])
def test_public_lazy_initial_connect_failure_closes_before_process_exit(
    tmp_path: Path,
    mode: str,
) -> None:
    child_source = tmp_path / "lazy_lifecycle_child.py"
    child_source.write_text(_CHILD, encoding="utf-8")
    phase_path, log_path = tmp_path / "phases.json", tmp_path / "child.log"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
    for name in ("TEST_DATABASE_URL", "TEST_RABBITMQ_URL", "RABBITMQ_URL"):
        environment.pop(name, None)
    signals: list[str] = []
    normal_deadline_met = False
    ready = False
    with (
        stalled_handshake_peer(tmp_path) as (port, accepted_marker, accepted),
        log_path.open("w", encoding="utf-8") as log,
    ):
        process = subprocess.Popen(
            [
                sys.executable,
                str(child_source),
                mode,
                str(port),
                str(phase_path),
                str(accepted_marker),
            ],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            # Bootstrap is separate from the five-second behavioral deadline.
            bootstrap_until = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < bootstrap_until:
                if phase_path.exists():
                    try:
                        phases = json.loads(phase_path.read_text())
                    except json.JSONDecodeError:
                        phases = []
                    if phases and phases[0]["phase"] == "bootstrap_ready":
                        ready = True
                        break
                time.sleep(0.01)
            if ready:
                try:
                    process.wait(timeout=5)
                    normal_deadline_met = True
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                signals.append("SIGINT")
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.terminate()
                    signals.append("SIGTERM")
                    process.wait(timeout=2)
    phases = json.loads(phase_path.read_text()) if phase_path.exists() else []
    outcome: dict[str, Any] = {
        "bootstrap_ready": ready,
        "normal_deadline_met": normal_deadline_met,
        "child_pid": process.pid,
        "child_exit": process.returncode,
        "signals": signals,
        "phases": phases,
        "accepted_connections": len(accepted),
    }
    (tmp_path / "outcome.json").write_text(json.dumps(outcome, indent=2) + "\n")
    assert len(accepted) > 0, outcome
    assert ready, "Child bootstrap failed before any behavioral evidence: " + log_path.read_text()
    assert any(p["phase"] == "public_close_returned" for p in phases), outcome
    assert normal_deadline_met and process.returncode == 0, outcome
    assert phases[-1]["phase"] == "normal_process_exit", outcome
    assert signals == []


@pytest.mark.parametrize("failure", ["error", "cancel", "channel"])
def test_public_channels_close_owned_connection_on_startup_error_and_cancellation(
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    observed: list[str] = []

    class FailureConnection:
        def __init__(self, url: object) -> None:
            assert str(url) == "amqp://synthetic:synthetic@localhost/"

        async def connect(self) -> None:
            observed.append("connect")
            if failure == "error":
                raise RuntimeError("synthetic_connect_failure")
            if failure == "cancel":
                raise asyncio.CancelledError()

        async def channel(self, **kwargs: object) -> None:
            observed.append("channel")
            raise RuntimeError("synthetic_channel_failure")

        async def close(self) -> None:
            observed.append("close")

    monkeypatch.setattr(aio_pika, "RobustConnection", FailureConnection)

    async def scenario() -> None:
        async with amqp.amqp_channels("amqp://synthetic:synthetic@localhost/", RetryDelays()):
            raise AssertionError("startup failure cannot enter the body")

    expected = asyncio.CancelledError if failure == "cancel" else RuntimeError
    with pytest.raises(expected):
        asyncio.run(scenario())
    assert observed == (
        ["connect", "channel", "close"] if failure == "channel" else ["connect", "close"]
    )


_QUEUES = (
    "reviews.dlq",
    "review.publish",
    "review.run.fast",
    "review.run.deep",
    "retry.30s.fast",
    "retry.2m.fast",
    "retry.10m.fast",
    "retry.30s.deep",
    "retry.2m.deep",
    "retry.10m.deep",
)
_EXCHANGES = ("reviews", "reviews.retry", "reviews.dlx")


@pytest.fixture
def lifecycle_broker_url() -> str:
    ci = os.environ.get("GITHUB_ACTIONS") == "true"
    url = os.environ.get("TEST_RABBITMQ_URL") or (os.environ.get("RABBITMQ_URL") if ci else None)
    if url is None:
        pytest.skip("set TEST_RABBITMQ_URL to an exclusive disposable RabbitMQ vhost")
    parsed = urlsplit(url)
    if not ci and (parsed.hostname, parsed.port, parsed.path) != (
        "127.0.0.1",
        58447,
        "/dmc169-test",
    ):
        pytest.fail("Lifecycle transport proof requires the reserved disposable RabbitMQ vhost")
    return url


async def remove_owned_topology(channel: AbstractChannel) -> None:
    for name in _QUEUES:
        await asyncio.wait_for(channel.queue_delete(name), 5)
    for name in _EXCHANGES:
        await asyncio.wait_for(channel.exchange_delete(name), 5)


@pytest.mark.integration
def test_established_robust_reconnect_restores_same_confirmed_github_publisher(
    lifecycle_broker_url: str,
    tmp_path: Path,
) -> None:
    pre_io_retries: list[int] = []
    wire_ids: list[str] = []

    async def scenario() -> None:
        consumer = await asyncio.wait_for(aio_pika.connect(lifecycle_broker_url, timeout=3), 5)
        try:
            channel = await asyncio.wait_for(consumer.channel(), 5)
            await remove_owned_topology(channel)
            try:
                async with amqp.amqp_channels(lifecycle_broker_url, RetryDelays()) as channels:
                    publisher = channels.publisher
                    for run_number in (101, 102):
                        message = PendingRunMessage(
                            UUID(int=run_number),
                            UUID(int=2),
                            17,
                            UUID(int=3),
                            17,
                            "octo/repo",
                            12,
                            "a" * 40,
                            "b" * 40,
                            "main",
                            "fast",
                            UUID(int=4),
                            UUID(int=5),
                            1,
                            datetime(2026, 10, 19, 12, tzinfo=UTC),
                        )
                        retries = 0
                        # Robust reconnect restores channels asynchronously. Only the
                        # client's pre-I/O closed-channel refusal is safe to retry here.
                        async with asyncio.timeout(5):
                            while True:
                                try:
                                    await publisher.publish_confirmed(message)
                                    break
                                except ChannelInvalidStateError:
                                    retries += 1
                                    await asyncio.sleep(0.01)
                        pre_io_retries.append(retries)
                        queue = await channel.get_queue("review.run.fast", ensure=False)
                        incoming = await asyncio.wait_for(queue.get(timeout=3), 5)
                        assert incoming is not None
                        body = json.loads(incoming.body)
                        wire_ids.append(body["run_id"])
                        amqp.validate_message("review.run.v1", body)
                        assert body["message_id"] == body["run_id"] == str(UUID(int=run_number))
                        assert body["repo"] == {
                            "id": str(UUID(int=3)),
                            "provider": "github",
                            "external_id": 17,
                            "full_name": "octo/repo",
                        }
                        assert body["pr"] == {
                            "number": 12,
                            "head_sha": "a" * 40,
                            "base_sha": "b" * 40,
                            "base_ref": "main",
                        }
                        assert (body["trigger"], body["attempt"], body["engine"]) == (
                            "webhook",
                            1,
                            "fast",
                        )
                        assert body["workspace_id"] == str(UUID(int=2))
                        assert body["installation_id"] == 17
                        assert body["requested_at"] == "2026-10-19T12:00:00Z"
                        assert incoming.message_id == str(UUID(int=run_number))
                        assert incoming.routing_key == "review.run.fast"
                        assert incoming.content_type == "application/json"
                        assert (incoming.priority or 0, int(incoming.delivery_mode or 0)) == (0, 2)
                        await asyncio.wait_for(incoming.ack(), 5)
                        if run_number == 101:
                            await asyncio.wait_for(channels.connection.reconnect(), 5)
                    assert channels.publisher is publisher
                    assert await queue.get(fail=False, timeout=1) is None
            finally:
                await remove_owned_topology(channel)
        finally:
            await asyncio.wait_for(consumer.close(), 5)

    asyncio.run(asyncio.wait_for(scenario(), 30))
    (tmp_path / "reconnect-outcome.json").write_text(
        json.dumps(
            {
                "pre_io_closed_channel_retries": pre_io_retries,
                "wire_run_ids": wire_ids,
                "same_publisher": True,
            },
            indent=2,
        )
        + "\n"
    )
