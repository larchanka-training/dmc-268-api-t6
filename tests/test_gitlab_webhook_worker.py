"""Public GitLab replay runtime, fair provider scheduling and owned lifetimes."""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Literal, Self, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.gitlab import gitlab_webhook_replay_resources
from app.modules.integrations.gitlab.infrastructure.settings import GitLabRuntimeSettings
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    ReplayBatchResult,
)
from app.modules.integrations.webhooks.application.revive_deferred_installation_deliveries import (
    ReviveDeferredInstallationDeliveries,
)
from app.modules.reviews.application.try_enqueue_webhook_run import RunMessagePublisher
from app.webhook_worker import WorkerConfig, revive_once, sweep_forever
from tests import test_gitlab_projection_postgres as projection


class BatchReceiver:
    def __init__(self, results: list[ReplayBatchResult]) -> None:
        self.results = results
        self.count = 0
        self.scans: asyncio.Queue[int] = asyncio.Queue()
        self.purges = 0

    async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
        self.count += 1
        self.scans.put_nowait(self.count)
        return self.results[min(self.count - 1, len(self.results) - 1)]

    async def replay_pending(self) -> int:
        return (await self.replay_batch()).handled_count

    async def purge_finished(self) -> int:
        self.purges += 1
        return 0


class Reviver:
    async def execute(self) -> int:
        return 0


class Wake(asyncio.Event):
    def __init__(self) -> None:
        super().__init__()
        self.waiting: asyncio.Queue[None] = asyncio.Queue()

    async def wait(self) -> Literal[True]:
        self.waiting.put_nowait(None)
        return await super().wait()


async def stop(task: asyncio.Task[object]) -> None:
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_absent_or_empty_github_group_is_supported() -> None:
    for values in ({}, {"GITHUB_APP_ID": "", "GITHUB_APP_PRIVATE_KEY": ""}):
        config = WorkerConfig.from_environment(
            {
                "DATABASE_URL": "postgresql+psycopg://synthetic@localhost/unused",
                "RABBITMQ_URL": "amqp://synthetic/",
                **values,
            }
        )
        assert config.app_id is None
        assert config.private_key is None
        assert config.bot_login is None


def test_full_scan_with_zero_handled_visits_remainder_before_fallback() -> None:
    async def scenario() -> None:
        receiver = BatchReceiver(
            [ReplayBatchResult(100, 0, 0, 100), ReplayBatchResult(1, 0, 0, 100)]
        )
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                Wake(),
                poll_interval=10,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), 1) == 1
            assert await asyncio.wait_for(receiver.scans.get(), 0.1) == 2
        finally:
            await stop(task)

    asyncio.run(scenario())


def test_legal_mixed_error_full_batch_cools_despite_repeated_hints() -> None:
    async def scenario() -> None:
        # Real engine invariant: handled + errors <= scanned. This is a legal batch.
        receiver, wake = BatchReceiver([ReplayBatchResult(100, 99, 1, 100)]), Wake()
        task = asyncio.create_task(
            sweep_forever(
                cast(ReceiveGitHubDelivery, receiver),
                cast(ReviveDeferredInstallationDeliveries, Reviver()),
                wake,
                poll_interval=10,
            )
        )
        try:
            assert await asyncio.wait_for(receiver.scans.get(), 1) == 1
            await asyncio.wait_for(wake.waiting.get(), 1)
            for _ in range(3):
                wake.set()
                await asyncio.wait_for(wake.waiting.get(), 1)
                assert receiver.count == 1
        finally:
            await stop(task)

    asyncio.run(scenario())


def test_changed_revival_catch_never_renders_secret_exception(
    caplog: pytest.LogCaptureFixture,
) -> None:
    class Failing:
        async def execute(self) -> int:
            raise RuntimeError("PAT-and-SQL-and-URL-canary")

    with caplog.at_level(logging.ERROR, logger="app.webhook_worker"):
        assert asyncio.run(revive_once(cast(ReviveDeferredInstallationDeliveries, Failing()))) == 0
    [record] = [entry for entry in caplog.records if entry.name == "app.webhook_worker"]
    assert record.exc_info is None
    assert "canary" not in caplog.text


replay_database = projection.authority_database


def configured_environment() -> dict[str, str]:
    return {
        "DATABASE_URL": "postgresql+psycopg://synthetic@localhost/unused",
        "RABBITMQ_URL": "amqp://synthetic/",
        "GITLAB_CREDENTIAL_KEYS": '{"test-key":"' + base64.b64encode(b"k" * 32).decode() + '"}',
        "GITLAB_ACTIVE_KEY_ID": "test-key",
        "GITLAB_ALLOWED_ORIGINS": '["https://gitlab.com","https://other.gitlab.example"]',
    }


class OwnedTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.entered = False
        self.closed = False

    async def __aenter__(self) -> Self:
        self.entered = True
        return self

    async def aclose(self) -> None:
        self.closed = True

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise AssertionError("Unexpected external HTTP")


