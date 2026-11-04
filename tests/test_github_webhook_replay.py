"""Move-only baseline GitHub webhook tests."""

from __future__ import annotations

from tests.github_webhook_base_support import (
    _DELIVERY_LOGGER,
    UTC,
    UUID,
    Config,
    Connection,
    FakeClaimedReceipt,
    FakeDispatcher,
    FakeReceiptUnitOfWork,
    FrozenInstanceError,
    InstallationDeliveryDispatchResult,
    InstallationDeliveryDispatchStatus,
    ReceiveGitHubDelivery,
    ReplayBatchResult,
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
    WebhookReceipt,
    async_sessionmaker,
    asyncio,
    command,
    create_async_engine,
    datetime,
    logging,
    pytest,
    text,
    timedelta,
    uuid4,
)

pytest_plugins = ("tests.github_webhook_base_support",)


def test_sweep_summary_counts_only_ordinary_final_deferral_as_deferred_for_good(
    caplog: pytest.LogCaptureFixture,
) -> None:
    received = datetime(2026, 10, 9, tzinfo=UTC)
    now = received + timedelta(hours=1)
    wait = WebhookReceipt("expired-wait", "check_suite", "{}")
    refusal = WebhookReceipt("unknown-repository", "pull_request", "{}")
    second_refusal = WebhookReceipt("another-unknown-repository", "pull_request", "{}")
    uow = FakeReceiptUnitOfWork(
        rows={
            wait.delivery_id: FakeClaimedReceipt(wait, received_at=received),
            refusal.delivery_id: FakeClaimedReceipt(
                refusal, received_at=received, projection_attempt_count=2
            ),
            second_refusal.delivery_id: FakeClaimedReceipt(
                second_refusal, received_at=received, projection_attempt_count=2
            ),
        }
    )

    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER
                if delivery.delivery_id == wait.delivery_id
                else InstallationDeliveryDispatchStatus.IGNORED_UNKNOWN_REPOSITORY
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=Dispatcher(), now=lambda: now
    )
    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        assert asyncio.run(receiver.replay_pending()) == 3

    assert uow.rows[wait.delivery_id].projection_failed_at == now
    assert uow.rows[wait.delivery_id].projection_deferred_at is None
    for delivery in (refusal, second_refusal):
        row = uow.rows[delivery.delivery_id]
        assert row.projection_deferred_at == now
        assert row.projection_failed_at is None
    for row in uow.rows.values():
        assert row.projected is False
        assert row.retry_after is None
        assert row.claim_token is None
        assert row.lease_until is None
    assert asyncio.run(receiver.replay_pending()) == 0
    assert [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("GitHub webhook sweep:")
    ] == ["GitHub webhook sweep: 3 handled, 3 deferred, 2 deferred for good"]


def test_superseded_run_wait_retries_beyond_three_polls_without_generic_attempts(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 10, 9, tzinfo=UTC)
    uow = FakeReceiptUnitOfWork()

    class WaitingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER,
                "pr=p head=bbbbbbb: deferred (active_run)",
            )

    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=WaitingDispatcher(), now=lambda: now
    )
    asyncio.run(receiver.execute(WebhookReceipt("wait-1", "check_suite", "{}")))
    with caplog.at_level(logging.INFO):
        for _ in range(4):
            assert asyncio.run(receiver.replay_pending()) == 1
            row = uow.rows["wait-1"]
            assert row.projected is False
            assert row.projection_failed_at is None
            assert row.projection_deferred_at is None
            assert row.projection_attempt_count == 0
            assert row.claim_token is None
            assert row.lease_until is None
            assert row.retry_after == now + timedelta(seconds=30)
            assert asyncio.run(receiver.replay_pending()) == 0
            now += timedelta(seconds=30)
    assert uow.commits == 9
    lines = [record.getMessage() for record in caplog.records]
    assert sum("status=deferred_run_trigger" in line and "retry_at=" in line for line in lines) == 4
    assert sum("1 handled, 1 deferred, 0 deferred for good" in line for line in lines) == 4


