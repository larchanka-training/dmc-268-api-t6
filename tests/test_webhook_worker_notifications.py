"""The webhook sweep consumes queue hints without losing races or its fallback (#129)."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace, TracebackType
from typing import Literal, Self, cast

import httpx
import psycopg
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import NullPool

from app import webhook_worker
from app.bootstrap.reviews_api import ReviewsApiResources
from app.common.infrastructure.heartbeat import is_fresh
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    ReplayBatchResult,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.repositories.application.rules_status import (
    RULES_REFRESH_BATCH,
    RefreshRulesStatuses,
)
from app.webhook_worker import sweep_forever


def test_bitbucket_only_worker_composes_rules_refresher() -> None:
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    config = webhook_worker.WorkerConfig(
        database_url="postgresql://unused",
        app_id=None,
        private_key=None,
        bot_login=None,
        github_api_url="https://api.github.com",
        rabbitmq_url="amqp://unused",
        bitbucket=cipher,
    )
    calls: list[dict[str, object]] = []
    marker = object()

    def record_rules(**kwargs: object) -> object:
        calls.append(kwargs)
        return marker

    resources = SimpleNamespace(rules_status_refresher=record_rules)

    async def scenario() -> None:
        async with httpx.AsyncClient(trust_env=False) as bitbucket_client:
            rules = webhook_worker.compose_rules_refresher(
                cast("ReviewsApiResources", resources),
                None,
                None,
                config,
                bitbucket_client=bitbucket_client,
            )
            assert rules is marker
            assert calls == [
                {
                    "client": None,
                    "token_provider": None,
                    "bitbucket_cipher": cipher,
                    "bitbucket_client": bitbucket_client,
                }
            ]

    asyncio.run(scenario())


class Wake(asyncio.Event):
    """Observe the public wait boundary without sleeps or the loop's internal state."""

    def __init__(self) -> None:
        super().__init__()
        self.waiting: asyncio.Queue[None] = asyncio.Queue()

    async def wait(self) -> Literal[True]:
        self.waiting.put_nowait(None)
        return await super().wait()


class Receiver:
    def __init__(self, *, first_batch: int = 0) -> None:
        self.first_batch = first_batch
        self.count = 0
        self.scans: asyncio.Queue[int] = asyncio.Queue()
        self.purges = 0

    async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
        self.count += 1
        self.scans.put_nowait(self.count)
        handled = self.first_batch if self.count == 1 else 0
        return ReplayBatchResult(handled, handled, 0, limit)

    async def purge_finished(self) -> int:
        self.purges += 1
        return 0


class Reviver:
    def __init__(self) -> None:
        self.calls = 0

    async def execute(self) -> int:
        self.calls += 1
        return 0


