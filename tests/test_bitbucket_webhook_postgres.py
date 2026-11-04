"""Bitbucket webhook admission survives retries and fences stale signed origins."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import BitbucketWebhookResources, get_bitbucket_webhook_resources
from app.main import app
from app.modules.integrations.bitbucket.api.dispatch import BitbucketWebhookDispatchAdapter
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    BitbucketReceiptError,
    prepare_bitbucket_receipt,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketConnectionStore,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    CredentialContext,
    CredentialPurpose,
)
from app.modules.integrations.bitbucket.infrastructure.webhook_auth import (
    SqlAlchemyBitbucketWebhookAuthenticator,
)
from app.modules.integrations.bitbucket.infrastructure.webhook_receipts import (
    SqlAlchemyBitbucketWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyWebhookReceiptOperations,
)
from app.modules.integrations.webhooks.infrastructure.models import WebhookEvent
from app.modules.reviews.application.try_enqueue_webhook_run import EnqueueResult
from app.modules.reviews.infrastructure.bitbucket_pull_request_projection import (
    SqlAlchemyBitbucketProjectionUnitOfWork,
    SqlAlchemyBitbucketPushCandidateReader,
)
from tests.bitbucket_connection_postgres_support import _config

pytest_plugins = ("tests.bitbucket_connection_postgres_support",)


@pytest.mark.integration
def test_push_deferred_candidate_waits_between_attempts_for_one_hour(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    delivery_id = "push-deferred-" + str(uuid4())

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "token",
                        context=CredentialContext(installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/deferred",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/deferred",
                )
                await session.execute(
                    text(
                        "INSERT INTO webhook_events "
                        "(id,provider_installation_id,provider,bitbucket_repository_id,"
                        "bitbucket_binding_generation,bitbucket_webhook_generation,"
                        "delivery_id,event,payload,received_at) "
                        "VALUES (:id,:installation,'bitbucket',:repository,1,1,"
                        ":delivery,'repo:push','{}'::jsonb,now()-interval '5 minutes')"
                    ),
                    {
                        "id": uuid4(),
                        "installation": installation,
                        "repository": repository,
                        "delivery": delivery_id,
                    },
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                await uow.push_continuation.advance(
                    delivery_id, repository, 1, 1, None, 17, ((17, "run_admission_deferred"),)
                )
                await uow.commit()
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert (
                    await uow.push_continuation.pending_numbers(delivery_id, repository, 1, 1) == ()
                )
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_push_retry_candidates "
                        "SET attempted_at=now()-interval '31 seconds' "
                        "WHERE pr_number=17"
                    )
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert await uow.push_continuation.pending_numbers(
                    delivery_id, repository, 1, 1
                ) == (17,)
                for _ in range(3):
                    assert await uow.push_continuation.settle_pending(
                        delivery_id, repository, 1, 1, (), ((17, "run_admission_deferred"),)
                    ) == (True, False, False)
                await uow.commit()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE webhook_events SET received_at=now()-interval '61 minutes' "
                        "WHERE delivery_id=:delivery"
                    ),
                    {"delivery": delivery_id},
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert await uow.push_continuation.settle_pending(
                    delivery_id, repository, 1, 1, (), ((17, "run_admission_deferred"),)
                ) == (False, False, True)
                await uow.commit()
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                await uow.push_continuation.advance(
                    delivery_id, repository, 1, 1, 17, 18, ((18, "canonical_stale"),)
                )
                await uow.commit()
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_push_retry_candidates "
                        "SET attempted_at=now()-interval '31 seconds' WHERE pr_number=18"
                    )
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert (
                    await uow.push_continuation.pending_numbers(delivery_id, repository, 1, 1) == ()
                )
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_push_retry_candidates "
                        "SET attempted_at=now()-interval '5 minutes 1 second' WHERE pr_number=18"
                    )
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert await uow.push_continuation.pending_numbers(
                    delivery_id, repository, 1, 1
                ) == (18,)
            conninfo = (
                make_url(os.environ["TEST_DATABASE_URL"])
                .set(drivername="postgresql")
                .render_as_string(hide_password=False)
            )
            async with await psycopg.AsyncConnection.connect(conninfo, autocommit=True) as listener:
                await listener.execute("LISTEN webhook_work_available")
                claim_token = uuid4()
                now = datetime.now(UTC)
                async with sessions.begin() as session:
                    receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                    assert (
                        await receipts.claim(
                            delivery_id, claim_token, now, now + timedelta(minutes=5)
                        )
                        is not None
                    )
                async with sessions.begin() as session:
                    receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                    await receipts.retry_push_continuation(delivery_id, claim_token, now)
                notification = await asyncio.wait_for(anext(listener.notifies()), timeout=3)
                assert (notification.channel, notification.payload) == (
                    "webhook_work_available",
                    "",
                )
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
@pytest.mark.parametrize(
    ("receipt_age", "retry_delay", "terminal"),
    [
        (timedelta(hours=6), timedelta(seconds=120), True),
        (timedelta(hours=5, minutes=59), timedelta(seconds=120), False),
        (timedelta(hours=1), timedelta(hours=1), False),
    ],
)
def test_shared_push_provider_pause_has_six_hour_receipt_ceiling(
    bitbucket_connection_schema: Connection,
    receipt_age: timedelta,
    retry_delay: timedelta,
    terminal: bool,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    delivery_id = "push-provider-pause-" + str(uuid4())
    now = datetime.now(UTC)

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "test-token",
                        context=CredentialContext(installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await session.execute(
                    text(
                        "INSERT INTO webhook_events "
                        "(id,provider_installation_id,provider,bitbucket_repository_id,"
                        "bitbucket_binding_generation,bitbucket_webhook_generation,"
                        "delivery_id,event,payload,received_at) VALUES "
                        "(:id,:installation,'bitbucket',:repository,1,1,:delivery,"
                        "'repo:push','{}'::jsonb,:received_at)"
                    ),
                    {
                        "id": uuid4(),
                        "installation": installation,
                        "repository": repository,
                        "delivery": delivery_id,
                        "received_at": now - receipt_age,
                    },
                )
            token = uuid4()
            async with sessions.begin() as session:
                receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                assert await receipts.claim(delivery_id, token, now, now + timedelta(minutes=5))
            async with sessions.begin() as session:
                receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                assert (
                    await receipts.retry_push_provider(
                        delivery_id,
                        token,
                        now + retry_delay,
                        now=now,
                        received_before=now - timedelta(hours=6),
                    )
                    is terminal
                )
            async with sessions() as session:
                row = (
                    await session.execute(
                        text(
                            "SELECT projection_failed_at,retry_after,projection_attempt_count "
                            "FROM webhook_events WHERE delivery_id=:delivery"
                        ),
                        {"delivery": delivery_id},
                    )
                ).one()
            assert row.projection_failed_at == (now if terminal else None)
            assert row.retry_after == (None if terminal else now + retry_delay)
            assert row.projection_attempt_count == 0
            async with sessions.begin() as session:
                receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                before_due = now + retry_delay - timedelta(seconds=1)
                assert (
                    await receipts.claim(
                        delivery_id, uuid4(), before_due, before_due + timedelta(minutes=5)
                    )
                    is None
                )
            async with sessions.begin() as session:
                receipts = SqlAlchemyWebhookReceiptOperations(session, provider="bitbucket")
                due = now + retry_delay
                assert (
                    await receipts.claim(delivery_id, uuid4(), due, due + timedelta(minutes=5))
                    is None
                ) is terminal
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
def test_retry_page_continues_while_other_candidates_are_already_due(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace, remote_repo = (uuid4() for _ in range(4))
    db.execute(
        text("INSERT INTO workspaces (id,name,daily_budget_usd) VALUES (:id,'test',7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    receipt_id, delivery_id = uuid4(), "push-due-backlog-" + str(uuid4())

    async def scenario() -> None:
        try:
            async with sessions.begin() as session:
                store = SqlAlchemyBitbucketConnectionStore(session)
                await store.create_connection(
                    workspace_id=workspace,
                    installation_id=installation,
                    bitbucket_workspace_uuid=remote_workspace,
                    token=cipher.encrypt(
                        "test-token",
                        context=CredentialContext(installation, workspace),
                        purpose=CredentialPurpose.TOKEN,
                    ),
                )
                repository, _ = await store.bind_repository(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_uuid=remote_repo,
                    full_name="team/repo",
                    default_branch="main",
                    web_url="https://bitbucket.org/team/repo",
                )
                await session.execute(
                    text(
                        "INSERT INTO webhook_events "
                        "(id,provider_installation_id,provider,bitbucket_repository_id,"
                        "bitbucket_binding_generation,bitbucket_webhook_generation,"
                        "delivery_id,event,payload) VALUES "
                        "(:id,:installation,'bitbucket',:repository,1,1,:delivery,"
                        "'repo:push','{}'::jsonb)"
                    ),
                    {
                        "id": receipt_id,
                        "installation": installation,
                        "repository": repository,
                        "delivery": delivery_id,
                    },
                )
                await session.execute(
                    text(
                        "INSERT INTO bitbucket_push_retry_candidates "
                        "(webhook_event_id,pr_number,attempted_at,attempt_count,reason) "
                        "VALUES (:receipt,:number,now()-interval '6 minutes',0,'canonical_stale')"
                    ),
                    [{"receipt": receipt_id, "number": number} for number in range(1, 61)],
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                first = await uow.push_continuation.pending_numbers(delivery_id, repository, 1, 1)
                assert first == (1, 2, 3, 4, 5)
                assert await uow.push_continuation.settle_pending(
                    delivery_id,
                    repository,
                    1,
                    1,
                    (),
                    tuple((number, "canonical_stale") for number in first),
                ) == (True, True, False)
                await uow.commit()
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert await uow.push_continuation.pending_numbers(
                    delivery_id, repository, 1, 1
                ) == (6, 7, 8, 9, 10)
            async with sessions.begin() as session:
                await session.execute(
                    text(
                        "UPDATE bitbucket_push_retry_candidates SET attempted_at=now() "
                        "WHERE webhook_event_id=:receipt"
                    ),
                    {"receipt": receipt_id},
                )
            async with SqlAlchemyBitbucketProjectionUnitOfWork(sessions) as uow:
                assert await uow.push_continuation.settle_pending(
                    delivery_id, repository, 1, 1, (), ()
                ) == (True, False, False)
        finally:
            await engine.dispose()

    asyncio.run(scenario())


@pytest.mark.integration
# Imported fixture owns a disposable PostgreSQL schema.
def test_bitbucket_signed_receipt_dedup_and_generation_fences(
    bitbucket_connection_schema: Connection,
) -> None:
    db = bitbucket_connection_schema
    command.upgrade(_config(db), "head")
    workspace, installation, remote_workspace = uuid4(), uuid4(), uuid4()
    remote_repo, second_remote = uuid4(), uuid4()
    db.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'test', 7)"),
        {"id": workspace},
    )
    schema = str(db.scalar(text("SELECT current_schema()")))
    db.commit()
    url = make_url(os.environ["TEST_DATABASE_URL"]).set(drivername="postgresql+psycopg_async")
    engine = create_async_engine(url, connect_args={"options": f"-csearch_path={schema}"})
    factory = async_sessionmaker(engine, expire_on_commit=False)
    cipher = BitbucketCredentialCipher(active_key_id="test", keys={"test": b"a" * 32})
    secret = "s" * 40
    rotated = "t" * 40

    async def setup() -> tuple[UUID, UUID]:
        async with factory.begin() as session:
            store = SqlAlchemyBitbucketConnectionStore(session)
            await store.create_connection(
                workspace_id=workspace,
                installation_id=installation,
                bitbucket_workspace_uuid=remote_workspace,
                token=cipher.encrypt(
                    "test-token",
                    context=CredentialContext(installation, workspace),
                    purpose=CredentialPurpose.TOKEN,
                ),
            )
            first, _ = await store.bind_repository(
                workspace_id=workspace,
                installation_id=installation,
                repository_uuid=remote_repo,
                full_name="nvdovnar/first",
                default_branch="main",
                web_url="https://bitbucket.org/nvdovnar/first",
            )
            second, _ = await store.bind_repository(
                workspace_id=workspace,
                installation_id=installation,
                repository_uuid=second_remote,
                full_name="nvdovnar/second",
                default_branch="main",
                web_url="https://bitbucket.org/nvdovnar/second",
            )
            for repository_id in (first, second):
                await store.set_repository_webhook_secret(
                    workspace_id=workspace,
                    installation_id=installation,
                    repository_id=repository_id,
                    secret=cipher.encrypt(
                        secret,
                        context=CredentialContext(installation, workspace, repository_id),
                        purpose=CredentialPurpose.WEBHOOK_SECRET,
                    ),
                )
            return first, second

    first, second = asyncio.run(setup())
    authenticator = SqlAlchemyBitbucketWebhookAuthenticator(factory, cipher)
    receiver = ReceiveWebhookDelivery(
        provider="bitbucket",
        uow_factory=lambda: SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory),
    )
    resources = BitbucketWebhookResources(authenticator=authenticator, receiver=receiver)
    body = ('{"repository":{"uuid":"{' + str(remote_repo) + '}"},"pullrequest":{"id":7}}').encode()
    signature = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    hook, request_id = uuid4(), uuid4()
    headers = {
        "X-Hub-Signature": signature,
        "X-Hook-UUID": "{" + str(hook) + "}",
        "X-Request-UUID": "{" + str(request_id) + "}",
        "X-Event-Key": "pullrequest:created",
        "X-Attempt-Number": "1",
    }
    url_path = f"/webhooks/bitbucket/{installation}/{first}"
    app.dependency_overrides[get_bitbucket_webhook_resources] = lambda: resources
    try:
        with TestClient(app) as client:
            invalid = client.post(
                url_path, content=body, headers={**headers, "X-Hub-Signature": "sha256=" + "0" * 64}
            )
            assert invalid.status_code == 401
            assert client.post(url_path, content=body, headers=headers).json() == {
                "status": "pending"
            }
            assert client.post(
                url_path, content=body, headers={**headers, "X-Attempt-Number": "2"}
            ).json() == {"status": "duplicate"}
            changed = body.replace(b'"id":7', b'"id":8')
            changed_sig = "sha256=" + hmac.new(secret.encode(), changed, hashlib.sha256).hexdigest()
            conflict = client.post(
                url_path, content=changed, headers={**headers, "X-Hub-Signature": changed_sig}
            )
            assert conflict.status_code == 409
    finally:
        app.dependency_overrides.clear()

    async def assert_fences() -> None:
        async with factory() as session:
            assert await session.scalar(
                select(WebhookEvent.id).where(WebhookEvent.provider == "bitbucket")
            )
            assert await session.scalar(
                text(
                    "SELECT intake_enabled FROM bitbucket_connections "
                    "WHERE provider_installation_id=:id"
                ),
                {"id": installation},
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM webhook_events WHERE provider='bitbucket'")
                )
                == 1
            )
        stale_origin = await authenticator.authenticate(
            installation_id=installation, repository_id=first, body=body, signature=signature
        )
        assert stale_origin is not None
        for column in ("enabled", "installation_available"):
            async with factory.begin() as session:
                await session.execute(
                    text(f"UPDATE repositories SET {column}=false WHERE id=:id"),
                    {"id": first},
                )
            assert (
                await authenticator.authenticate(
                    installation_id=installation,
                    repository_id=first,
                    body=body,
                    signature=signature,
                )
                is None
            )
            inactive = prepare_bitbucket_receipt(
                origin=stale_origin,
                event_name="pullrequest:created",
                hook_uuid=uuid4(),
                request_uuid=uuid4(),
                payload_json=body.decode(),
            )
            with pytest.raises(BitbucketReceiptError, match="inactive_origin"):
                await receiver.execute(inactive)
            async with factory.begin() as session:
                await session.execute(
                    text(f"UPDATE repositories SET {column}=true WHERE id=:id"),
                    {"id": first},
                )
        push_body = (
            '{"repository":{"uuid":"{' + str(remote_repo) + '}"},'
            '"push":{"changes":[{"new":{"type":"branch","name":"feature",'
            '"target":{"hash":"' + "a" * 40 + '"}}}]}}'
        )
        push_receipt = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="repo:push",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=push_body,
        )
        assert await receiver.execute(push_receipt) == InstallationDeliveryDispatchStatus.PENDING
        push_claim = uuid4()
        push_now = datetime.now(UTC)
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    push_receipt.delivery_id,
                    push_claim,
                    push_now,
                    push_now + timedelta(minutes=5),
                )
                is not None
            )
            await uow.commit()
        async with factory.begin() as session:
            await session.execute(
                text(
                    "UPDATE webhook_events SET projection_attempt_count=2 "
                    "WHERE delivery_id=:delivery"
                ),
                {"delivery": push_receipt.delivery_id},
            )
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            await uow.receipts.retry_push_continuation(
                push_receipt.delivery_id, push_claim, push_now + timedelta(seconds=1)
            )
            await uow.commit()
        async with factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT projection_claim_token,projection_attempt_count,retry_after "
                        "FROM webhook_events WHERE delivery_id=:delivery"
                    ),
                    {"delivery": push_receipt.delivery_id},
                )
            ).one()
            assert row.projection_claim_token is None
            assert row.projection_attempt_count == 0
            assert row.retry_after == push_now + timedelta(seconds=1)
        resumed_push_claim = uuid4()
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    push_receipt.delivery_id,
                    resumed_push_claim,
                    push_now + timedelta(seconds=2),
                    push_now + timedelta(minutes=5, seconds=2),
                )
                is not None
            )
            await uow.commit()
        async with factory() as session:
            assert (
                await session.scalar(
                    select(WebhookEvent.projected_at).where(
                        WebhookEvent.delivery_id == push_receipt.delivery_id
                    )
                )
                is None
            )
            assert {
                binding.repository_id: binding.confirmed_at is not None
                for binding in await SqlAlchemyBitbucketConnectionStore(session).list_bindings(
                    workspace_id=workspace
                )
            } == {first: True, second: False}
        async with factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO bitbucket_push_retry_candidates "
                    "(webhook_event_id,pr_number,reason) "
                    "SELECT id,17,'run_admission_deferred' FROM webhook_events "
                    "WHERE delivery_id=:delivery"
                ),
                {"delivery": push_receipt.delivery_id},
            )
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            await uow.receipts.mark_projected(
                push_receipt.delivery_id, resumed_push_claim, push_now + timedelta(seconds=3)
            )
            await uow.commit()
        async with factory() as session:
            assert (
                await session.scalar(
                    text(
                        "SELECT count(*) FROM bitbucket_push_retry_candidates AS retry "
                        "JOIN webhook_events AS receipt ON receipt.id=retry.webhook_event_id "
                        "WHERE receipt.delivery_id=:delivery"
                    ),
                    {"delivery": push_receipt.delivery_id},
                )
                == 0
            )
        fresh_receipt = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="pullrequest:created",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=body.decode(),
        )
        concurrent = await asyncio.gather(
            receiver.execute(fresh_receipt), receiver.execute(fresh_receipt)
        )
        assert set(concurrent) == {
            InstallationDeliveryDispatchStatus.PENDING,
            InstallationDeliveryDispatchStatus.DUPLICATE,
        }
        first_receipt = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="pullrequest:created",
            hook_uuid=hook,
            request_uuid=request_id,
            payload_json=body.decode(),
        )
        now = datetime.now(UTC)
        first_claim = uuid4()
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            claimed = await uow.receipts.claim(
                first_receipt.delivery_id, first_claim, now, now + timedelta(minutes=5)
            )
            assert claimed is not None and claimed.delivery_id == first_receipt.delivery_id
            await uow.commit()
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    first_receipt.delivery_id,
                    uuid4(),
                    now + timedelta(minutes=1),
                    now + timedelta(minutes=6),
                )
                is None
            )
            await uow.commit()
        # A fresh UOW after the lease expires models a worker process restart.
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    first_receipt.delivery_id,
                    uuid4(),
                    now + timedelta(minutes=6),
                    now + timedelta(minutes=11),
                )
                is not None
            )
            await uow.commit()
        async with factory() as session:
            assert (
                await SqlAlchemyWebhookReceiptOperations(session, provider="gitlab").claim(
                    first_receipt.delivery_id,
                    uuid4(),
                    now + timedelta(minutes=12),
                    now + timedelta(minutes=17),
                )
                is None
            )
        sibling_body = ('{"repository":{"uuid":"{' + str(second_remote) + '}"}}').encode()
        sibling_sig = (
            "sha256=" + hmac.new(secret.encode(), sibling_body, hashlib.sha256).hexdigest()
        )
        sibling = await authenticator.authenticate(
            installation_id=installation,
            repository_id=second,
            body=sibling_body,
            signature=sibling_sig,
        )
        assert sibling is not None
        failed_push = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="repo:push",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=push_body,
        )
        assert await receiver.execute(failed_push) == InstallationDeliveryDispatchStatus.PENDING
        failed_push_now = datetime.now(UTC)
        failed_push_claim = uuid4()
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    failed_push.delivery_id,
                    failed_push_claim,
                    failed_push_now,
                    failed_push_now + timedelta(minutes=5),
                )
                is not None
            )
            await uow.commit()
        async with factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO bitbucket_push_retry_candidates "
                    "(webhook_event_id,pr_number,reason,attempt_count) "
                    "SELECT id,17,'canonical_stale',2 FROM webhook_events "
                    "WHERE delivery_id=:delivery"
                ),
                {"delivery": failed_push.delivery_id},
            )
        # Candidate settlement commits, then the worker dies before failing the receipt.
        async with SqlAlchemyBitbucketProjectionUnitOfWork(factory) as projection_uow:
            assert await projection_uow.push_continuation.settle_pending(
                failed_push.delivery_id, first, 1, 2, (), ((17, "canonical_stale"),)
            ) == (False, False, True)
            await projection_uow.commit()
        racing_push = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="repo:push",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=push_body,
        )
        assert await receiver.execute(racing_push) == InstallationDeliveryDispatchStatus.PENDING
        racing_claim_token = uuid4()
        racing_claimed_at = datetime.now(UTC)
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            claimed_racing_push = await uow.receipts.claim(
                racing_push.delivery_id,
                racing_claim_token,
                racing_claimed_at,
                racing_claimed_at + timedelta(minutes=5),
            )
            assert claimed_racing_push is not None
            await uow.commit()
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            with pytest.raises(RuntimeError, match="webhook projection claim was lost"):
                await uow.receipts.fail_push_candidates(
                    racing_push.delivery_id, racing_claim_token, racing_claimed_at
                )
        async with factory() as session:
            assert (
                await session.scalar(
                    select(WebhookEvent.projection_claim_token).where(
                        WebhookEvent.delivery_id == racing_push.delivery_id
                    )
                )
                == racing_claim_token
            )
        async with factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO bitbucket_push_retry_candidates "
                    "(webhook_event_id,pr_number,reason,attempt_count,failed_at) "
                    "SELECT id,19,'canonical_stale',3,now() FROM webhook_events "
                    "WHERE delivery_id=:delivery"
                ),
                {"delivery": racing_push.delivery_id},
            )
        revoked_push = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="repo:push",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=push_body,
        )
        assert await receiver.execute(revoked_push) == InstallationDeliveryDispatchStatus.PENDING
        async with factory.begin() as session:
            await session.execute(
                text(
                    "INSERT INTO bitbucket_push_retry_candidates "
                    "(webhook_event_id,pr_number,reason,attempt_count) "
                    "SELECT id,20,'canonical_stale',1 FROM webhook_events "
                    "WHERE delivery_id=:delivery"
                ),
                {"delivery": revoked_push.delivery_id},
            )
        async with factory.begin() as session:
            store = SqlAlchemyBitbucketConnectionStore(session)
            await store.set_repository_webhook_secret(
                workspace_id=workspace,
                installation_id=installation,
                repository_id=first,
                secret=cipher.encrypt(
                    rotated,
                    context=CredentialContext(installation, workspace, first),
                    purpose=CredentialPurpose.WEBHOOK_SECRET,
                ),
            )
        revoked_at = datetime.now(UTC)
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    revoked_push.delivery_id,
                    uuid4(),
                    revoked_at,
                    revoked_at + timedelta(minutes=5),
                )
                is None
            )
            await uow.commit()
        async with factory() as session:
            revoked_row = (
                await session.execute(
                    text(
                        "SELECT receipt.projection_failed_at,retry.reason,retry.failed_at "
                        "FROM webhook_events AS receipt "
                        "JOIN bitbucket_push_retry_candidates AS retry "
                        "ON retry.webhook_event_id=receipt.id "
                        "WHERE receipt.delivery_id=:delivery"
                    ),
                    {"delivery": revoked_push.delivery_id},
                )
            ).one()
            assert revoked_row.projection_failed_at == revoked_at
            assert revoked_row.reason == "origin_inactive"
            assert revoked_row.failed_at == revoked_at

        class ReaderHandler:
            async def push(
                self,
                installation_id: UUID,
                repository_id: UUID,
                repository_uuid: UUID,
                branches: frozenset[str],
                binding_generation: int,
                webhook_generation: int,
                delivery_id: str,
            ) -> tuple[EnqueueResult, ...]:
                await SqlAlchemyBitbucketPushCandidateReader(factory).matching_open_pr_page(
                    installation_id,
                    repository_id,
                    repository_uuid,
                    binding_generation,
                    webhook_generation,
                    branches,
                    None,
                )
                return ()

            async def pull_request(
                self,
                installation_id: UUID,
                repository_id: UUID,
                repository_uuid: UUID,
                number: int,
                binding_generation: int,
                webhook_generation: int,
            ) -> EnqueueResult | None:
                raise AssertionError("push receipt must not dispatch a pull request")

        # Claim was active, but the binding rotated before the provider-free dispatch.
        racing_result = await BitbucketWebhookDispatchAdapter(ReaderHandler()).execute(
            claimed_racing_push
        )
        assert racing_result.status == InstallationDeliveryDispatchStatus.IGNORED_IRRELEVANT_EVENT
        assert racing_result.detail == "inactive_origin"
        racing_finalized_at = datetime.now(UTC)
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            await uow.receipts.mark_projected(
                racing_push.delivery_id, racing_claim_token, racing_finalized_at
            )
            await uow.commit()
        async with factory() as session:
            racing_failure = (
                await session.execute(
                    text(
                        "SELECT receipt.projected_at,receipt.projection_failed_at,"
                        "retry.reason,retry.attempt_count,retry.failed_at "
                        "FROM webhook_events AS receipt "
                        "JOIN bitbucket_push_retry_candidates AS retry "
                        "ON retry.webhook_event_id=receipt.id "
                        "WHERE receipt.delivery_id=:delivery"
                    ),
                    {"delivery": racing_push.delivery_id},
                )
            ).one()
            assert racing_failure.projected_at is None
            assert racing_failure.projection_failed_at == racing_finalized_at
            assert (racing_failure.reason, racing_failure.attempt_count) == (
                "canonical_stale",
                3,
            )
            assert racing_failure.failed_at is not None
        reclaim_at = failed_push_now + timedelta(minutes=6)
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    failed_push.delivery_id,
                    uuid4(),
                    reclaim_at,
                    reclaim_at + timedelta(minutes=5),
                )
                is None
            )
            await uow.commit()
        async with factory() as session:
            failure = (
                await session.execute(
                    text(
                        "SELECT receipt.projected_at,receipt.projection_failed_at,"
                        "retry.reason,retry.attempt_count,retry.failed_at "
                        "FROM webhook_events AS receipt "
                        "JOIN bitbucket_push_retry_candidates AS retry "
                        "ON retry.webhook_event_id=receipt.id "
                        "WHERE receipt.delivery_id=:delivery"
                    ),
                    {"delivery": failed_push.delivery_id},
                )
            ).one()
            assert failure.projected_at is None
            assert failure.projection_failed_at == reclaim_at
            assert (failure.reason, failure.attempt_count) == ("canonical_stale", 3)
            assert failure.failed_at is not None
        stale_receipt = prepare_bitbucket_receipt(
            origin=stale_origin,
            event_name="pullrequest:created",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=body.decode(),
        )
        with pytest.raises(BitbucketReceiptError, match="inactive_origin"):
            await receiver.execute(stale_receipt)
        async with factory() as session:
            assert {
                binding.repository_id: binding.confirmed_at is not None
                for binding in await SqlAlchemyBitbucketConnectionStore(session).list_bindings(
                    workspace_id=workspace
                )
            } == {first: False, second: False}
        assert (
            await authenticator.authenticate(
                installation_id=installation, repository_id=first, body=body, signature=signature
            )
            is None
        )
        sibling_receipt = prepare_bitbucket_receipt(
            origin=sibling,
            event_name="pullrequest:created",
            hook_uuid=hook,
            request_uuid=request_id,
            payload_json=sibling_body.decode(),
        )
        await receiver.execute(sibling_receipt)
        async with factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM webhook_events WHERE provider='bitbucket'")
                )
                == 7
            )
            assert {
                binding.repository_id: binding.confirmed_at is not None
                for binding in await SqlAlchemyBitbucketConnectionStore(session).list_bindings(
                    workspace_id=workspace
                )
            } == {first: False, second: True}
        rotated_origin = await authenticator.authenticate(
            installation_id=installation,
            repository_id=first,
            body=body,
            signature="sha256=" + hmac.new(rotated.encode(), body, hashlib.sha256).hexdigest(),
        )
        assert rotated_origin is not None
        async with factory.begin() as session:
            store = SqlAlchemyBitbucketConnectionStore(session)
            assert await store.disconnect_repository(
                workspace_id=workspace, installation_id=installation, repository_id=first
            )
        disconnected_receipt = prepare_bitbucket_receipt(
            origin=rotated_origin,
            event_name="pullrequest:created",
            hook_uuid=hook,
            request_uuid=uuid4(),
            payload_json=body.decode(),
        )
        with pytest.raises(BitbucketReceiptError, match="inactive_origin"):
            await receiver.execute(disconnected_receipt)
        assert (
            await authenticator.authenticate(
                installation_id=installation,
                repository_id=first,
                body=body,
                signature="sha256=" + hmac.new(rotated.encode(), body, hashlib.sha256).hexdigest(),
            )
            is None
        )
        assert (
            await authenticator.authenticate(
                installation_id=installation,
                repository_id=second,
                body=sibling_body,
                signature=sibling_sig,
            )
            is not None
        )
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            assert (
                await uow.receipts.claim(
                    first_receipt.delivery_id,
                    uuid4(),
                    now + timedelta(minutes=12),
                    now + timedelta(minutes=17),
                )
                is None
            )
            await uow.commit()
        async with factory() as session:
            assert (
                await session.scalar(
                    select(WebhookEvent.projected_at).where(
                        WebhookEvent.delivery_id == first_receipt.delivery_id
                    )
                )
                is not None
            )

        fairness_now = datetime.now(UTC)
        fresh_delivery = "bitbucket-fresh-" + str(uuid4())
        async with factory.begin() as session:
            await session.execute(
                text("UPDATE webhook_events SET projected_at=:now WHERE provider='bitbucket'"),
                {"now": fairness_now},
            )
            await session.execute(
                text(
                    "INSERT INTO webhook_events "
                    "(id,provider_installation_id,provider,bitbucket_repository_id,"
                    "bitbucket_binding_generation,bitbucket_webhook_generation,"
                    "bitbucket_push_after_pr_number,delivery_id,event,payload,received_at,"
                    "retry_after) VALUES (:id,:installation,'bitbucket',:repository,1,2,5,"
                    ":delivery,'repo:push','{}'::jsonb,:received,:retry)"
                ),
                [
                    {
                        "id": uuid4(),
                        "installation": installation,
                        "repository": second,
                        "delivery": f"bitbucket-old-{uuid4()}",
                        "received": fairness_now - timedelta(hours=1),
                        "retry": fairness_now + timedelta(seconds=1),
                    }
                    for _ in range(100)
                ],
            )
            await session.execute(
                text(
                    "INSERT INTO webhook_events "
                    "(id,provider_installation_id,provider,bitbucket_repository_id,"
                    "bitbucket_binding_generation,bitbucket_webhook_generation,delivery_id,"
                    "event,payload,received_at) VALUES "
                    "(:id,:installation,'bitbucket',:repository,1,2,:delivery,"
                    "'pullrequest:updated','{}'::jsonb,:received)"
                ),
                {
                    "id": uuid4(),
                    "installation": installation,
                    "repository": second,
                    "delivery": fresh_delivery,
                    "received": fairness_now,
                },
            )
        async with SqlAlchemyBitbucketWebhookReceiptUnitOfWork(factory) as uow:
            pending = await uow.receipts.pending_ids(fairness_now + timedelta(seconds=2), 100)
            assert fresh_delivery in pending

    try:
        asyncio.run(assert_fences())
    finally:
        asyncio.run(engine.dispose())