def test_public_replay_context_owns_injected_http_transport() -> None:
    async def scenario() -> None:
        transport = OwnedTransport()
        settings = GitLabRuntimeSettings.from_environment(configured_environment())
        async with gitlab_webhook_replay_resources(
            async_sessionmaker[AsyncSession](),
            settings=settings,
            run_publisher=cast(RunMessagePublisher, object()),
            transport=transport,
        ):
            assert transport.entered is True
            assert transport.closed is False
        assert transport.closed is True

    asyncio.run(scenario())


@pytest.mark.integration
def test_public_replay_context_projects_and_confirms_one_origin_pinned_run(
    replay_database: tuple[str, str, UUID],
) -> None:
    async def scenario() -> None:
        async with projection.rig(replay_database) as fixture:
            target = await fixture.seed()
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            publisher = projection.ConfirmedPointers([])
            transport = httpx.MockTransport(
                lambda request: projection.automatic_response(request, projection.canonical(target))
            )
            async with gitlab_webhook_replay_resources(
                fixture.sessions,
                settings=GitLabRuntimeSettings.from_environment(environment),
                run_publisher=publisher,
                connect_args={"options": f"-csearch_path={replay_database[1]}"},
                transport=transport,
                now=lambda: projection.NOW,
            ) as resources:
                await resources.receiver.execute(projection.receipt_for(target))
                assert publisher.messages == []
                result = await resources.receiver.replay_batch()
                assert result == ReplayBatchResult(1, 1, 0, 100)
                assert len(publisher.messages) == 1
                message = publisher.messages[0]
                assert (
                    message.provider,
                    message.workspace_id,
                    message.repository_id,
                    message.repository_external_id,
                    message.head_sha,
                    message.base_sha,
                ) == (
                    "gitlab",
                    target.workspace_id,
                    target.repository_id,
                    17,
                    "a" * 40,
                    "b" * 40,
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "field", ["GITHUB_APP_ID", "GITHUB_APP_PRIVATE_KEY", "GITHUB_APP_BOT_LOGIN"]
)
def test_partial_github_group_fails_with_static_actionable_error(field: str) -> None:
    environment = {
        "DATABASE_URL": "synthetic-db",
        "RABBITMQ_URL": "synthetic-broker",
        field: "canary",
    }
    with pytest.raises(RuntimeError) as caught:
        WorkerConfig.from_environment(environment)
    assert str(caught.value) == (
        "GITHUB_APP_ID, GITHUB_APP_PRIVATE_KEY and GITHUB_APP_BOT_LOGIN must be configured together"
    )
    assert "canary" not in str(caught.value)


@pytest.mark.parametrize("app_id", ["0", "-1", "ID-canary", " "])
def test_invalid_complete_github_id_has_no_rendered_cause(app_id: str) -> None:
    import traceback

    environment = configured_environment()
    environment.update(
        GITHUB_APP_ID=app_id, GITHUB_APP_PRIVATE_KEY="key-canary", GITHUB_APP_BOT_LOGIN="bot"
    )
    with pytest.raises(RuntimeError) as caught:
        WorkerConfig.from_environment(environment)
    assert str(caught.value) == "GITHUB_APP_ID must be a positive integer"
    rendered = "".join(traceback.format_exception(caught.value))
    assert "ID-canary" not in rendered and "key-canary" not in rendered


@pytest.mark.parametrize("ca", [None, "", "/missing/CA-canary", "\x00canary"])
def test_ca_and_legacy_fields_alone_never_enable_gitlab_or_fail_partial(ca: str | None) -> None:
    environment = {
        "DATABASE_URL": "synthetic-db",
        "RABBITMQ_URL": "synthetic-broker",
        "GITLAB_TOKEN": "legacy-PAT-canary",
        "GITLAB_BASE_URL": "https://legacy-canary/",
    }
    if ca is not None:
        environment["SSL_CERT_FILE"] = ca
    config = WorkerConfig.from_environment(environment)
    assert config.gitlab is None and config.github_configured is False
    assert "canary" not in repr(config)
    environment.update(
        GITHUB_APP_ID="17", GITHUB_APP_PRIVATE_KEY="private-key-canary", GITHUB_APP_BOT_LOGIN="bot"
    )
    assert WorkerConfig.from_environment(environment).github_configured is True


@pytest.mark.parametrize(
    "field",
    [
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
    ],
)
def test_partial_gitlab_group_including_private_only_fails_without_legacy_fallback(
    field: str,
) -> None:
    environment = {
        "DATABASE_URL": "synthetic-db",
        "RABBITMQ_URL": "synthetic-broker",
        field: "[]",
        "GITLAB_TOKEN": "legacy-canary",
        "GITLAB_BASE_URL": "https://gitlab.com",
    }
    with pytest.raises(RuntimeError) as caught:
        WorkerConfig.from_environment(environment)
    assert str(caught.value) == (
        "GITLAB_CREDENTIAL_KEYS, GITLAB_ACTIVE_KEY_ID and GITLAB_ALLOWED_ORIGINS "
        "must be configured together"
    )


