"""Typed, default-disabled webhook resources over the caller's shared DB pool."""

from __future__ import annotations

import logging
import ssl
import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID

import httpx
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.api.dispatch import GitLabWebhookDispatchAdapter, action_of
from app.modules.integrations.gitlab.application.accept_webhook import AcceptGitLabWebhook
from app.modules.integrations.gitlab.application.authenticate_webhook import (
    AuthenticateGitLabWebhook,
    GitLabWebhookAuthStatus,
)
from app.modules.integrations.gitlab.application.eligibility import DetermineGitLabEligibility
from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabRequestCredentials,
    GitLabRequestUnavailable,
)
from app.modules.integrations.gitlab.application.webhook_receipts import GitLabReceiptError
from app.modules.integrations.gitlab.infrastructure.connection_store import CredentialCipherAdapter
from app.modules.integrations.gitlab.infrastructure.credential_cipher import CredentialCipher
from app.modules.integrations.gitlab.infrastructure.current_merge_request import (
    HttpGitLabCurrentMergeRequestProvider,
)
from app.modules.integrations.gitlab.infrastructure.request_credentials import (
    SqlAlchemyGitLabTargetReader,
)
from app.modules.integrations.gitlab.infrastructure.settings import GitLabRuntimeSettings
from app.modules.integrations.gitlab.infrastructure.webhook_auth import (
    GitLabWebhookSecretVerifierAdapter,
    SqlAlchemyGitLabWebhookAuthReader,
)
from app.modules.integrations.gitlab.infrastructure.webhook_receipts import (
    SqlAlchemyGitLabWebhookReceiptUnitOfWork,
)
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    InstallationDeliveryDispatchStatus,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    ReceiveWebhookDelivery,
    WebhookReceipt,
)
from app.modules.reviews.application.project_gitlab_merge_request import (
    HandleGitLabMergeRequest,
    ProjectGitLabMergeRequest,
)
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.gitlab_eligibility_candidates import (
    SqlAlchemyGitLabEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.gitlab_merge_request_projection import (
    SqlAlchemyGitLabMergeRequestProjectionLock,
    SqlAlchemyGitLabMergeRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork


class GitLabWebhookAuthenticator(Protocol):
    async def execute(
        self, *, connection_id: UUID, secret: str | None
    ) -> GitLabWebhookAuthStatus: ...


class GitLabWebhookAcceptor(Protocol):
    async def execute(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus: ...


class GitLabWebhookUnavailable(Exception):
    """Constant transport-safe failure; never render an underlying database error."""

    def __init__(self) -> None:
        super().__init__("gitlab_webhook_unavailable")


class GitLabWebhookResources:
    def __init__(
        self,
        *,
        authenticator: GitLabWebhookAuthenticator,
        receiver: GitLabWebhookAcceptor,
        intake_enabled: bool = False,
    ) -> None:
        self._authenticator = authenticator
        self._receiver = receiver
        self._intake_enabled = intake_enabled
        self._admission = AcceptGitLabWebhook(authenticator=self, receiver=self)

    @property
    def intake_enabled(self) -> bool:
        return self._intake_enabled

    async def authenticate(
        self, *, connection_id: UUID, secret: str | None
    ) -> GitLabWebhookAuthStatus:
        if not self.intake_enabled:
            raise GitLabWebhookUnavailable()
        try:
            return await self._authenticator.execute(connection_id=connection_id, secret=secret)
        except Exception as error:
            logging.getLogger(__name__).error(
                "GitLab webhook authenticate failed",
                extra={"error_type": type(error).__name__},
            )
            raise GitLabWebhookUnavailable() from None

    async def accept(self, delivery: WebhookReceipt) -> InstallationDeliveryDispatchStatus:
        if not self.intake_enabled:
            raise GitLabWebhookUnavailable()
        try:
            return await self._receiver.execute(delivery)
        except GitLabReceiptError:
            raise
        except Exception as error:
            logging.getLogger(__name__).error(
                "GitLab webhook accept failed",
                extra={"error_type": type(error).__name__},
            )
            raise GitLabWebhookUnavailable() from None

    async def accept_authenticated(
        self, delivery: WebhookReceipt, *, secret: str | None
    ) -> InstallationDeliveryDispatchStatus:
        return await self._admission.execute(delivery, secret=secret)


def build_gitlab_webhook_resources(
    session_factory: async_sessionmaker[AsyncSession], *, intake_enabled: bool = False
) -> GitLabWebhookResources:
    """Borrow the existing sessionmaker; own no engine, HTTP client or global state."""
    return GitLabWebhookResources(
        intake_enabled=intake_enabled,
        authenticator=AuthenticateGitLabWebhook(
            reader=SqlAlchemyGitLabWebhookAuthReader(session_factory),
            verifier=GitLabWebhookSecretVerifierAdapter(),
        ),
        receiver=ReceiveWebhookDelivery(
            provider="gitlab",
            uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(session_factory),
            dispatcher=None,
        ),
    )


@asynccontextmanager
async def gitlab_webhook_lifespan(
    app: FastAPI,
    session_factory: async_sessionmaker[AsyncSession] | None,
    *,
    intake_enabled: bool = False,
) -> AsyncIterator[None]:
    """Borrow the reviews pool; remove our reference before its owner closes it."""
    if session_factory is not None:
        app.state.gitlab_webhook_resources = build_gitlab_webhook_resources(
            session_factory, intake_enabled=intake_enabled
        )
    try:
        yield
    finally:
        if session_factory is not None:
            del app.state.gitlab_webhook_resources


def get_gitlab_webhook_resources(request: Request) -> GitLabWebhookResources:
    resources = getattr(request.app.state, "gitlab_webhook_resources", None)
    if not isinstance(resources, GitLabWebhookResources) or not resources.intake_enabled:
        raise HTTPException(status_code=503, detail="gitlab_webhook_unavailable")
    return resources


@dataclass(frozen=True)
class GitLabWebhookReplayResources:
    receiver: ReceiveWebhookDelivery
    enqueuer: TryEnqueueWebhookRun


class GitLabWebhookReplayUnavailable(RuntimeError):
    def __init__(self) -> None:
        super().__init__("gitlab_webhook_replay_unavailable")


@asynccontextmanager
async def gitlab_webhook_replay_resources(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    settings: GitLabRuntimeSettings,
    run_publisher: RunMessagePublisher,
    connect_args: dict[str, Any] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[GitLabWebhookReplayResources]:
    """Borrow sessions/publisher; own HTTP and an independent same-schema lock channel.

    settings.database.database_url and connect_args MUST match the supplied data
    sessionmaker, including its schema/search_path. Finish all replay tasks before exit.
    An explicitly injected transport is an external test seam, not verified-TLS evidence.
    """
    stack = AsyncExitStack()
    preserve_primary = False

    async def close_owned(close: Callable[[], Awaitable[None]]) -> None:
        try:
            await close()
        except Exception as error:
            if not isinstance(error, GitLabRequestUnavailable):
                logging.getLogger(__name__).error(
                    "GitLab webhook replay resource cleanup failed",
                    extra={"error_type": type(error).__name__},
                )
            if not preserve_primary:
                raise GitLabWebhookReplayUnavailable() from None

    try:
        try:
            tls = ssl.create_default_context(cafile=settings.ssl_cert_file)
            codec = CredentialCipherAdapter(
                CredentialCipher(active_key_id=settings.active_key_id, keys=settings.keys)
            )
            reader = SqlAlchemyGitLabTargetReader(session_factory)
            authority = GitLabRequestCredentials(reader, codec)
            client = httpx.AsyncClient(
                verify=tls,
                trust_env=False,
                follow_redirects=False,
                timeout=10.0,
                transport=transport,
            )
            # Register ownership before transport startup, which can itself fail.
            stack.push_async_callback(close_owned, client.aclose)
            lock = SqlAlchemyGitLabMergeRequestProjectionLock(
                settings.database.database_url, connect_args=connect_args
            )
            stack.push_async_callback(close_owned, lock.aclose)
            await client.__aenter__()
            projector = ProjectGitLabMergeRequest(
                authority=reader,
                current_provider=HttpGitLabCurrentMergeRequestProvider(
                    client=client, authority=authority, origin_policy=settings.origin_policy
                ),
                projection_lock=lock,
                uow_factory=lambda: SqlAlchemyGitLabMergeRequestProjectionUnitOfWork(
                    session_factory
                ),
                now=now,
            )
            enqueuer = TryEnqueueWebhookRun(
                eligibility=DetermineGitLabEligibility(
                    candidates=SqlAlchemyGitLabEligibilityCandidateStore(session_factory)
                ),
                uow_factory=lambda: SqlAlchemyWebhookRunUnitOfWork(session_factory),
                publisher=run_publisher,
                now=now,
            )
            resources = GitLabWebhookReplayResources(
                receiver=ReceiveWebhookDelivery(
                    provider="gitlab",
                    uow_factory=lambda: SqlAlchemyGitLabWebhookReceiptUnitOfWork(session_factory),
                    dispatcher=GitLabWebhookDispatchAdapter(
                        HandleGitLabMergeRequest(projector=projector, enqueuer=enqueuer)
                    ),
                    action_of=action_of,
                    now=now,
                ),
                enqueuer=enqueuer,
            )
        except Exception as error:
            logging.getLogger(__name__).error(
                "GitLab webhook replay startup failed",
                extra={"error_type": type(error).__name__},
            )
            raise GitLabWebhookReplayUnavailable() from None
        # Body/application errors are outside startup translation.
        yield resources
    finally:
        preserve_primary = sys.exception() is not None
        await stack.aclose()