@pytest.mark.integration
def test_postgresql_run_wait_release_is_token_guarded_durable_and_flush_only(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")

    async def exercise() -> None:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        receipt = WebhookReceipt("run-wait-1", "check_suite", "{}")
        now = datetime(2026, 10, 9, tzinfo=UTC)
        received = now
        token, replacement = uuid4(), uuid4()

        async def row() -> tuple[object, ...]:
            async with sessions() as session:
                result = await session.execute(
                    text(
                        "SELECT projected_at, projection_failed_at, projection_deferred_at, "
                        "projection_attempt_count, projection_claim_token, projection_lease_until, "
                        "retry_after FROM webhook_events WHERE delivery_id = 'run-wait-1'"
                    )
                )
                return tuple(result.one())

        try:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                await uow.receipts.save(receipt)
                await uow.session.execute(
                    text(
                        "UPDATE webhook_events SET received_at = :received, "
                        "projection_attempt_count = 2 WHERE delivery_id = 'run-wait-1'"
                    ),
                    {"received": received},
                )
                await uow.receipts.claim("run-wait-1", token, now, now + timedelta(minutes=5))
                await uow.commit()
            # Flush-only release must roll back when its use case does not commit.
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert not await uow.receipts.retry_run_trigger(
                    "run-wait-1", token, now, now=now, received_before=now - timedelta(hours=1)
                )
            assert (await row())[4] == token
            assert (await row())[3] == 2
            now += timedelta(minutes=6)
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert (
                    await uow.receipts.claim(
                        "run-wait-1", replacement, now, now + timedelta(minutes=5)
                    )
                    == receipt
                )
                await uow.commit()
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.retry_run_trigger(
                        "run-wait-1", token, now, now=now, received_before=now - timedelta(hours=1)
                    )
            for _ in range(4):
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    assert not await uow.receipts.retry_run_trigger(
                        "run-wait-1",
                        replacement,
                        now + timedelta(seconds=30),
                        now=now,
                        received_before=now - timedelta(hours=1),
                    )
                    await uow.commit()
                assert await row() == (None, None, None, 0, None, None, now + timedelta(seconds=30))
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    assert await uow.receipts.pending_ids(now, 10) == ()
                    assert await uow.receipts.purge_finished(now + timedelta(days=31)) == 0
                    await uow.commit()
                now += timedelta(seconds=30)
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    assert await uow.receipts.pending_ids(now, 10) == ("run-wait-1",)
                    assert (
                        await uow.receipts.claim(
                            "run-wait-1", replacement, now, now + timedelta(minutes=5)
                        )
                        == receipt
                    )
                    await uow.commit()
            now = received + timedelta(hours=1)
            claimed = await row()
            # Expiry is also token guarded and flush-only; rollback preserves the lease.
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.retry_run_trigger(
                        "run-wait-1", token, now, now=now, received_before=received
                    )
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.retry_run_trigger(
                    "run-wait-1", replacement, now, now=now, received_before=received
                )
            assert await row() == claimed
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.retry_run_trigger(
                    "run-wait-1", replacement, now, now=now, received_before=received
                )
                await uow.commit()
            assert await row() == (None, now, None, 0, None, None, None)
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(now + timedelta(days=1), 10) == ()
                assert (
                    await uow.receipts.claim("run-wait-1", token, now, now + timedelta(minutes=5))
                    is None
                )
                assert await uow.receipts.purge_finished(now + timedelta(days=31)) == 1
                await uow.commit()
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.parametrize("commit_fails", [False, True])
def test_wait_expiry_warning_follows_successful_commit(
    caplog: pytest.LogCaptureFixture, commit_fails: bool
) -> None:
    received = datetime(2026, 10, 9, tzinfo=UTC)
    now = received + timedelta(hours=1)
    receipt = WebhookReceipt("expiry-1", "check_suite", "{}")

    class ExpiryUnitOfWork(FakeReceiptUnitOfWork):
        async def commit(self) -> None:
            assert "supersession wait reached its age limit" not in caplog.text
            if self.commits == 1 and commit_fails:
                raise RuntimeError("finalize commit failed")
            await super().commit()

    class WaitingDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_RUN_TRIGGER
            )

    uow = ExpiryUnitOfWork(rows={"expiry-1": FakeClaimedReceipt(receipt, received_at=received)})
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=WaitingDispatcher(), now=lambda: now
    )
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(receiver.replay_pending()) == (0 if commit_fails else 1)
    expired = [
        record.getMessage()
        for record in caplog.records
        if "supersession wait reached its age limit" in record.getMessage()
    ]
    assert expired == (
        []
        if commit_fails
        else [
            "GitHub webhook delivery expiry-1 failed after supersession wait reached its age limit"
        ]
    )
    assert uow.commits == (1 if commit_fails else 2)