@pytest.mark.parametrize(
    "field",
    [
        "GITLAB_CREDENTIAL_KEYS",
        "GITLAB_ACTIVE_KEY_ID",
        "GITLAB_ALLOWED_ORIGINS",
        "GITLAB_PRIVATE_ORIGINS",
        "SSL_CERT_FILE",
    ],
)
def test_malformed_complete_gitlab_configuration_is_static(field: str) -> None:
    import traceback

    environment = configured_environment()
    environment[field] = "value-canary" if field != "SSL_CERT_FILE" else ""
    with pytest.raises(RuntimeError) as caught:
        WorkerConfig.from_environment(environment)
    assert str(caught.value).startswith("GitLab webhook replay configuration is invalid; check ")
    assert "value-canary" not in "".join(traceback.format_exception(caught.value))


def test_complete_both_groups_keep_gitlab_private_origin_and_redact_key_material() -> None:
    environment = configured_environment()
    environment.update(
        GITHUB_APP_ID="17",
        GITHUB_APP_PRIVATE_KEY="private-key-canary",
        GITHUB_APP_BOT_LOGIN="bot",
        GITLAB_PRIVATE_ORIGINS='["https://other.gitlab.example"]',
    )
    config = WorkerConfig.from_environment(environment)
    assert config.github_configured is True and config.app_id == 17
    assert config.gitlab is not None
    assert config.gitlab.origin_policy.private_origins == frozenset(
        {"https://other.gitlab.example"}
    )
    assert "private-key-canary" not in repr(config)
    assert environment["GITLAB_CREDENTIAL_KEYS"] not in repr(config)