async def cancel(task: asyncio.Task[object]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_startup_scan_and_idle_notification_happen_before_polling_deadline() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert receiver.purges == reviver.calls == 1
            wake.set()
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_signal_during_dispatch_survives_transition_back_to_waiting() -> None:
    async def scenario() -> None:
        wake, entered, release = Wake(), asyncio.Event(), asyncio.Event()
        wake.set()  # A pre-start hint must be consumed by the immediate scan.

        class BusyReceiver(Receiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                assert wake.is_set() is False
                if self.count == 0:
                    entered.set()
                    await release.wait()
                return await super().replay_batch(limit=limit)

        receiver = BusyReceiver()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                wake,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            wake.set()
            release.set()
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_fallback_timeout_scans_again_without_a_notification() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                poll_interval=0.02,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert wake.is_set() is False
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            assert receiver.purges == reviver.calls == 1
        finally:
            await cancel(task)

    asyncio.run(scenario())


@pytest.mark.parametrize(("batch", "continues"), [(99, False), (100, True), (101, False)])
def test_only_exact_full_batch_continues_immediately_and_yields_to_other_tasks(
    batch: int, continues: bool
) -> None:
    async def scenario() -> None:
        wake, other_task_ran = Wake(), asyncio.Event()

        class FairReceiver(Receiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                if self.count == 1:
                    assert other_task_ran.is_set() is True
                return await super().replay_batch(limit=limit)

        receiver = FairReceiver(first_batch=batch)
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                wake,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            other_task_ran.set()
            if continues:
                assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
            else:
                await asyncio.wait_for(wake.waiting.get(), timeout=1)
                assert receiver.count == 1
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_frequent_notifications_keep_hourly_maintenance_on_monotonic_schedule(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        clock = [100.0]
        wake = Wake()
        purges: list[float] = []
        revivals: list[float] = []
        monkeypatch.setattr(webhook_worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        class NoisyReceiver(Receiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                clock[0] = [100.0, 101.0, 3699.0, 3700.0, 3701.0][self.count]
                if self.count < 4:
                    wake.set()
                return await super().replay_batch(limit=limit)

            async def purge_finished(self) -> int:
                purges.append(clock[0])
                return 0

        class TimedReviver(Reviver):
            async def execute(self) -> int:
                revivals.append(clock[0])
                return 0

        receiver = NoisyReceiver()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, TimedReviver()),
                wake,
            )
        )
        try:
            for expected in range(1, 6):
                assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == expected
            assert purges == [100.0, 3700.0]
            assert revivals == [100.0, 3700.0]
        finally:
            await cancel(task)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("provider", "close_failure"),
    [
        ("github", False),
        ("gitlab", False),
        ("gitlab", True),
        ("bitbucket", False),
        ("both", False),
    ],
)
def test_worker_starts_scan_and_heartbeat_before_listener_ready_and_awaits_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    provider: str,
    close_failure: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        database_attempted = asyncio.Event()
        listening, register, receiving = asyncio.Event(), asyncio.Event(), asyncio.Event()
        closing, finish_close = asyncio.Event(), asyncio.Event()
        closed: list[str] = []
        expected_before_publisher = ["listener", "lock"] if provider == "gitlab" else ["listener"]
        clients: list[dict[str, object]] = []
        created_clients: list[httpx.AsyncClient] = []
        rules_calls: list[dict[str, object]] = []
        real_client = httpx.AsyncClient
        real_rules = ReviewsApiResources.rules_status_refresher

        def rules_refresher(self: ReviewsApiResources, **kwargs: object) -> RefreshRulesStatuses:
            rules_calls.append(kwargs)
            return real_rules(self, **kwargs)  # type: ignore[arg-type]

        def http_client(**kwargs: object) -> httpx.AsyncClient:
            clients.append(kwargs)
            created = real_client(**kwargs)  # type: ignore[arg-type]
            created_clients.append(created)
            return created

        monkeypatch.setattr(httpx, "AsyncClient", http_client)
        monkeypatch.setattr(ReviewsApiResources, "rules_status_refresher", rules_refresher)
        listener_connections: list[str] = []
        heartbeat = tmp_path / "webhook-worker.heartbeat"
        messages: asyncio.Queue[psycopg.Notify] = asyncio.Queue()

        class ListenerConnection:
            async def __aenter__(self) -> Self:
                return self

            async def __aexit__(
                self,
                exc_type: type[BaseException] | None,
                exc: BaseException | None,
                traceback: TracebackType | None,
            ) -> None:
                closing.set()
                await finish_close.wait()
                closed.append("listener")

            async def execute(self, statement: str) -> None:
                assert statement == "LISTEN webhook_work_available"
                listening.set()
                await register.wait()

            async def notifies(self) -> AsyncIterator[psycopg.Notify]:
                receiving.set()
                while True:
                    yield await messages.get()

        async def connect(conninfo: str = "", **kwargs: object) -> ListenerConnection:
            if kwargs.get("autocommit") is True:
                listener_connections.append(conninfo)
                return ListenerConnection()
            database_attempted.set()
            raise psycopg.OperationalError("synthetic queue database outage")

        class Publisher:
            def __init__(self, url: str) -> None:
                assert url == "amqp://synthetic-broker/"

            async def aclose(self) -> None:
                assert closed == expected_before_publisher
                closed.append("publisher")

        real_dispose = AsyncEngine.dispose

        async def dispose(engine: AsyncEngine, close: bool = True) -> None:
            if isinstance(engine.pool, NullPool):
                assert closed == ["listener"]
                await real_dispose(engine, close=close)
                closed.append("lock")
            else:
                assert closed == [*expected_before_publisher, "publisher"]
                await real_dispose(engine, close=close)
                closed.append("database")
                if close_failure:
                    raise RuntimeError("data-close-PAT-URL-canary")

        monkeypatch.setattr(psycopg.AsyncConnection, "connect", connect)
        monkeypatch.setattr(webhook_worker, "LazyAmqpPublisher", Publisher)
        monkeypatch.setattr(AsyncEngine, "dispose", dispose)
        for name in (
            "GITHUB_APP_ID",
            "GITHUB_APP_PRIVATE_KEY",
            "GITHUB_APP_BOT_LOGIN",
            "GITLAB_CREDENTIAL_KEYS",
            "GITLAB_ACTIVE_KEY_ID",
            "GITLAB_ALLOWED_ORIGINS",
            "GITLAB_PRIVATE_ORIGINS",
            "BITBUCKET_CREDENTIAL_KEYS",
            "BITBUCKET_ACTIVE_KEY_ID",
        ):
            monkeypatch.delenv(name, raising=False)
        environment = {
            "DATABASE_URL": "postgresql+psycopg://app:synthetic@127.0.0.1:1/app",
            "GITHUB_APP_ID": "17",
            "GITHUB_APP_PRIVATE_KEY": "synthetic-private-key",
            "GITHUB_APP_BOT_LOGIN": "reviewer[bot]",
            "RABBITMQ_URL": "amqp://synthetic-broker/",
            "WORKER_HEARTBEAT_FILE": str(heartbeat),
        }
        if provider == "gitlab":
            import base64

            for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_BOT_LOGIN"):
                del environment[name]
            environment.update(
                GITLAB_CREDENTIAL_KEYS='{"test-key":"'
                + base64.b64encode(b"k" * 32).decode()
                + '"}',
                GITLAB_ACTIVE_KEY_ID="test-key",
                GITLAB_ALLOWED_ORIGINS='["https://gitlab.com"]',
            )
        if provider in {"bitbucket", "both"}:
            import base64

            if provider == "bitbucket":
                monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "missing-ca.pem"))
                for name in ("GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_BOT_LOGIN"):
                    del environment[name]
            environment.update(
                BITBUCKET_CREDENTIAL_KEYS='{"test":"' + base64.b64encode(b"a" * 32).decode() + '"}',
                BITBUCKET_ACTIVE_KEY_ID="test",
            )
        for name, value in environment.items():
            monkeypatch.setenv(name, value)
        task = asyncio.create_task(webhook_worker.run_forever())
        try:
            await asyncio.wait_for(listening.wait(), timeout=2)
            await asyncio.wait_for(database_attempted.wait(), timeout=2)
            assert is_fresh(heartbeat, max_age=5, now=time.time()) is True
            assert task.done() is False
            register.set()
            await asyncio.wait_for(receiving.wait(), timeout=2)
            for _ in range(3):
                messages.put_nowait(psycopg.Notify("webhook_work_available", "", 123))
            assert len(listener_connections) == 1
            task.cancel()
            await asyncio.wait_for(closing.wait(), timeout=2)
            assert closed == []
            finish_close.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=2)
            assert closed == [*expected_before_publisher, "publisher", "database"]
            assert len(clients) == {"github": 1, "gitlab": 1, "bitbucket": 2, "both": 3}[provider]
            if provider == "gitlab":
                assert "base_url" not in clients[0]  # no GitHub client/token/reviver composition
                assert clients[0]["trust_env"] is False
            elif provider in {"github", "both"}:
                assert clients[0]["base_url"] == "https://api.github.com"
                assert clients[0].get("trust_env", True) is True
            if provider in {"bitbucket", "both"}:
                rules_client_index = 0 if provider == "bitbucket" else 1
                assert "base_url" not in clients[rules_client_index]
                assert clients[rules_client_index]["trust_env"] is False
                assert clients[rules_client_index]["follow_redirects"] is False
                assert len(rules_calls) == 1
                assert (rules_calls[0]["token_provider"] is None) == (provider == "bitbucket")
                assert isinstance(rules_calls[0]["bitbucket_cipher"], BitbucketCredentialCipher)
                assert rules_calls[0]["client"] is (
                    None if provider == "bitbucket" else created_clients[0]
                )
                assert rules_calls[0]["bitbucket_client"] is created_clients[rules_client_index]
        finally:
            finish_close.set()
            if not task.done():
                await cancel(task)

    asyncio.run(scenario())
    if close_failure:
        records = [record for record in caplog.records if record.name == "app.webhook_worker"]
        assert any(
            record.getMessage() == "Webhook worker resource cleanup failed" for record in records
        )
        assert all(record.exc_info is None for record in records)
        assert "data-close-PAT-URL-canary" not in caplog.text


