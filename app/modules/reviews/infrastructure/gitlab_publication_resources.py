"""Execution-local installed GitLab publication, borrowing the sole T8 authority."""

from __future__ import annotations

import ssl
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from contextlib import asynccontextmanager, contextmanager
from types import TracebackType
from typing import Literal

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.gitlab.application.manage_connection import GitLabOriginPolicy
from app.modules.integrations.gitlab.application.request_credentials import GitLabRequestAuthority
from app.modules.integrations.gitlab.infrastructure.run_credentials import GitLabRunCredentials
from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabPublicationResponse,
    GitLabRunHttp,
    GitLabRunHttpError,
    JsonValue,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.publish_run_review import (
    GitLabPublicationGateway,
    PublishContext,
    PublishError,
    ReviewSubmission,
    SubmittedReview,
)
from app.modules.reviews.application.run_failures import RequiredSourceUnavailable, RunCancelled
from app.modules.reviews.infrastructure.gitlab_review_publication import (
    GitLabMergeRequestDiscussionGateway,
)


class GitLabInstalledPublicationResources:
    """Stateless shared factory; one credentials generation and HTTP ledger per open."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        authority: GitLabRequestAuthority,
        origin_policy: GitLabOriginPolicy,
        ssl_context: ssl.SSLContext | None = None,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
    ) -> None:
        if ssl_context is not None and (
            ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname
        ):
            raise ValueError("GitLab publication requires verified TLS")
        self._sessions = session_factory
        self._authority = authority
        self._policy = origin_policy
        self._ssl_context = ssl_context
        self._transports = transport_factory

    @asynccontextmanager
    async def open(
        self,
        context: PublishContext,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> AsyncIterator[GitLabPublicationGateway]:
        expected = context.vcs_input
        if (
            context.provider != "gitlab"
            or not context.origin_active
            or expected is None
            or context.workspace_id is None
            or expected.locator.provider != "gitlab"
            or expected.head_sha != context.head_sha
            or expected.locator.repository_external_id != context.project_id
            or expected.locator.installation_external_id != context.installation_id
            or expected.locator.repository_full_name != context.repository_full_name
            or expected.locator.number != context.pr_number
        ):
            raise RunCancelled("installation_unavailable")
        credentials = GitLabRunCredentials(
            session_factory=self._sessions,
            authority=self._authority,
            run_id=context.run_id,
            expected_workspace_id=context.workspace_id,
            expected_input=expected,
            checkpoint=checkpoint,
        )
        requests = _PublicationRequests(
            context,
            credentials,
            self._policy,
            self._ssl_context,
            self._transports,
        )
        try:
            yield _InstalledGateway(requests)
        except BaseException as primary:
            await requests.close(type(primary), primary, primary.__traceback__)
            raise
        else:
            await requests.close(None, None, None)


@contextmanager
def _publication_errors() -> Iterator[None]:
    """The same narrow conversion at request and final pinned-authority fences."""
    try:
        yield
    except RequiredSourceUnavailable as exc:
        raise PublishError(
            "retryable",
            "GitLab publication authority unavailable",
            retry_after=exc.retry_after,
        ) from None
    except GitLabRunHttpError as exc:
        raise PublishError(
            "forbidden" if exc.status_code in (401, 403) else "retryable",
            "GitLab publication request failed",
            http_status=exc.status_code,
            retry_after=exc.retry_after,
        ) from None


class _InstalledGateway:
    def __init__(self, requests: _PublicationRequests) -> None:
        self._requests = requests
        self._gateway = GitLabMergeRequestDiscussionGateway(requests=requests)

    async def submit_review(
        self,
        submission: ReviewSubmission,
        *,
        project_id: int,
    ) -> SubmittedReview:
        return await self._gateway.submit_review(submission, project_id=project_id)

    async def checkpoint(self) -> None:
        await self._requests.checkpoint()


class _PublicationRequests:
    def __init__(
        self,
        context: PublishContext,
        credentials: GitLabRunCredentials,
        policy: GitLabOriginPolicy,
        ssl_context: ssl.SSLContext | None,
        transports: Callable[[], httpx.AsyncBaseTransport] | None,
    ) -> None:
        self._context = context
        self._credentials = credentials
        self._policy = policy
        self._ssl_context = ssl_context
        self._transports = transports
        self._http: GitLabRunHttp | None = None

    async def checkpoint(self) -> None:
        if self._http is None:
            raise RuntimeError("GitLab publication HTTP is not open")
        with _publication_errors():
            await self._http.checkpoint()

    async def close(
        self,
        kind: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self._http is not None:
            await self._http.__aexit__(kind, exc, traceback)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, int] | None = None,
        json_body: Mapping[str, JsonValue] | None = None,
    ) -> GitLabPublicationResponse:
        prefix = (
            f"/api/v4/projects/{self._context.project_id}/merge_requests/{self._context.pr_number}/"
        )
        if path not in {prefix + "versions", prefix + "discussions"}:
            raise RunCancelled("installation_unavailable")
        kind: Literal["versions", "discussions"] = (
            "versions" if path.endswith("/versions") else "discussions"
        )
        if (
            method not in {"GET", "POST"}
            or method == "POST"
            and kind != "discussions"
            or params is not None
            and set(params) - {"page", "per_page"}
            or method == "GET"
            and json_body is not None
            or method == "POST"
            and (params is not None or json_body is None)
        ):
            raise RunCancelled("installation_unavailable")
        with _publication_errors():
            if self._http is None:
                # Retain ownership before awaited startup. A failed startup has already
                # been closed by GitLabRunHttp and may be retried with a fresh transport.
                try:
                    self._http = GitLabRunHttp(
                        credentials=self._credentials,
                        origin_policy=self._policy,
                        ssl_context=self._ssl_context,
                        transport=self._transports() if self._transports else None,
                    )
                    await self._http.__aenter__()
                except (httpx.HTTPError, OSError, TimeoutError):
                    self._http = None
                    raise PublishError("retryable", "GitLab publication startup failed") from None
            if method == "POST":
                assert json_body is not None
                return await self._http.post_discussion(json_body)
            return await self._http.get_publication_json(
                kind,
                page=(params or {}).get("page"),
                per_page=(params or {}).get("per_page"),
            )