@pytest.mark.parametrize("healthy", ["github", "gitlab"])
@pytest.mark.parametrize("whole_query_fault", [False, True])
def test_healthy_provider_drains_while_peer_failure_is_bounded(
    healthy: str,
    whole_query_fault: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.webhook_worker import WebhookReplayProvider, sweep_providers_forever

    async def scenario() -> None:
        calls: list[str] = []
        wake = Wake()

        class Healthy(BatchReceiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                calls.append("healthy")
                return await super().replay_batch(limit=limit)

        class Failing(BatchReceiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                calls.append("failed")
                result = await super().replay_batch(limit=limit)
                if whole_query_fault:
                    raise RuntimeError("query-PAT-URL-SQL-canary")
                return result

        good = Healthy([ReplayBatchResult(100, 0, 0, 100)] * 2 + [ReplayBatchResult(1, 1, 0, 100)])
        bad = Failing([ReplayBatchResult(100, 99, 1, 100)])
        providers = (
            WebhookReplayProvider(cast(Literal["github", "gitlab"], healthy), good),
            WebhookReplayProvider("gitlab" if healthy == "github" else "github", bad),
        )
        task = asyncio.create_task(sweep_providers_forever(providers, wake, poll_interval=10))
        try:
            await asyncio.wait_for(wake.waiting.get(), 1)
            assert calls == ["healthy", "failed", "healthy", "healthy"]
            for _ in range(3):
                wake.set()
                await asyncio.wait_for(wake.waiting.get(), 1)
            assert bad.count == 1
        finally:
            await stop(task)

    with caplog.at_level(logging.ERROR, logger="app.webhook_worker"):
        asyncio.run(scenario())
    assert "canary" not in caplog.text
    assert all(
        record.exc_info is None for record in caplog.records if record.name == "app.webhook_worker"
    )


@pytest.mark.parametrize("during", [0, 1])
def test_round_clears_once_and_hints_during_either_provider_survive(during: int) -> None:
    from app.webhook_worker import WebhookReplayProvider, sweep_providers_forever

    async def scenario() -> None:
        entered, release = asyncio.Event(), asyncio.Event()

        class CountedWake(Wake):
            clears = 0

            def clear(self) -> None:
                self.clears += 1
                super().clear()

        wake = CountedWake()

        class Receiver(BatchReceiver):
            def __init__(self, index: int) -> None:
                super().__init__([ReplayBatchResult(0, 0, 0, 100)])
                self.index = index

            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                if self.count == 0:
                    assert wake.clears == 1
                    if self.index == during:
                        entered.set()
                        await release.wait()
                return await super().replay_batch(limit=limit)

        first, second = Receiver(0), Receiver(1)
        task = asyncio.create_task(
            sweep_providers_forever(
                (WebhookReplayProvider("github", first), WebhookReplayProvider("gitlab", second)),
                wake,
                poll_interval=10,
            )
        )
        try:
            await asyncio.wait_for(entered.wait(), 1)
            wake.set()
            release.set()
            for receiver in (first, second):
                assert await asyncio.wait_for(receiver.scans.get(), 1) == 1
                assert await asyncio.wait_for(receiver.scans.get(), 1) == 2
            assert wake.clears == 2
        finally:
            release.set()
            await stop(task)

    asyncio.run(scenario())


def test_busy_drain_maintains_each_provider_despite_purge_and_revival_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from types import SimpleNamespace

    from app import webhook_worker
    from app.webhook_worker import WebhookReplayProvider, sweep_providers_forever

    async def scenario() -> None:
        clock, calls, wake = [100.0], [], Wake()
        monkeypatch.setattr(webhook_worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))

        class Healthy(BatchReceiver):
            async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                clock[0] = [100.0, 3700.0, 3701.0][min(self.count, 2)]
                return await super().replay_batch(limit=limit)

            async def purge_finished(self) -> int:
                calls.append(("github-purge", clock[0]))
                raise RuntimeError("purge-secret-canary")

        class Other(BatchReceiver):
            async def purge_finished(self) -> int:
                calls.append(("gitlab-purge", clock[0]))
                return 0

        class FailedReviver:
            async def execute(self) -> int:
                calls.append(("github-revive", clock[0]))
                raise RuntimeError("revive-secret-canary")

        good = Healthy(
            [ReplayBatchResult(100, 100, 0, 100)] * 2 + [ReplayBatchResult(0, 0, 0, 100)]
        )
        other = Other([ReplayBatchResult(0, 0, 0, 100)])
        task = asyncio.create_task(
            sweep_providers_forever(
                (
                    WebhookReplayProvider(
                        "github", good, cast(ReviveDeferredInstallationDeliveries, FailedReviver())
                    ),
                    WebhookReplayProvider("gitlab", other),
                ),
                wake,
                poll_interval=10,
            )
        )
        try:
            await asyncio.wait_for(wake.waiting.get(), 1)
            assert calls == [
                ("github-purge", 100.0),
                ("github-revive", 100.0),
                ("gitlab-purge", 100.0),
                ("github-purge", 3700.0),
                ("github-revive", 3700.0),
                ("gitlab-purge", 3700.0),
            ]
        finally:
            await stop(task)

    with caplog.at_level(logging.ERROR, logger="app.webhook_worker"):
        asyncio.run(scenario())
    assert "canary" not in caplog.text
    assert all(
        record.exc_info is None for record in caplog.records if record.name == "app.webhook_worker"
    )


@pytest.mark.parametrize("body", ["none", "error", "cancel"])
def test_resource_close_faults_preserve_primary_and_close_owned_transport(
    body: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from app.bootstrap.gitlab import GitLabWebhookReplayUnavailable

    class CloseFault(OwnedTransport):
        async def aclose(self) -> None:
            await super().aclose()
            raise RuntimeError("close-PAT-URL-canary")

    async def scenario() -> None:
        transport = CloseFault()
        error = LookupError("primary body")
        expected = (
            asyncio.CancelledError
            if body == "cancel"
            else LookupError
            if body == "error"
            else GitLabWebhookReplayUnavailable
        )
        with pytest.raises(expected) as caught:
            async with gitlab_webhook_replay_resources(
                async_sessionmaker[AsyncSession](),
                settings=GitLabRuntimeSettings.from_environment(configured_environment()),
                run_publisher=cast(RunMessagePublisher, object()),
                transport=transport,
            ):
                if body == "cancel":
                    raise asyncio.CancelledError()
                if body == "error":
                    raise error
        if body == "error":
            assert caught.value is error
        assert transport.closed is True

    with caplog.at_level(logging.ERROR, logger="app.bootstrap.gitlab"):
        asyncio.run(scenario())
    assert "canary" not in caplog.text
    assert all(
        record.exc_info is None
        for record in caplog.records
        if record.name == "app.bootstrap.gitlab"
    )


def test_partial_http_startup_failure_closes_owned_transport_with_static_error() -> None:
    from app.bootstrap.gitlab import GitLabWebhookReplayUnavailable

    class StartFault(OwnedTransport):
        async def __aenter__(self) -> Self:
            await super().__aenter__()
            raise RuntimeError("startup-PAT-URL-canary")

    async def scenario() -> None:
        transport = StartFault()
        with pytest.raises(
            GitLabWebhookReplayUnavailable, match="^gitlab_webhook_replay_unavailable$"
        ) as caught:
            async with gitlab_webhook_replay_resources(
                async_sessionmaker[AsyncSession](),
                settings=GitLabRuntimeSettings.from_environment(configured_environment()),
                run_publisher=cast(RunMessagePublisher, object()),
                transport=transport,
            ):
                pytest.fail("startup must not yield")
        assert caught.value.__cause__ is None
        assert transport.entered is True and transport.closed is True

    asyncio.run(scenario())


async def assert_committed_pool_and_no_transaction(
    fixture: projection.Rig,
    database_url: str,
) -> None:
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine
    from sqlalchemy.pool import NullPool

    # Reusing the SAME pool_size1/max_overflow0 proves no nested data lease is held.
    async with fixture.sessions() as session:
        assert await session.scalar(text("SELECT 1")) == 1
    observer = create_async_engine(database_url, poolclass=NullPool, isolation_level="AUTOCOMMIT")
    try:
        async with observer.connect() as connection:
            assert (
                await connection.scalar(
                    text(
                        "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                        "AND backend_type='client backend' AND pid<>pg_backend_pid() "
                        "AND xact_start IS NOT NULL"
                    )
                )
                == 0
            )
    finally:
        await observer.dispose()


@pytest.mark.integration
@pytest.mark.parametrize("failed_confirm", [False, True])
def test_composed_replay_duplicate_and_outbox_recovery_keep_one_committed_run(
    replay_database: tuple[str, str, UUID],
    failed_confirm: bool,
) -> None:
    from sqlalchemy import text

    from app.modules.reviews.application.try_enqueue_webhook_run import (
        PendingRunMessage,
        RunPublicationKind,
    )

    async def scenario() -> None:
        async with (
            projection.rig(replay_database) as fixture,
            projection.lock_for(replay_database) as observer_lock,
        ):
            target = await fixture.seed()
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            messages: list[PendingRunMessage] = []
            attempts = 0
            requests: list[str] = []

            class Publisher:
                async def publish_confirmed(
                    self,
                    message: PendingRunMessage,
                    *,
                    kind: RunPublicationKind = RunPublicationKind.QUEUED,
                ) -> None:
                    nonlocal attempts
                    attempts += 1
                    await assert_committed_pool_and_no_transaction(fixture, replay_database[0])
                    async with fixture.sessions() as session:
                        assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                        assert (
                            await session.scalar(text("SELECT message_published_at FROM runs"))
                            is None
                        )
                    # Projection hold is released before the publisher port starts.
                    async with observer_lock.hold(projection.command_for(target)):
                        pass
                    if failed_confirm and attempts == 1:
                        raise RuntimeError("synthetic confirmation unavailable")
                    messages.append(message)

            data = projection.canonical(target)

            async def handle(request: httpx.Request) -> httpx.Response:
                requests.append(str(request.url))
                assert request.headers["PRIVATE-TOKEN"] == "synthetic-authority-PAT-canary"
                assert request.headers.get("Authorization") is None
                assert request.extensions["timeout"] == {
                    "connect": 10.0,
                    "read": 10.0,
                    "write": 10.0,
                    "pool": 10.0,
                }
                await assert_committed_pool_and_no_transaction(fixture, replay_database[0])
                return projection.automatic_response(request, data)

            async with gitlab_webhook_replay_resources(
                fixture.sessions,
                settings=GitLabRuntimeSettings.from_environment(environment),
                run_publisher=Publisher(),
                connect_args={"options": f"-csearch_path={replay_database[1]}"},
                transport=httpx.MockTransport(handle),
                now=lambda: projection.NOW,
            ) as resources:
                original = projection.receipt_for(target)
                await resources.receiver.execute(original)
                assert await resources.receiver.replay_batch() == ReplayBatchResult(1, 1, 0, 100)
                async with fixture.sessions() as session:
                    row = (
                        await session.execute(
                            text(
                                "SELECT c.head_sha,c.base_sha,c.state,"
                                "r.origin_provider_installation_id,"
                                "r.message_published_at IS NOT NULL FROM code_changes c "
                                "JOIN runs r ON r.code_change_id=c.id"
                            )
                        )
                    ).one()
                    assert tuple(row) == (
                        "a" * 40,
                        "b" * 40,
                        "open",
                        target.origin_provider_installation_id,
                        not failed_confirm,
                    )
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL"
                            )
                        )
                        == 1
                    )
                if failed_confirm:
                    assert messages == []
                    assert await resources.enqueuer.replay_pending_publications() == 1
                assert len(messages) == 1
                assert (
                    await resources.receiver.execute(original)
                    == InstallationDeliveryDispatchStatus.DUPLICATE
                )
                data["title"] = "Canonical metadata changed"
                await resources.receiver.execute(
                    projection.receipt_for(target, "metadata", "update")
                )
                assert await resources.receiver.replay_batch() == ReplayBatchResult(1, 1, 0, 100)
                assert len(messages) == 1
                assert len(requests) == 2
            # Borrowed data engine stays live after replay-context closure.
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 1
                assert (
                    await session.scalar(text("SELECT title FROM code_changes"))
                    == "Canonical metadata changed"
                )
                assert (
                    await session.scalar(
                        text("SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL")
                    )
                    == 2
                )
            message = messages[0]
            assert (
                message.provider,
                message.workspace_id,
                message.repository_id,
                message.repository_external_id,
                message.head_sha,
                message.base_sha,
                message.attempt,
            ) == (
                "gitlab",
                target.workspace_id,
                target.repository_id,
                17,
                "a" * 40,
                "b" * 40,
                1,
            )

    asyncio.run(asyncio.wait_for(scenario(), 20))