class RulesRefresher:
    """Counts the passes of the rules status sweep (#150); the first may fill a batch."""

    def __init__(self, *, first_batch: int = 0, fail: bool = False) -> None:
        self.first_batch = first_batch
        self.fail = fail
        self.passes: asyncio.Queue[int] = asyncio.Queue()
        self.count = 0

    async def execute(self) -> int:
        self.count += 1
        self.passes.put_nowait(self.count)
        if self.fail:
            raise RuntimeError("rules status store down")
        return self.first_batch if self.count == 1 else 0


def test_rules_status_requests_are_answered_on_startup_and_on_each_hint() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        rules = RulesRefresher()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                rules=cast(RefreshRulesStatuses, rules),
            )
        )
        try:
            # A request written while the worker was down is answered by the first pass.
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 1
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            wake.set()
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 2
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_a_full_rules_status_batch_runs_the_next_pass_without_waiting() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        rules = RulesRefresher(first_batch=RULES_REFRESH_BATCH)
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                rules=cast(RefreshRulesStatuses, rules),
            )
        )
        try:
            # The first wait for a hint comes only after the pass that found the batch short.
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert rules.count == 2
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_a_failed_rules_status_pass_does_not_stop_the_webhook_sweep(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        rules = RulesRefresher(fail=True)
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                rules=cast(RefreshRulesStatuses, rules),
            )
        )
        try:
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            wake.set()
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 1
            assert await asyncio.wait_for(receiver.scans.get(), timeout=1) == 2
        finally:
            await cancel(task)

    asyncio.run(scenario())
    assert "Rules status refresh sweep failed" in caplog.text