def test_replay_batch_counts_selection_handling_and_each_caught_projection_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    now = datetime(2026, 10, 19, tzinfo=UTC)

    class MixedReceipts(FakeReceiptUnitOfWork):
        async def claim(
            self, delivery_id: str, token: UUID, now: datetime, until: datetime
        ) -> WebhookReceipt | None:
            if delivery_id == "lost":
                return None
            if delivery_id == "inactive":
                self.rows[delivery_id].projected = True
                return None
            if delivery_id == "claim-fault":
                raise RuntimeError("claim-secret-canary")
            return await super().claim(delivery_id, token, now, until)

        async def mark_projected(self, delivery_id: str, token: UUID, at: datetime) -> None:
            if delivery_id == "finalize-fault":
                raise RuntimeError("finalize-secret-canary")
            await super().mark_projected(delivery_id, token, at)

        async def release_after_dispatch_failure(
            self,
            delivery_id: str,
            token: UUID,
            retry_after: datetime,
            failed_at: datetime,
            max_attempts: int,
        ) -> bool:
            if delivery_id == "release-fault":
                raise RuntimeError("release-secret-canary")
            return await super().release_after_dispatch_failure(
                delivery_id, token, retry_after, failed_at, max_attempts
            )

    class MixedDispatcher:
        def __init__(self) -> None:
            self.deliveries: list[str] = []

        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            self.deliveries.append(delivery.delivery_id)
            if delivery.delivery_id in {"dispatch-fault", "release-fault"}:
                raise RuntimeError("dispatch-secret-canary")
            status = (
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                if delivery.delivery_id == "deferred"
                else InstallationDeliveryDispatchStatus.ONBOARDED
            )
            return InstallationDeliveryDispatchResult(status)

    uow = MixedReceipts()
    dispatcher = MixedDispatcher()
    receiver = ReceiveGitHubDelivery(
        uow_factory=lambda: uow, dispatcher=dispatcher, now=lambda: now
    )
    for delivery_id in (
        "lost",
        "inactive",
        "success",
        "deferred",
        "claim-fault",
        "finalize-fault",
        "dispatch-fault",
        "release-fault",
    ):
        asyncio.run(receiver.execute(WebhookReceipt(delivery_id, "ping", "{}")))

    with caplog.at_level(logging.INFO):
        result = asyncio.run(receiver.replay_batch(limit=8))
    assert result == ReplayBatchResult(scanned_count=8, handled_count=2, error_count=4, limit=8)
    assert result.full is True
    assert dispatcher.deliveries == [
        "success",
        "deferred",
        "finalize-fault",
        "dispatch-fault",
        "release-fault",
    ]
    assert uow.rows["inactive"].projected is True
    assert uow.rows["lost"].projected is False
    assert uow.rows["success"].projected is True
    assert uow.rows["deferred"].retry_after == now + timedelta(minutes=5)
    assert uow.rows["dispatch-fault"].projection_attempt_count == 1
    assert uow.rows["dispatch-fault"].retry_after == now + timedelta(seconds=30)
    assert "canary" not in repr(result) + caplog.text
    assert all(record.exc_info is None for record in caplog.records)
    assert "GitHub webhook sweep: 2 handled, 1 deferred, 0 deferred for good" in caplog.text