@pytest.mark.integration
def test_composed_replay_defers_new_head_until_old_run_finishes_without_new_projection(
    replay_database: tuple[str, str, UUID],
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    async def scenario() -> None:
        async with projection.rig(replay_database) as fixture:
            target, clock = await fixture.seed(), projection.Clock(datetime.now(UTC))
            data, publisher = projection.canonical(target), projection.ConfirmedPointers([])
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            async with gitlab_webhook_replay_resources(
                fixture.sessions,
                settings=GitLabRuntimeSettings.from_environment(environment),
                run_publisher=publisher,
                connect_args={"options": f"-csearch_path={replay_database[1]}"},
                transport=httpx.MockTransport(
                    lambda request: projection.automatic_response(request, data)
                ),
                now=clock,
            ) as resources:
                await resources.receiver.execute(projection.receipt_for(target))
                assert (await resources.receiver.replay_batch()).handled_count == 1
                old = publisher.messages[0].run_id
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE runs SET state='running' WHERE id=:id"), {"id": old}
                    )
                data["diff_refs"]["head_sha"] = "d" * 40
                await resources.receiver.execute(
                    projection.receipt_for(target, "new-head", "update")
                )
                assert (await resources.receiver.replay_batch()).handled_count == 1
                assert len(publisher.messages) == 1
                async with fixture.sessions.begin() as session:
                    assert (
                        await session.scalar(
                            text("SELECT cancel_requested FROM runs WHERE id=:id"), {"id": old}
                        )
                        is True
                    )
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 1
                    )
                    await session.execute(
                        text("UPDATE runs SET state='cancelled',finished_at=:now WHERE id=:id"),
                        {"id": old, "now": clock.value},
                    )
                clock.value += timedelta(seconds=31)
                assert (await resources.receiver.replay_batch()).handled_count == 1
                assert [message.head_sha for message in publisher.messages] == ["a" * 40, "d" * 40]
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 2
                    assert (
                        await session.scalar(
                            text("SELECT count(*) FROM webhook_events WHERE projected_at IS NULL")
                        )
                        == 0
                    )

    asyncio.run(asyncio.wait_for(scenario(), 20))