def test_rules_status_requests_are_answered_on_the_poll_timeout_without_a_hint() -> None:
    async def scenario() -> None:
        receiver, reviver, wake = Receiver(), Reviver(), Wake()
        rules = RulesRefresher()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, reviver),
                wake,
                poll_interval=0.01,
                rules=cast(RefreshRulesStatuses, rules),
            )
        )
        try:
            # A lost NOTIFY only delays the request until the next poll.
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 1
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 2
            assert not wake.is_set()
        finally:
            await cancel(task)

    asyncio.run(scenario())


def test_mixed_provider_round_keeps_rules_requests_and_both_receipts_moving() -> None:
    async def scenario() -> None:
        github, gitlab, wake = Receiver(), Receiver(), Wake()
        rules = RulesRefresher(first_batch=RULES_REFRESH_BATCH)
        task = asyncio.create_task(
            webhook_worker.sweep_providers_forever(
                (
                    webhook_worker.WebhookReplayProvider("github", github),
                    webhook_worker.WebhookReplayProvider("gitlab", gitlab),
                ),
                wake,
                rules=cast(RefreshRulesStatuses, rules),
            )
        )
        try:
            # A full rules batch schedules another complete provider round immediately.
            await asyncio.wait_for(wake.waiting.get(), timeout=1)
            assert github.count == gitlab.count == rules.count == 2
            assert github.purges == gitlab.purges == 1
            wake.set()
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 1
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 2
            assert await asyncio.wait_for(rules.passes.get(), timeout=1) == 3
            assert github.count == gitlab.count == 3
        finally:
            await cancel(task)

    asyncio.run(scenario())