@pytest.mark.parametrize(
    ("count", "scanned", "full"), [(0, 0, False), (1, 1, False), (2, 2, True), (3, 2, True)]
)
def test_replay_batch_reports_empty_partial_and_full_bounded_selections(
    count: int, scanned: int, full: bool
) -> None:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=FakeDispatcher())
    for number in range(count):
        asyncio.run(receiver.execute(WebhookReceipt(f"bounded-{number}", "ping", "{}")))
    result = asyncio.run(receiver.replay_batch(limit=2))
    assert result.scanned_count == scanned
    assert result.handled_count == scanned
    assert result.error_count == 0
    assert result.limit == 2
    assert result.full is full


def test_replay_batch_defaults_to_one_hundred_and_result_is_immutable() -> None:
    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=FakeDispatcher())
    for number in range(101):
        asyncio.run(receiver.execute(WebhookReceipt(f"default-{number}", "ping", "{}")))
    first = asyncio.run(receiver.replay_batch())
    assert first == ReplayBatchResult(100, 100, 0, 100)
    assert first.full is True
    for field_name in ("scanned_count", "handled_count", "error_count", "limit"):
        with pytest.raises(FrozenInstanceError):
            setattr(first, field_name, 0)
    second = asyncio.run(receiver.replay_batch())
    assert second == ReplayBatchResult(1, 1, 0, 100)
    assert second.full is False


@pytest.mark.parametrize("method", ["batch", "pending"])
@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize("limit", [0, -1])
def test_replay_preserves_dispatcher_before_positive_limit_validation(
    method: str, configured: bool, limit: int
) -> None:
    factory_calls: list[str] = []

    def factory() -> FakeReceiptUnitOfWork:
        factory_calls.append("entered")
        return FakeReceiptUnitOfWork()

    receiver = ReceiveGitHubDelivery(
        uow_factory=factory, dispatcher=FakeDispatcher() if configured else None
    )
    replay = receiver.replay_batch if method == "batch" else receiver.replay_pending
    error = ValueError if configured else RuntimeError
    message = "^limit must be positive$" if configured else "^webhook dispatcher is not configured$"
    with pytest.raises(error, match=message):
        asyncio.run(replay(limit=limit))
    assert factory_calls == []


@pytest.mark.parametrize("method", ["batch", "pending"])
def test_replay_propagates_whole_pending_query_failure(method: str) -> None:
    failure = RuntimeError("pending-query-secret-canary")

    class UnavailableSelection(FakeReceiptUnitOfWork):
        async def pending_ids(self, now: datetime, limit: int) -> tuple[str, ...]:
            raise failure

    uow = UnavailableSelection()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=FakeDispatcher())
    replay = receiver.replay_batch if method == "batch" else receiver.replay_pending
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(replay())
    assert caught.value is failure
    assert uow.commits == 0


@pytest.mark.parametrize("method", ["batch", "pending"])
def test_replay_preserves_cancellation_from_a_receipt_claim(method: str) -> None:
    class CancelledClaim(FakeReceiptUnitOfWork):
        async def claim(
            self, delivery_id: str, token: UUID, now: datetime, until: datetime
        ) -> WebhookReceipt | None:
            raise asyncio.CancelledError

    uow = CancelledClaim()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=FakeDispatcher())
    asyncio.run(receiver.execute(WebhookReceipt("cancelled-batch", "ping", "{}")))
    replay = receiver.replay_batch if method == "batch" else receiver.replay_pending
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(replay())


@pytest.mark.parametrize("deferred", [False, True])
def test_replay_pending_keeps_int_handled_projection_and_limit_compatibility(
    deferred: bool,
) -> None:
    class Dispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            return InstallationDeliveryDispatchResult(
                InstallationDeliveryDispatchStatus.DEFERRED_KNOWN_EVENT
                if deferred
                else InstallationDeliveryDispatchStatus.ONBOARDED
            )

    uow = FakeReceiptUnitOfWork()
    receiver = ReceiveGitHubDelivery(uow_factory=lambda: uow, dispatcher=Dispatcher())
    for number in range(3):
        asyncio.run(receiver.execute(WebhookReceipt(f"compat-{number}", "ping", "{}")))
    first = asyncio.run(receiver.replay_pending(limit=2))
    assert type(first) is int
    assert first == 2
    second = asyncio.run(receiver.replay_pending())
    assert type(second) is int
    assert second == 1
    assert asyncio.run(receiver.replay_pending()) == 0