@pytest.mark.integration
@pytest.mark.parametrize("mutation", ["rotate", "revoke", "rebind"])
def test_composed_replay_refuses_authority_changed_during_http(
    replay_database: tuple[str, str, UUID],
    mutation: str,
) -> None:
    from datetime import UTC, datetime, timedelta

    from sqlalchemy import text

    async def scenario() -> None:
        async with projection.rig(replay_database) as fixture:
            target, publisher, clock = (
                await fixture.seed(),
                projection.ConfirmedPointers([]),
                projection.Clock(datetime.now(UTC)),
            )
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            requests: list[str] = []

            async def handle(request: httpx.Request) -> httpx.Response:
                requests.append(request.headers["PRIVATE-TOKEN"])
                if len(requests) == 1:
                    if mutation == "rotate":
                        await fixture.lifecycle.rotate_token(
                            installation_id=target.origin_provider_installation_id,
                            token="replacement-authority-token",
                            expected_generation=1,
                        )
                    else:
                        await fixture.lifecycle.revoke_installation(
                            installation_id=target.origin_provider_installation_id
                        )
                        if mutation == "rebind":
                            # Synthetic replacement; old receipt must never adopt its origin.
                            await fixture.seed(origin="https://other.gitlab.example")
                return projection.automatic_response(request, projection.canonical(target))

            async with gitlab_webhook_replay_resources(
                fixture.sessions,
                settings=GitLabRuntimeSettings.from_environment(environment),
                run_publisher=publisher,
                connect_args={"options": f"-csearch_path={replay_database[1]}"},
                transport=httpx.MockTransport(handle),
                now=clock,
            ) as resources:
                await resources.receiver.execute(projection.receipt_for(target))
                expected = (
                    ReplayBatchResult(1, 0, 1, 100)
                    if mutation == "rotate"
                    else ReplayBatchResult(1, 1, 0, 100)
                )
                assert await resources.receiver.replay_batch() == expected
                assert publisher.messages == []
                async with fixture.sessions() as session:
                    assert await session.scalar(text("SELECT count(*) FROM code_changes")) == 0
                    assert await session.scalar(text("SELECT count(*) FROM runs")) == 0
                clock.value += timedelta(seconds=31)
                later = await resources.receiver.replay_batch()
                if mutation == "rotate":
                    assert later == ReplayBatchResult(1, 1, 0, 100)
                    assert requests == [
                        "synthetic-authority-PAT-canary",
                        "replacement-authority-token",
                    ]
                    assert len(publisher.messages) == 1
                else:
                    assert later == ReplayBatchResult(0, 0, 0, 100)
                    assert len(requests) == 1
                    assert publisher.messages == []

    asyncio.run(asyncio.wait_for(scenario(), 20))


