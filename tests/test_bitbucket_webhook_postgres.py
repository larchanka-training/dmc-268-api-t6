"""Bitbucket webhook admission survives retries and fences stale signed origins."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from app.bootstrap.bitbucket import BitbucketWebhookResources, get_bitbucket_webhook_resources
from app.main import app
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
from tests.test_bitbucket_connections_postgres import _config

pytest_plugins = ("tests.test_bitbucket_connections_postgres",)


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
                == 4
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

    try:
        asyncio.run(assert_fences())
    finally:
        asyncio.run(engine.dispose())
