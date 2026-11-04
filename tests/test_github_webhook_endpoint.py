"""Verified GitHub webhook receipts are durable before projection."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.types import Message, Scope

from alembic import command
from app.bootstrap.reviews_api import get_github_webhook_receipt_uow_factory
from app.main import (
    _has_valid_github_signature,
    _is_github_delivery_id,
    app,
    get_github_webhook_secret,
)
from app.modules.integrations.webhooks.api.receipt import VerifiedGitHubDelivery
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchResult,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveGitHubDelivery,
    WebhookReceipt,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from tests.github_webhook_fakes import (
    _DELIVERY_LOGGER,
    _SECRET,
    FakeReceiptUnitOfWork,
    _headers,
    _post,
)


def test_signed_delivery_is_saved_once_and_http_never_dispatches() -> None:
    body = json.dumps({"action": "added", "installation": {"id": 17}}).encode()
    receipts = FakeReceiptUnitOfWork()

    first = _post(body, _headers(body), receipts)
    second = _post(body, _headers(body), receipts)

    assert first == (202, {"status": "pending"})
    assert second == (202, {"status": "duplicate"})
    assert (
        receipts.rows["delivery-42"].delivery
        == VerifiedGitHubDelivery(
            delivery_id="delivery-42",
            event_name="installation_repositories",
            payload={"action": "added", "installation": {"id": 17}},
        ).to_receipt()
    )
    assert receipts.rows["delivery-42"].projected is False


def test_oversized_webhook_payload_returns_413_before_signature_validation() -> None:
    receipts = FakeReceiptUnitOfWork()
    oversized = b"x" * (10 * 1024 * 1024 + 1)
    headers = {
        "X-GitHub-Event": "push",
        "X-GitHub-Delivery": "d-oversized",
        "X-Hub-Signature-256": "sha256=invalid-hmac-signature",
        "Content-Length": str(len(oversized)),
    }
    status, response = _post(oversized, headers, receipts)
    assert status == 413
    assert receipts.rows == {}
    assert receipts.commits == 0


def test_chunked_oversized_webhook_returns_413_without_saving_receipt() -> None:
    receipts = FakeReceiptUnitOfWork()
    body = b'{"padding":"' + b"x" * (10 * 1024 * 1024) + b'"}'
    # Each ASGI chunk is below the 10 MiB limit; only the accumulated body is too big.
    chunks = iter(
        (
            (body[: 5 * 1024 * 1024], True),
            (body[5 * 1024 * 1024 : -2], True),
            (body[-2:], False),
        )
    )
    headers = _headers(body, delivery_id="chunked-oversized")
    headers["Transfer-Encoding"] = "chunked"
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/webhooks/github",
        "raw_path": b"/webhooks/github",
        "query_string": b"",
        "headers": [(key.lower().encode(), value.encode()) for key, value in headers.items()],
        "server": ("testserver", 80),
        "client": ("testclient", 123),
    }
    messages: list[Message] = []

    async def receive() -> Message:
        chunk, more_body = next(chunks)
        return {"type": "http.request", "body": chunk, "more_body": more_body}

    async def send(message: Message) -> None:
        messages.append(message)

    app.dependency_overrides[get_github_webhook_secret] = lambda: _SECRET
    app.dependency_overrides[get_github_webhook_receipt_uow_factory] = lambda: lambda: receipts
    try:
        asyncio.run(app(scope, receive, send))
    finally:
        app.dependency_overrides.clear()

    assert [
        message["status"] for message in messages if message["type"] == "http.response.start"
    ] == [413]
    assert json.loads(b"".join(message.get("body", b"") for message in messages)) == {
        "detail": "payload too large"
    }
    assert receipts.rows == {}
    assert receipts.commits == 0


def test_invalid_signature_never_saves_or_dispatches() -> None:
    body = b'{"action":"added"}'
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = "sha256=" + "0" * 64

    status, response = _post(body, headers, receipts)

    assert status == 401
    assert response == {"detail": "invalid GitHub webhook signature"}
    assert receipts.rows == {}


def test_missing_headers_and_malformed_signed_json_are_rejected() -> None:
    body = b"not json"
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)

    malformed_status, _ = _post(body, headers, receipts)
    missing_headers = {key: value for key, value in headers.items() if key != "X-GitHub-Event"}
    missing_status, _ = _post(body, missing_headers, receipts)

    assert malformed_status == 400
    assert missing_status == 400
    assert receipts.rows == {}


@pytest.mark.parametrize(
    ("payload", "event_name", "delivery_id"),
    [
        ({"action": "added"}, "x" * 101, "valid-id"),
        ({"action": "added"}, "push", "x" * 256),
        ({"action": "added"}, "push", "delivery 42"),
        ({"action": "added"}, "push", "status=projected"),
        ({"action": "added"}, "push", "delivery_42"),
        ({"action": "added"}, "push", "delivery.42"),
        ({"action": 42}, "push", "valid-id"),
        ({"action": "x" * 101}, "push", "valid-id"),
        ({"installation": {"id": True}}, "push", "valid-id"),
        ({"installation": {"id": 9223372036854775808}}, "push", "valid-id"),
        ({"installation": {"id": 0}}, "push", "valid-id"),
        ({"installation": "invalid"}, "push", "valid-id"),
    ],
)
def test_invalid_delivery_fields_do_not_store_or_dispatch(
    payload: dict[str, object], event_name: str, delivery_id: str
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body, delivery_id=delivery_id)
    headers["X-GitHub-Event"] = event_name

    status, _ = _post(body, headers, receipts)

    assert status == 400
    assert receipts.rows == {}


def test_delivery_id_outside_the_guid_alphabet_is_rejected_without_logging_it(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # webhook-worker logs the stored id as is: spaces and ``key=value`` would forge fields.
    sentinel = "zq7-forged status=projected detail=ok"
    body = json.dumps({"action": "labeled"}).encode()
    receipts = FakeReceiptUnitOfWork()

    with caplog.at_level(logging.DEBUG):
        status, response = _post(body, _headers(body, delivery_id=sentinel), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub delivery id"}
    assert receipts.rows == {}
    assert "zq7-forged" not in caplog.text


@pytest.mark.parametrize("delivery_id", ["72D3162E-cc78-11e3-81ab-4c9367dc0958", "x" * 255])
def test_delivery_id_in_the_guid_alphabet_is_stored_as_received(delivery_id: str) -> None:
    body = json.dumps({"action": "added"}).encode()
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body, delivery_id=delivery_id), receipts)

    assert (status, response) == (202, {"status": "pending"})
    assert list(receipts.rows) == [delivery_id]


@pytest.mark.parametrize(
    ("delivery_id", "expected"),
    [
        ("72d3162e-cc78-11e3-81ab-4c9367dc0958", True),
        ("é", False),  # Latin small e with acute: a letter, not ASCII
        ("٣", False),  # Arabic-Indic digit three
        ("１", False),  # fullwidth digit one
    ],
)
def test_delivery_id_rule_accepts_only_ascii_letters_digits_and_hyphens(
    delivery_id: str, expected: bool
) -> None:
    # httpx sends only ASCII header strings, so non-ASCII ids are checked at the rule itself.
    assert _is_github_delivery_id(delivery_id) is expected


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity", "1e999", "-1e999"])
def test_nonfinite_signed_json_is_rejected_before_storage(constant: str) -> None:
    body = f'{{"score":{constant}}}'.encode()
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"x": "\x00"},
        {"outer": {"x": ["safe", "bad\x00"]}},
        {"outer": {"bad\x00": "value"}},
    ],
)
def test_signed_json_with_nul_string_or_key_is_rejected_before_storage(
    payload: dict[str, object],
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    assert b"\\u0000" in body

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize(
    "payload",
    [
        {"outer": {"value": ["safe", "bad\ud800"]}},
        {"outer": {"bad\udfff": "value"}},
    ],
)
def test_signed_json_with_unpaired_surrogate_is_rejected_before_storage(
    payload: dict[str, object],
) -> None:
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


def test_signed_json_with_paired_surrogate_preserves_emoji_payload() -> None:
    payload = {"outer": {"emoji\U0001f600": ["value\U0001f600"]}}
    body = json.dumps(payload).encode()
    receipts = FakeReceiptUnitOfWork()
    assert b"\\ud83d\\ude00" in body

    status, response = _post(body, _headers(body), receipts)

    assert status == 202
    assert response == {"status": "pending"}
    assert json.loads(receipts.rows["delivery-42"].delivery.payload_json) == payload


@pytest.mark.parametrize("depth", [300, 10_000])
def test_excessively_nested_signed_json_is_rejected_before_storage(depth: int) -> None:
    body = b'{"nested":' * depth + b"0" + b"}" * depth
    receipts = FakeReceiptUnitOfWork()

    status, response = _post(body, _headers(body), receipts)

    assert status == 400
    assert response == {"detail": "malformed GitHub webhook payload"}
    assert receipts.rows == {}


@pytest.mark.parametrize("digest", ["z" * 64, "a" * 63])
def test_malformed_signature_is_rejected(digest: str) -> None:
    body = b"{}"
    receipts = FakeReceiptUnitOfWork()
    headers = _headers(body)
    headers["X-Hub-Signature-256"] = f"sha256={digest}"

    status, _ = _post(body, headers, receipts)

    assert status == 401
    assert receipts.rows == {}


def test_non_ascii_signature_is_rejected_by_verifier() -> None:
    assert not _has_valid_github_signature(
        raw_body=b"{}", signature=f"sha256={'é' * 64}", secret=_SECRET
    )


@pytest.mark.integration
def test_postgresql_claim_is_exclusive_expires_and_excludes_projected_rows(
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
        delivery = VerifiedGitHubDelivery("claim-1", "pull_request", {"action": "opened"})
        now = datetime(2026, 9, 28, tzinfo=UTC)
        first_token, second_token, replacement_token = uuid4(), uuid4(), uuid4()

        async def claim(token: UUID, at: datetime) -> WebhookReceipt | None:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                claimed = await uow.receipts.claim(
                    delivery.delivery_id, token, at, at + timedelta(minutes=5)
                )
                if claimed is not None:
                    await uow.commit()
                return claimed

        try:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(delivery.to_receipt()) is True
                await uow.commit()

            first, second = await asyncio.gather(claim(first_token, now), claim(second_token, now))
            assert (first is None) != (second is None)
            assert first == delivery.to_receipt() or second == delivery.to_receipt()
            winning_token = first_token if first is not None else second_token
            assert await claim(replacement_token, now + timedelta(minutes=4)) is None

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(now + timedelta(minutes=4), 10) == ()

            assert (
                await claim(replacement_token, now + timedelta(minutes=6)) == delivery.to_receipt()
            )
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                with pytest.raises(RuntimeError, match="claim was lost"):
                    await uow.receipts.mark_projected(
                        delivery.delivery_id, winning_token, now + timedelta(minutes=6)
                    )

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                await uow.receipts.mark_projected(
                    delivery.delivery_id, replacement_token, now + timedelta(minutes=6)
                )
                await uow.commit()

            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.pending_ids(now + timedelta(minutes=7), 10) == ()
            assert await claim(uuid4(), now + timedelta(minutes=7)) is None
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgresql_failed_dispatches_are_terminal_after_three_attempts(
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
        delivery = VerifiedGitHubDelivery("failure-limit-1", "ping", {"zen": "hello"})
        now = datetime(2026, 9, 28, tzinfo=UTC)
        try:
            async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                assert await uow.receipts.save(delivery.to_receipt()) is True
                await uow.commit()

            finals: list[bool] = []
            for attempt in range(1, 4):
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    token = uuid4()
                    assert (
                        await uow.receipts.claim(
                            delivery.delivery_id, token, now, now + timedelta(minutes=5)
                        )
                        == delivery.to_receipt()
                    )
                    finals.append(
                        await uow.receipts.release_after_dispatch_failure(
                            delivery.delivery_id,
                            token,
                            now + timedelta(seconds=30),
                            now,
                            max_attempts=3,
                        )
                    )
                    await uow.commit()

                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    expected_pending = (delivery.delivery_id,) if attempt < 3 else ()
                    assert await uow.receipts.pending_ids(now + timedelta(seconds=31), 10) == (
                        expected_pending
                    )
                now += timedelta(seconds=31)
            # Only the release that marks the receipt failed reports it.
            assert finals == [False, False, True]
        finally:
            await engine.dispose()

    asyncio.run(exercise())


@pytest.mark.integration
def test_postgresql_last_failed_dispatch_logs_that_the_delivery_failed(
    isolated_webhook_database: tuple[Connection, str, str], caplog: pytest.LogCaptureFixture
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "head")

    class AlwaysFailDispatcher:
        async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchResult:
            raise RuntimeError("GitHub unavailable")

    async def exercise() -> list[list[str]]:
        engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        clock = [datetime(2026, 9, 28, tzinfo=UTC)]
        receiver = ReceiveGitHubDelivery(
            uow_factory=lambda: SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions),
            dispatcher=AlwaysFailDispatcher(),
            now=lambda: clock[0],
        )
        warnings: list[list[str]] = []
        try:
            receipt = VerifiedGitHubDelivery("last-pg-1", "ping", {"zen": "hello"}).to_receipt()
            await receiver.execute(receipt)
            for _ in range(3):
                caplog.clear()
                assert await receiver.replay_pending() == 0
                warnings.append(
                    [
                        record.getMessage()
                        for record in caplog.records
                        if record.name == _DELIVERY_LOGGER and record.levelno == logging.WARNING
                    ]
                )
                clock[0] += timedelta(seconds=31)
        finally:
            await engine.dispose()
        return warnings

    with caplog.at_level(logging.INFO, logger=_DELIVERY_LOGGER):
        per_attempt = asyncio.run(exercise())

    failure = (
        "GitHub webhook delivery last-pg-1 event=ping action=- "
        "failed stage=dispatch category=internal error=RuntimeError"
    )
    assert per_attempt == [
        [failure],
        [failure],
        [failure, "GitHub webhook delivery last-pg-1 failed after its last attempt: RuntimeError"],
    ]


@pytest.mark.integration
def test_migration_preserves_legacy_receipt_and_concurrent_insert_is_unique(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, database_url, schema = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20260925_0011")
    connection.execute(
        text(
            "INSERT INTO webhook_events "
            "(id, installation_external_id, delivery_id, event, action, "
            "payload_s3_ref) "
            "VALUES (:id, 17, 'legacy-1', 'installation', 'created', 's3://old')"
        ),
        {"id": uuid4()},
    )
    connection.commit()
    command.upgrade(config, "head")

    async def exercise() -> tuple[bool, bool]:
        async_engine = create_async_engine(
            database_url, connect_args={"options": f"-csearch_path={schema}"}
        )
        sessions = async_sessionmaker(async_engine, expire_on_commit=False)
        try:
            delivery = VerifiedGitHubDelivery(
                delivery_id="new-1",
                event_name="ping",
                payload={"zen": "hello", "nested": {"ok": True}},
            )

            async def save_once() -> bool:
                async with SqlAlchemyGitHubWebhookReceiptUnitOfWork(sessions) as uow:
                    inserted = await uow.receipts.save(delivery.to_receipt())
                    await uow.commit()
                    return inserted

            first, second = await asyncio.gather(save_once(), save_once())
            return first, second
        finally:
            await async_engine.dispose()

    assert sorted(asyncio.run(exercise())) == [False, True]
    rows = (
        connection.execute(
            text(
                "SELECT delivery_id, installation_external_id, payload, payload_s3_ref "
                "FROM webhook_events ORDER BY delivery_id"
            )
        )
        .mappings()
        .all()
    )
    assert len(rows) == 2
    assert rows[0]["delivery_id"] == "legacy-1"
    assert rows[0]["payload_s3_ref"] == "s3://old"
    assert rows[0]["payload"] is None
    assert rows[1]["delivery_id"] == "new-1"
    assert rows[1]["installation_external_id"] is None
    assert rows[1]["payload"] == {"zen": "hello", "nested": {"ok": True}}
    assert rows[1]["payload_s3_ref"] is None


@pytest.mark.integration
def test_downgrade_preserves_legacy_rows_and_refuses_jsonb_data_loss(
    isolated_webhook_database: tuple[Connection, str, str],
) -> None:
    connection, _, _ = isolated_webhook_database
    config = Config("alembic.ini")
    config.attributes["connection"] = connection
    command.upgrade(config, "20260925_0011")
    connection.execute(
        text(
            "INSERT INTO webhook_events "
            "(id, installation_external_id, delivery_id, event, payload_s3_ref) "
            "VALUES (:id, 17, 'legacy-1', 'installation', 's3://old')"
        ),
        {"id": uuid4()},
    )
    connection.commit()

    command.upgrade(config, "head")
    command.downgrade(config, "20260925_0011")
    assert (
        connection.scalar(
            text("SELECT payload_s3_ref FROM webhook_events WHERE delivery_id = 'legacy-1'")
        )
        == "s3://old"
    )
    connection.rollback()

    command.upgrade(config, "head")
    connection.execute(
        text(
            "INSERT INTO webhook_events (id, delivery_id, event, payload) "
            "VALUES (:id, 'new-1', 'ping', CAST('{\"zen\":\"hello\"}' AS JSONB))"
        ),
        {"id": uuid4()},
    )
    connection.commit()
    with pytest.raises(DatabaseError, match="Cannot downgrade webhook payloads"):
        command.downgrade(config, "20260925_0011")
