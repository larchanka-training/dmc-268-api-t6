"""Composition root for the reviews HTTP API's database dependencies."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import AsyncExitStack, asynccontextmanager, nullcontext
from dataclasses import dataclass
from functools import partial
from typing import Annotated, cast

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.bootstrap.installation_onboarding import InstallationOnboarding
from app.bootstrap.portal_auth import get_auth_scope
from app.bootstrap.reconciler import reconciler_loop
from app.modules.auth.application.scope import AuthScope
from app.modules.auth.infrastructure.sessions import SqlAlchemyAuthSessionUnitOfWork
from app.modules.integrations.webhooks.api.dispatch import GitHubWebhookDispatchAdapter, action_of
from app.modules.integrations.webhooks.application.github_installation_dispatch import (
    GitHubInstallationDeliveryDispatcher,
)
from app.modules.integrations.webhooks.application.installation_event_projector import (
    InstallationRepositoryDetailsProvider,
    InstallationRepositoryLabelProvider,
    InstallationRepositoryTreeProvider,
)
from app.modules.integrations.webhooks.application.receive_github_delivery import (
    GitHubWebhookReceiptUnitOfWork,
    ReceiveGitHubDelivery,
)
from app.modules.integrations.webhooks.infrastructure.failure_category import classify_failure
from app.modules.integrations.webhooks.infrastructure.github_current_pull_request import (
    HttpGitHubCurrentPullRequestProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_resolver import (
    SqlAlchemyGitHubInstallationResolver,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    GitHubInstallationAccessTokenProvider,
    GitHubInstallationTreeProvider,
    TokenErrorClassifyingProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_details import (
    GitHubInstallationRepositoryDetailsProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_repository_labels import (
    GitHubRepositoryLabelProvider,
)
from app.modules.integrations.webhooks.infrastructure.github_webhook_receipts import (
    SqlAlchemyGitHubWebhookReceiptUnitOfWork,
)
from app.modules.repositories.application.repository_settings import (
    RepositorySettingsUowFactory,
)
from app.modules.repositories.infrastructure.repository_settings import (
    SqlAlchemyRepositorySettingsUnitOfWork,
)
from app.modules.reviews.application.cancel_run import (
    CancellationSignals,
    CancelRun,
    CancelRunRepository,
)
from app.modules.reviews.application.determine_ci_eligibility import DetermineCiEligibility
from app.modules.reviews.application.get_run import RunDetailRepository
from app.modules.reviews.application.get_run_actions import RunActionsRepository
from app.modules.reviews.application.get_run_comments import RunCommentsRepository
from app.modules.reviews.application.get_run_diff import RunDiffRepository
from app.modules.reviews.application.get_run_file_lines import BlobCache, RunFileRepository
from app.modules.reviews.application.list_pulls import PullRequestRepository
from app.modules.reviews.application.list_runs import RunRepository
from app.modules.reviews.application.project_github_pull_request import ProjectGitHubPullRequest
from app.modules.reviews.application.publish_cancellation_signals import (
    PublishCancellationSignals,
)
from app.modules.reviews.application.rerun_run import RerunUnitOfWork
from app.modules.reviews.application.run_events import (
    InMemoryRunUpdateHub,
    RunAccessRepository,
    RunUpdatePublisher,
)
from app.modules.reviews.application.trigger_from_delivery import TriggerFromDelivery
from app.modules.reviews.application.try_enqueue_webhook_run import (
    RunMessagePublisher,
    TryEnqueueWebhookRun,
)
from app.modules.reviews.infrastructure.amqp import LazyAmqpPublisher
from app.modules.reviews.infrastructure.blob_cache import SqlAlchemyBlobCache
from app.modules.reviews.infrastructure.ci_eligibility_candidates import (
    SqlAlchemyEligibilityCandidateStore,
)
from app.modules.reviews.infrastructure.github_ci import HttpGitHubCurrentHeadCiProvider
from app.modules.reviews.infrastructure.github_pull_request_projection import (
    SqlAlchemyPullRequestProjectionLock,
    SqlAlchemyPullRequestProjectionUnitOfWork,
)
from app.modules.reviews.infrastructure.pull_request_queries import SqlAlchemyPullRequestQueries
from app.modules.reviews.infrastructure.rerun_store import SqlAlchemyRerunUnitOfWork
from app.modules.reviews.infrastructure.run_repository import (
    SqlAlchemyCancelRunUnitOfWork,
    SqlAlchemyRunRepository,
)
from app.modules.reviews.infrastructure.webhook_run_targets import (
    SqlAlchemyRunTriggerUnitOfWork,
)
from app.modules.reviews.infrastructure.webhook_runs import SqlAlchemyWebhookRunUnitOfWork
from app.modules.workspaces.application.link_github_installations import LinkGitHubInstallations
from app.modules.workspaces.infrastructure.github_installation_links import (
    SqlAlchemyGitHubInstallationLinkUnitOfWork,
)
from app.modules.workspaces.infrastructure.github_user_installations import (
    HttpGitHubUserInstallationsProvider,
)


@dataclass(frozen=True)
class GitHubAuthHttpClients:
    oauth: httpx.AsyncClient
    api: httpx.AsyncClient


class ReviewsApiResources:
    """Long-lived database resources shared by all HTTP requests."""

    def __init__(
        self,
        engine: AsyncEngine,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._engine = engine
        self._session_factory = session_factory

    @classmethod
    def from_database_url(cls, database_url: str) -> ReviewsApiResources:
        engine = create_async_engine(database_url, pool_pre_ping=True)
        return cls(engine, async_sessionmaker(engine, expire_on_commit=False))

    def run_repository(
        self,
        scope: AuthScope | None = None,
        *,
        allow_unscoped: bool = False,
    ) -> (
        RunRepository
        | RunDetailRepository
        | RunCommentsRepository
        | RunActionsRepository
        | RunDiffRepository
        | RunFileRepository
        | CancelRunRepository
    ):
        return SqlAlchemyRunRepository(self._session_factory, scope, allow_unscoped=allow_unscoped)

    def file_blob_cache(self) -> BlobCache:
        return SqlAlchemyBlobCache(self._session_factory)

    def github_webhook_receipts(self) -> SqlAlchemyGitHubWebhookReceiptUnitOfWork:
        return SqlAlchemyGitHubWebhookReceiptUnitOfWork(self._session_factory)

    def auth_session_uow(self) -> SqlAlchemyAuthSessionUnitOfWork:
        return SqlAlchemyAuthSessionUnitOfWork(self._session_factory)

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        return self._session_factory

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    def github_installation_linker(self, *, client: httpx.AsyncClient) -> LinkGitHubInstallations:
        """Compose callback reconciliation after the caller obtains a GitHub user token."""
        return LinkGitHubInstallations(
            github=HttpGitHubUserInstallationsProvider(client),
            uow_factory=lambda: SqlAlchemyGitHubInstallationLinkUnitOfWork(self._session_factory),
        )

    def github_delivery_receiver(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
        bot_login: str | None = None,
        run_publisher: RunMessagePublisher | None = None,
        app_id: int | None = None,
    ) -> ReceiveGitHubDelivery:
        return ReceiveGitHubDelivery(
            uow_factory=self.github_webhook_receipts,
            dispatcher=self.github_installation_delivery_dispatcher(
                client=client,
                token_provider=token_provider,
                bot_login=bot_login,
                run_publisher=run_publisher,
                app_id=app_id,
            ),
            action_of=action_of,
            classify_failure=classify_failure,
        )

    def installation_onboarding(
        self,
        tree_provider: InstallationRepositoryTreeProvider,
        label_provider: InstallationRepositoryLabelProvider,
        details_provider: InstallationRepositoryDetailsProvider,
    ) -> InstallationOnboarding:
        """Compose repository onboarding with the API process's database pool.

        A GitHub installation delivery consumer supplies the tree provider and
        then calls the returned handler with its already-validated typed event.
        This keeps the creation boundary callable from the current app without
        making the reviews API own webhook transport or delivery dispatch.
        """
        return InstallationOnboarding(
            session_factory=self._session_factory,
            tree_provider=tree_provider,
            label_provider=label_provider,
            details_provider=details_provider,
        )

    def github_installation_delivery_dispatcher(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: GitHubInstallationAccessTokenProvider,
        bot_login: str | None = None,
        run_publisher: RunMessagePublisher | None = None,
        app_id: int | None = None,
    ) -> GitHubWebhookDispatchAdapter:
        """Compose the verified-delivery application boundary.

        With a ``run_publisher`` and the App id, PR, label and CI events create Runs
        through ``try_enqueue`` (T1) and cancellations signal the worker at once (T6).
        """
        # Onboarding tells an installation-wide token failure from a repository error.
        onboarding_tokens = TokenErrorClassifyingProvider(token_provider)
        tree_provider = GitHubInstallationTreeProvider(
            client=client,
            token_provider=onboarding_tokens,
        )
        label_provider = GitHubRepositoryLabelProvider(
            client=client,
            token_provider=onboarding_tokens,
        )
        details_provider = GitHubInstallationRepositoryDetailsProvider(
            client=client,
            token_provider=onboarding_tokens,
        )
        signals = (
            PublishCancellationSignals(
                uow_factory=partial(
                    SqlAlchemyPullRequestProjectionUnitOfWork, self._session_factory
                ),
                publisher=run_publisher,
            )
            if run_publisher is not None
            else None
        )
        run_trigger = (
            TriggerFromDelivery(
                uow_factory=partial(SqlAlchemyRunTriggerUnitOfWork, self._session_factory),
                enqueuer=TryEnqueueWebhookRun(
                    eligibility=DetermineCiEligibility(
                        candidates=SqlAlchemyEligibilityCandidateStore(self._session_factory),
                        ci=HttpGitHubCurrentHeadCiProvider(
                            client=client, token_provider=token_provider
                        ),
                        own_app_id=app_id,
                    ),
                    uow_factory=partial(SqlAlchemyWebhookRunUnitOfWork, self._session_factory),
                    publisher=run_publisher,
                    cancellation_signals=signals,
                ),
            )
            if run_publisher is not None and app_id is not None
            else None
        )
        projector = (
            ProjectGitHubPullRequest(
                uow_factory=lambda: SqlAlchemyPullRequestProjectionUnitOfWork(
                    self._session_factory
                ),
                bot_login=bot_login,
                current_provider=HttpGitHubCurrentPullRequestProvider(
                    client=client,
                    token_provider=token_provider,
                ),
                projection_lock=SqlAlchemyPullRequestProjectionLock(self._engine),
                cancellation_signals=signals,
            )
            if bot_login is not None
            else None
        )
        dispatcher = GitHubInstallationDeliveryDispatcher(
            resolver=SqlAlchemyGitHubInstallationResolver(self._session_factory),
            onboarding=self.installation_onboarding(
                tree_provider, label_provider, details_provider
            ),
            pull_request_projector=projector,
            label_intent_projector=projector,
            run_trigger=run_trigger,
        )
        return GitHubWebhookDispatchAdapter(dispatcher)

    async def aclose(self) -> None:
        await self._engine.dispose()


def _resources(request: Request) -> ReviewsApiResources:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if resources is None:
        raise RuntimeError("DATABASE_URL must be configured to serve review runs")
    return cast(ReviewsApiResources, resources)


def get_run_repository(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
) -> (
    RunRepository
    | RunDetailRepository
    | RunCommentsRepository
    | RunActionsRepository
    | RunDiffRepository
    | RunFileRepository
    | CancelRunRepository
    | RunAccessRepository
):
    """Provide a request-scoped repository backed by the application pool."""
    return _resources(request).run_repository(scope)


def get_repository_settings(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
) -> RepositorySettingsUowFactory:
    return partial(
        SqlAlchemyRepositorySettingsUnitOfWork, _resources(request).session_factory, scope
    )


def get_rerun_uow_factory(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
) -> Callable[[], RerunUnitOfWork]:
    return partial(SqlAlchemyRerunUnitOfWork, _resources(request).session_factory, scope)


def get_run_event_hub(request: Request) -> InMemoryRunUpdateHub:
    hub = getattr(request.app.state, "run_update_hub", None)
    if not isinstance(hub, InMemoryRunUpdateHub):
        raise RuntimeError("run_update_hub is not configured on app.state")
    return hub


def get_cancel_run(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
    signals: Annotated[CancellationSignals | None, Depends(get_cancellation_signals)],
    event_hub: Annotated[RunUpdatePublisher, Depends(get_run_event_hub)],
) -> CancelRun:
    resources = _resources(request)
    uow_factory = partial(SqlAlchemyCancelRunUnitOfWork, resources.session_factory, scope)
    return CancelRun(
        uow_factory=uow_factory,
        event_publisher=event_hub,
        signals=signals,
    )


def get_pull_requests(
    request: Request,
    scope: Annotated[AuthScope, Depends(get_auth_scope)],
) -> PullRequestRepository:
    return SqlAlchemyPullRequestQueries(_resources(request).session_factory, scope)


def get_run_publisher(request: Request) -> RunMessagePublisher | None:
    """The API process publisher (rerun, T6 close signal); ``None`` without RabbitMQ."""
    publisher = getattr(request.app.state, "run_publisher", None)
    return cast(RunMessagePublisher | None, publisher)


def get_cancellation_signals(request: Request) -> CancellationSignals | None:
    publisher = get_run_publisher(request)
    if publisher is None:
        return None
    return PublishCancellationSignals(
        uow_factory=partial(
            SqlAlchemyPullRequestProjectionUnitOfWork, _resources(request).session_factory
        ),
        publisher=publisher,
    )


def get_file_blob_cache(request: Request) -> BlobCache:
    """Provide the file cache backed by the application pool."""
    return _resources(request).file_blob_cache()


def get_github_webhook_receipt_uow_factory(
    request: Request,
) -> Callable[[], GitHubWebhookReceiptUnitOfWork]:
    resources = getattr(request.app.state, "reviews_api_resources", None)
    if not isinstance(resources, ReviewsApiResources):
        raise HTTPException(status_code=503, detail="GitHub webhook is not configured")
    return resources.github_webhook_receipts


@asynccontextmanager
async def reviews_api_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Create process-scoped pools and HTTP clients and dispose them at shutdown."""
    async with AsyncExitStack() as stack:
        oauth_client = await stack.enter_async_context(
            httpx.AsyncClient(base_url="https://github.com", timeout=10)
        )
        api_client = await stack.enter_async_context(
            httpx.AsyncClient(
                base_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"), timeout=10
            )
        )
        app.state.github_auth_http_clients = GitHubAuthHttpClients(oauth_client, api_client)
        database_url = os.environ.get("DATABASE_URL")
        github_webhook_secret = os.environ.get("GITHUB_WEBHOOK_SECRET")
        resources: ReviewsApiResources | None = None
        if database_url is not None:
            resources = ReviewsApiResources.from_database_url(database_url)
            app.state.reviews_api_resources = resources
            if github_webhook_secret is not None:
                app.state.github_webhook_secret = github_webhook_secret
        rabbitmq_url = os.environ.get("RABBITMQ_URL")
        if resources is not None and rabbitmq_url:
            publisher = LazyAmqpPublisher(rabbitmq_url)
            stack.push_async_callback(publisher.aclose)
            app.state.run_publisher = publisher
        try:
            async with (
                reconciler_loop(
                    resources.engine, resources.session_factory, os.environ.get("RABBITMQ_URL")
                )
                if resources is not None
                else nullcontext()
            ):
                yield
        finally:
            if resources is not None:
                if github_webhook_secret is not None:
                    del app.state.github_webhook_secret
                await resources.aclose()
                del app.state.reviews_api_resources
            del app.state.github_auth_http_clients
            if hasattr(app.state, "run_publisher"):
                del app.state.run_publisher
