"""Request-scoped Bitbucket browser management composition."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Annotated, cast
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reviews_api import ReviewsApiResources
from app.modules.auth.application.scope import AuthScope
from app.modules.code_index.application.request_bitbucket_index_refresh import (
    RequestBitbucketIndexRefresh,
)
from app.modules.code_index.infrastructure.bitbucket_index_refresh import (
    SqlAlchemyBitbucketIndexRefreshUnitOfWork,
)
from app.modules.integrations.bitbucket.api.dispatch import BitbucketWebhookDispatchAdapter
from app.modules.integrations.bitbucket.application.eligibility import DetermineBitbucketEligibility
from app.modules.integrations.bitbucket.application.manage_connection import (
    ManageBitbucketConnection,
)
from app.modules.integrations.bitbucket.application.webhook_receipts import (
    AuthenticatedBitbucketOrigin,
    BitbucketReceiptError,
)
from app.modules.integrations.bitbucket.infrastructure.connection_store import (
    SqlAlchemyBitbucketManagementUnitOfWork,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
    optional_bitbucket_cipher_from_environment,
)
from app.modules.integrations.bitbucket.infrastructure.credential_codec import (
    BitbucketCredentialCodecAdapter,
)
from app.modules.integrations.bitbucket.infrastructure.current_pull_request import (
    BitbucketCurrentPullRequestProvider,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketCloudRepositoryProvider,
)
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
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
    WebhookReceipt,
)
from app.modules.repositories.application.onboard_repository import load_default_rule_sets
from app.modules.reviews.application.project_bitbucket_pull_request import (
    ProjectBitbucketPullRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.bitbucket_eligibility_candidates import (
    SqlAlchemyBitbucketEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.bitbucket_pull_request_projection import (
    SqlAlchemyBitbucketProjectionAuthority,
    SqlAlchemyBitbucketProjectionUnitOfWork,
    SqlAlchemyBitbucketPushCandidateReader,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork


def _cipher_from_environment() -> BitbucketCredentialCipher:
    try:
        cipher = optional_bitbucket_cipher_from_environment(os.environ)
        if cipher is None:
            raise ValueError
        return cipher
    except ValueError:
        raise HTTPException(
            status_code=503, detail="Bitbucket connection is not configured"
        ) from None


def _webhook_base_from_environment() -> str:
    value = os.environ.get("BITBUCKET_WEBHOOK_BASE_URL", "")
    parsed = urlsplit(value)
    if (
        not value
        or len(value) > 300
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in ("", "/")
    ):
        raise HTTPException(status_code=503, detail="Bitbucket connection is not configured")
    return value.rstrip("/")


def get_bitbucket_workspace_scope(
    workspace_id: UUID, scope: Annotated[AuthScope, Depends(get_auth_scope)]
) -> AuthScope:
    if workspace_id not in scope.workspace_ids:
        raise HTTPException(status_code=404, detail="workspace not found")
    return scope


async def get_bitbucket_management(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_bitbucket_workspace_scope)],
) -> AsyncIterator[ManageBitbucketConnection]:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if not isinstance(resources, ReviewsApiResources):
        raise HTTPException(status_code=503, detail="Bitbucket connection is not configured")
    cipher = BitbucketCredentialCodecAdapter(_cipher_from_environment())
    webhook_base = _webhook_base_from_environment()
    rule_sets = load_default_rule_sets(Path(__file__).resolve().parents[2] / "review" / "rules")
    async with httpx.AsyncClient(timeout=10, trust_env=False, follow_redirects=False) as client:
        yield ManageBitbucketConnection(
            uow_factory=partial(SqlAlchemyBitbucketManagementUnitOfWork, resources.session_factory),
            cipher=cipher,
            provider=BitbucketCloudRepositoryProvider(client),
            rule_sets=rule_sets,
            webhook_base_url=webhook_base,
        )


class BitbucketWebhookResources:
    def __init__(
        self,
        *,
        authenticator: SqlAlchemyBitbucketWebhookAuthenticator,
        receiver: ReceiveWebhookDelivery,
    ) -> None:
        self._authenticator = authenticator
        self._receiver = receiver

    async def authenticate(
        self, *, installation_id: UUID, repository_id: UUID, body: bytes, signature: str | None
    ) -> AuthenticatedBitbucketOrigin | None:
        try:
            return await self._authenticator.authenticate(
                installation_id=installation_id,
                repository_id=repository_id,
                body=body,
                signature=signature,
            )
        except Exception as error:
            logging.getLogger(__name__).error(
                "Bitbucket webhook authenticate failed",
                extra={"error_type": type(error).__name__},
            )
            raise BitbucketWebhookUnavailable() from None

    async def accept(self, receipt: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        try:
            return await self._receiver.execute(receipt)
        except BitbucketReceiptError:
            raise
        except Exception as error:
            logging.getLogger(__name__).error(
                "Bitbucket webhook accept failed",
                extra={"error_type": type(error).__name__},
            )
            raise BitbucketWebhookUnavailable() from None


class BitbucketWebhookUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("bitbucket_webhook_unavailable")


def get_bitbucket_webhook_resources(request: Request) -> BitbucketWebhookResources:
    injected = getattr(request.app.state, "bitbucket_webhook_resources", None)
    if injected is not None:
        return cast(BitbucketWebhookResources, injected)
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if not isinstance(resources, ReviewsApiResources):
        raise HTTPException(status_code=503, detail="bitbucket_webhook_unavailable")
    return BitbucketWebhookResources(
        authenticator=SqlAlchemyBitbucketWebhookAuthenticator(
            resources.session_factory,
            _cipher_from_environment(),
        ),
        receiver=ReceiveWebhookDelivery(
            provider="bitbucket",
            uow_factory=partial(
                SqlAlchemyBitbucketWebhookReceiptUnitOfWork,
                resources.session_factory,
            ),
        ),
    )


@dataclass(frozen=True)
class BitbucketWebhookReplayResources:
    receiver: ReceiveWebhookDelivery
    enqueuer: TryEnqueueWebhookRun


@asynccontextmanager
async def bitbucket_webhook_replay_resources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    cipher: BitbucketCredentialCipher,
    run_publisher: RunMessagePublisher,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[BitbucketWebhookReplayResources]:
    """Borrow the database pool and publisher; own only the fixed-origin HTTP client."""
    async with httpx.AsyncClient(
        timeout=10.0,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:
        target_reader = SqlAlchemyBitbucketTargetReader(session_factory)
        credentials = BitbucketRequestCredentials(target_reader, cipher)
        enqueuer = TryEnqueueWebhookRun(
            eligibility=DetermineBitbucketEligibility(
                candidates=SqlAlchemyBitbucketEligibilityCandidateStore(session_factory)
            ),
            uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(session_factory),
            publisher=run_publisher,
            now=now,
        )
        projector = ProjectBitbucketPullRequest(
            authority=SqlAlchemyBitbucketProjectionAuthority(session_factory),
            current=BitbucketCurrentPullRequestProvider(client, credentials),
            uow_factory=lambda: SqlAlchemyBitbucketProjectionUnitOfWork(session_factory),
            push_candidates=SqlAlchemyBitbucketPushCandidateReader(session_factory),
            enqueuer=enqueuer,
            index_refresh=RequestBitbucketIndexRefresh(
                lambda: SqlAlchemyBitbucketIndexRefreshUnitOfWork(session_factory)
            ),
        )
        receiver = ReceiveWebhookDelivery(
            provider="bitbucket",
            uow_factory=lambda: SqlAlchemyBitbucketWebhookReceiptUnitOfWork(session_factory),
            dispatcher=BitbucketWebhookDispatchAdapter(projector),
            action_of=lambda receipt: receipt.event_name.partition(":")[2],
            now=now,
        )
        yield BitbucketWebhookReplayResources(receiver, enqueuer)