@pytest.mark.integration
def test_real_inactive_full_scan_reaches_101st_receipt_without_fallback(
    replay_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import text

    from app.webhook_worker import WebhookReplayProvider, sweep_providers_forever

    async def scenario() -> None:
        async with projection.rig(replay_database) as fixture:
            target = await fixture.seed()
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            async with gitlab_webhook_replay_resources(
                fixture.sessions,
                settings=GitLabRuntimeSettings.from_environment(environment),
                run_publisher=cast(RunMessagePublisher, object()),
                connect_args={"options": f"-csearch_path={replay_database[1]}"},
                transport=OwnedTransport(),
                now=lambda: projection.NOW,
            ) as resources:
                for number in range(101):
                    await resources.receiver.execute(
                        projection.receipt_for(target, f"inactive-{number}")
                    )
                # Synthetic readiness mutation, not a supported activation operation.
                async with fixture.sessions.begin() as session:
                    await session.execute(
                        text("UPDATE gitlab_connections SET intake_enabled=false")
                    )
                results: asyncio.Queue[ReplayBatchResult] = asyncio.Queue()

                class Observed:
                    async def replay_batch(self, *, limit: int = 100) -> ReplayBatchResult:
                        result = await resources.receiver.replay_batch(limit=limit)
                        results.put_nowait(result)
                        return result

                    async def purge_finished(self) -> int:
                        return await resources.receiver.purge_finished()

                peer = BatchReceiver([ReplayBatchResult(0, 0, 0, 100)])
                task = asyncio.create_task(
                    sweep_providers_forever(
                        (
                            WebhookReplayProvider("github", peer),
                            WebhookReplayProvider("gitlab", Observed()),
                        ),
                        Wake(),
                        poll_interval=30,
                    )
                )
                try:
                    assert await asyncio.wait_for(results.get(), 5) == ReplayBatchResult(
                        100, 0, 0, 100
                    )
                    assert await asyncio.wait_for(results.get(), 5) == ReplayBatchResult(
                        1, 0, 0, 100
                    )
                    assert peer.count == 2
                finally:
                    await stop(task)
                async with fixture.sessions() as session:
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events WHERE projected_at IS NOT NULL"
                            )
                        )
                        == 101
                    )
                    assert (
                        await session.scalar(
                            text(
                                "SELECT count(*) FROM webhook_events "
                                "WHERE projection_claim_token IS NOT NULL"
                            )
                        )
                        == 0
                    )

    asyncio.run(asyncio.wait_for(scenario(), 30))


@pytest.mark.integration
def test_cancellation_awaits_real_projection_hold_before_context_close_and_borrows_data(
    replay_database: tuple[str, str, UUID],
) -> None:
    from sqlalchemy import text

    async def scenario() -> None:
        async with (
            projection.rig(replay_database) as fixture,
            projection.lock_for(replay_database) as observer_lock,
        ):
            target = await fixture.seed()
            environment = configured_environment()
            environment["DATABASE_URL"] = replay_database[0]
            entered = asyncio.Event()

            class Blocking(OwnedTransport):
                async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
                    assert request.method == "GET"
                    assert request.url.host == "gitlab.com"
                    assert request.url.path == "/api/v4/projects/17/merge_requests/12"
                    await assert_committed_pool_and_no_transaction(fixture, replay_database[0])
                    entered.set()
                    await asyncio.Event().wait()
                    raise AssertionError("unreachable")

            transport = Blocking()

            async def replay() -> None:
                async with gitlab_webhook_replay_resources(
                    fixture.sessions,
                    settings=GitLabRuntimeSettings.from_environment(environment),
                    run_publisher=cast(RunMessagePublisher, object()),
                    connect_args={"options": f"-csearch_path={replay_database[1]}"},
                    transport=transport,
                    now=lambda: projection.NOW,
                ) as resources:
                    await resources.receiver.execute(projection.receipt_for(target))
                    await resources.receiver.replay_batch()

            task = asyncio.create_task(replay())
            try:
                await asyncio.wait_for(entered.wait(), 5)
            finally:
                await stop(task)
            assert transport.closed is True
            async with observer_lock.hold(projection.command_for(target)):
                pass
            async with fixture.sessions() as session:
                assert await session.scalar(text("SELECT 1")) == 1
                assert await session.scalar(text("SELECT count(*) FROM runs")) == 0

    asyncio.run(asyncio.wait_for(scenario(), 20))


def test_cooldown_expires_at_fixed_deadline_even_with_early_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from app import webhook_worker
    from app.webhook_worker import WebhookReplayProvider, sweep_providers_forever

    async def scenario() -> None:
        clock, wake = [100.0], Wake()
        monkeypatch.setattr(webhook_worker, "time", SimpleNamespace(monotonic=lambda: clock[0]))
        receiver = BatchReceiver([ReplayBatchResult(100, 99, 1, 100)])
        task = asyncio.create_task(
            sweep_providers_forever((WebhookReplayProvider("gitlab", receiver),), wake)
        )
        try:
            await asyncio.wait_for(wake.waiting.get(), 1)
            clock[0] = 129.0
            wake.set()
            await asyncio.wait_for(wake.waiting.get(), 1)
            assert receiver.count == 1
            clock[0] = 130.0
            wake.set()
            await asyncio.wait_for(wake.waiting.get(), 1)
            assert receiver.count == 2
        finally:
            await stop(task)

    asyncio.run(scenario())


def test_unconfigured_provider_loop_waits_and_cancels_without_spinning() -> None:
    from app.webhook_worker import sweep_providers_forever

    async def scenario() -> None:
        wake = Wake()
        task = asyncio.create_task(sweep_providers_forever((), wake))
        try:
            await asyncio.wait_for(wake.waiting.get(), 1)
            assert task.done() is False
            assert wake.waiting.empty()
        finally:
            await stop(task)

    asyncio.run(scenario())


def test_public_context_verifies_tls_disables_ambient_http_and_closes_lock_before_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ssl
    from typing import Any

    from sqlalchemy.ext.asyncio import AsyncEngine

    async def scenario() -> None:
        disposed: list[str] = []
        captured: list[dict[str, Any]] = []
        real_client, real_dispose = httpx.AsyncClient, AsyncEngine.dispose

        def client(**kwargs: Any) -> httpx.AsyncClient:
            captured.append(kwargs)
            return real_client(**kwargs)

        async def dispose(engine: AsyncEngine, close: bool = True) -> None:
            await real_dispose(engine, close=close)
            disposed.append("lock")

        class Transport(OwnedTransport):
            async def aclose(self) -> None:
                assert disposed == ["lock"]
                await super().aclose()

        monkeypatch.setattr(httpx, "AsyncClient", client)
        monkeypatch.setattr(AsyncEngine, "dispose", dispose)
        transport = Transport()
        async with gitlab_webhook_replay_resources(
            async_sessionmaker[AsyncSession](),
            settings=GitLabRuntimeSettings.from_environment(configured_environment()),
            run_publisher=cast(RunMessagePublisher, object()),
            transport=transport,
        ):
            [options] = captured
            assert isinstance(options["verify"], ssl.SSLContext)
            assert options["verify"].check_hostname is True
            assert options["verify"].verify_mode == ssl.CERT_REQUIRED
            assert options["trust_env"] is False and options["follow_redirects"] is False
            assert options["timeout"] == 10.0
            assert options["transport"] is transport
            assert "headers" not in options and "auth" not in options and "base_url" not in options
        assert transport.closed is True

    asyncio.run(scenario())


@pytest.mark.parametrize("primary", [False, True])
def test_owned_lock_close_fault_still_closes_http_and_preserves_body(
    monkeypatch: pytest.MonkeyPatch,
    primary: bool,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from sqlalchemy.ext.asyncio import AsyncEngine

    from app.bootstrap.gitlab import GitLabWebhookReplayUnavailable

    async def scenario() -> None:
        real_dispose = AsyncEngine.dispose

        async def dispose(engine: AsyncEngine, close: bool = True) -> None:
            await real_dispose(engine, close=close)
            raise RuntimeError("lock-close-DSN-PAT-canary")

        monkeypatch.setattr(AsyncEngine, "dispose", dispose)
        transport = OwnedTransport()
        error = LookupError("primary error")
        with pytest.raises(LookupError if primary else GitLabWebhookReplayUnavailable) as caught:
            async with gitlab_webhook_replay_resources(
                async_sessionmaker[AsyncSession](),
                settings=GitLabRuntimeSettings.from_environment(configured_environment()),
                run_publisher=cast(RunMessagePublisher, object()),
                transport=transport,
            ):
                if primary:
                    raise error
        if primary:
            assert caught.value is error
        assert transport.closed is True

    with caplog.at_level(logging.ERROR, logger="app.bootstrap.gitlab"):
        asyncio.run(scenario())
    assert "canary" not in caplog.text


def test_bad_ca_file_has_static_resource_error_before_transport_startup() -> None:
    from dataclasses import replace

    from app.bootstrap.gitlab import GitLabWebhookReplayUnavailable

    async def scenario() -> None:
        settings = replace(
            GitLabRuntimeSettings.from_environment(configured_environment()),
            ssl_cert_file="/not-existing/CA-path-canary",
        )
        transport = OwnedTransport()
        with pytest.raises(
            GitLabWebhookReplayUnavailable, match="^gitlab_webhook_replay_unavailable$"
        ) as caught:
            async with gitlab_webhook_replay_resources(
                async_sessionmaker[AsyncSession](),
                settings=settings,
                run_publisher=cast(RunMessagePublisher, object()),
                transport=transport,
            ):
                pytest.fail("invalid CA cannot start replay")
        assert caught.value.__cause__ is None
        assert transport.entered is False  # caller transport not acquired

    asyncio.run(scenario())
