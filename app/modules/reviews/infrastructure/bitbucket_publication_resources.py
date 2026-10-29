"""Per-publication Bitbucket Cloud authority and bounded fixed-origin HTTP."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import timedelta
from urllib.parse import parse_qsl, unquote, urlsplit
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketBindingGenerationChanged,
    BitbucketCredentialGenerationChanged,
    BitbucketCredentialUnavailable,
    BitbucketRequestAuthority,
    BitbucketTargetInactive,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.commit_hash import pr_commit_hash
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketRunOrigin
from app.modules.integrations.bitbucket.infrastructure.request_credentials import (
    BitbucketRequestCredentials,
    SqlAlchemyBitbucketTargetReader,
)
from app.modules.integrations.bitbucket.infrastructure.retry_after import parse_retry_after
from app.modules.reviews.application.bitbucket_publication_claims import (
    BitbucketPublicationClaims,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.publish_run_review import (
    ObsoletePublication,
    PublishContext,
    PublishError,
    publication_matches,
)
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.bitbucket_publication_claims import (
    SqlAlchemyBitbucketPublicationClaimUnitOfWork,
)
from app.modules.reviews.infrastructure.bitbucket_review_publication import (
    BitbucketPullRequestCommentGateway,
)

_ORIGIN = "https://api.bitbucket.org"
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
_MAX_TOTAL_BYTES = 16 * 1024 * 1024
_COORDINATE_ERROR = re.compile(rb"\b(?:inline|line|path|position|diff)\b", re.I)
_LOGGER = logging.getLogger(__name__)


class BitbucketPublicationHttp:
    """HTTP methods constrained to one immutable destination UUID and PR number."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        credentials: BitbucketRequestAuthority,
        target: BoundBitbucketTarget,
        *,
        pr_number: int,
        checkpoint: RunProviderCheckpoint,
    ) -> None:
        if type(pr_number) is not int or pr_number <= 0:
            raise ValueError("invalid Bitbucket PR number")
        self._client = client
        self._credentials = credentials
        self._target = target
        self._checkpoint = checkpoint
        self._root = (
            f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/pullrequests/{pr_number}"
        )
        self._total_bytes = 0

    async def checkpoint(self) -> None:
        await self._checkpoint()
        try:
            await self._credentials.checkpoint(self._target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
            BitbucketCredentialUnavailable,
        ):
            raise RunCancelled("installation_unavailable") from None

    def _page_url(self, cursor: str) -> str:
        if len(cursor) > 2048 or not cursor.isascii():
            raise PublishError("retryable", "Bitbucket comment pagination is unsafe")
        parsed = urlsplit(cursor)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.bitbucket.org"
            or parsed.fragment
            or unquote(parsed.path) != unquote(f"{self._root}/comments")
        ):
            raise PublishError("retryable", "Bitbucket comment pagination is unsafe")
        try:
            params = parse_qsl(parsed.query, keep_blank_values=False, strict_parsing=True)
        except ValueError:
            raise PublishError("retryable", "Bitbucket comment pagination is unsafe") from None
        keys = [key for key, _ in params]
        if (
            not params
            or len(keys) != len(set(keys))
            or set(keys) - {"page", "pagelen", "cursor"}
            or any(len(value) > 256 for _, value in params)
        ):
            raise PublishError("retryable", "Bitbucket comment pagination is unsafe")
        return cursor

    async def _request(
        self, method: str, url: str, *, json_body: dict[str, object] | None = None
    ) -> object:
        await self.checkpoint()
        try:
            credential = await self._credentials.credential_for(self._target)
        except (
            BitbucketTargetInactive,
            BitbucketCredentialGenerationChanged,
            BitbucketBindingGenerationChanged,
            BitbucketCredentialUnavailable,
        ):
            raise RunCancelled("installation_unavailable") from None
        # Credentials are short lived; the final fence is immediately before the write.
        if method in {"POST", "PUT"}:
            await self.checkpoint()
        try:
            async with self._client.stream(
                method,
                url,
                json=json_body,
                headers={"Authorization": f"Bearer {credential.token}"},
                follow_redirects=False,
                timeout=10.0,
            ) as response:
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE_BYTES or self._total_bytes + size > _MAX_TOTAL_BYTES:
                        raise PublishError(
                            "retryable", "Bitbucket publication response is too large"
                        )
                    chunks.append(chunk)
                body = b"".join(chunks)
                self._total_bytes += size
                status = response.status_code
                retry_after = parse_retry_after(response.headers.get("Retry-After"))
                if status == 429 and retry_after is None:
                    retry_after = timedelta(hours=1)
        except httpx.RequestError:
            raise PublishError("retryable", "Bitbucket publication request failed") from None
        if status not in ({201} if method == "POST" else {200}):
            if (
                method == "POST"
                and json_body is not None
                and "inline" in json_body
                and status in {400, 422}
                and _COORDINATE_ERROR.search(body[:8192])
            ):
                raise PublishError(
                    "coordinates", "Bitbucket rejected inline coordinates", http_status=status
                )
            if status in {401, 403, 404} or 300 <= status < 400:
                raise PublishError(
                    "forbidden", "Bitbucket publication unavailable", http_status=status
                )
            raise PublishError(
                "retryable",
                "Bitbucket publication request failed",
                http_status=status,
                retry_after=retry_after,
            )
        try:
            return json.loads(body)
        except (ValueError, RecursionError):
            raise PublishError("retryable", "Bitbucket publication response is malformed") from None

    async def pull_request(self) -> object:
        return await self._request("GET", f"{_ORIGIN}{self._root}")

    async def current_user(self) -> object:
        return await self._request("GET", f"{_ORIGIN}/2.0/user")

    async def source_commit(self, repository_uuid: UUID, head_sha: str) -> object:
        if type(repository_uuid) is not UUID or not isinstance(head_sha, str):
            raise PublishError("retryable", "Bitbucket fork source is unavailable")
        try:
            pr_commit_hash(head_sha)
        except ValueError:
            raise PublishError("retryable", "Bitbucket fork source is unavailable") from None
        return await self._request(
            "GET",
            f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{repository_uuid}%7D/commit/{head_sha}",
        )

    async def list_comments(self, cursor: str | None) -> object:
        url = (
            self._page_url(cursor)
            if cursor is not None
            else f"{_ORIGIN}{self._root}/comments?pagelen=100"
        )
        return await self._request("GET", url)

    async def post_comment(self, payload: dict[str, object]) -> object:
        return await self._request("POST", f"{_ORIGIN}{self._root}/comments", json_body=payload)

    async def update_comment(self, comment_id: int, payload: dict[str, object]) -> object:
        if type(comment_id) is not int or comment_id <= 0:
            raise PublishError("retryable", "Bitbucket comment id is malformed")
        return await self._request(
            "PUT", f"{_ORIGIN}{self._root}/comments/{comment_id}", json_body=payload
        )


class BitbucketInstalledPublicationResources:
    """Rebuild a Run's immutable binding authority for each publication attempt."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        cipher: BitbucketCredentialCipher,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        heartbeat_interval_seconds: float = 20.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("invalid Bitbucket publication heartbeat interval")
        self._sessions = session_factory
        self._reader = SqlAlchemyBitbucketTargetReader(session_factory)
        self._credentials = BitbucketRequestCredentials(self._reader, cipher)
        self._claims = BitbucketPublicationClaims(
            lambda: SqlAlchemyBitbucketPublicationClaimUnitOfWork(session_factory)
        )
        self._transports = transport_factory
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    @asynccontextmanager
    async def open(
        self, context: PublishContext, *, checkpoint: RunProviderCheckpoint
    ) -> AsyncIterator[BitbucketPullRequestCommentGateway]:
        vcs = context.vcs_input
        identity = context.publication_identity
        if (
            context.provider != "bitbucket"
            or not context.origin_active
            or context.workspace_id is None
            or vcs is None
            or identity is None
            or vcs.locator.repository_uuid is None
            or vcs.locator.origin_provider_installation_id is None
            or vcs.locator.provider != "bitbucket"
            or vcs.base_ref is None
            or vcs.head_sha != context.head_sha
            or vcs.locator.number != context.pr_number
            or not publication_matches(context, context)
        ):
            raise RunCancelled("installation_unavailable")
        async with self._sessions() as session:
            origin = await session.scalar(
                select(BitbucketRunOrigin).where(BitbucketRunOrigin.run_id == context.run_id)
            )
        if (
            origin is None
            or origin.workspace_id != context.workspace_id
            or origin.provider_installation_id != identity.origin_provider_installation_id
            or origin.repository_id != identity.repository_id
            or origin.repository_uuid != vcs.locator.repository_uuid
            or origin.pr_number != context.pr_number
            or origin.head_sha != context.head_sha
        ):
            raise RunCancelled("installation_unavailable")
        target = BoundBitbucketTarget(
            origin.provider_installation_id,
            origin.workspace_id,
            origin.repository_id,
            origin.repository_uuid,
            origin.credential_generation,
            origin.binding_generation,
        )
        await checkpoint()
        claim = await self._claims.acquire(context.run_id)
        lost = asyncio.Event()

        async def claim_checkpoint() -> None:
            if lost.is_set():
                raise ObsoletePublication
            await checkpoint()
            try:
                await self._claims.renew(claim)
            except ObsoletePublication:
                lost.set()
                raise

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                try:
                    await self._claims.renew(claim)
                except Exception:
                    lost.set()
                    _LOGGER.warning(
                        "Bitbucket publication claim renewal failed for run %s", context.run_id
                    )
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            await claim_checkpoint()
            try:
                await self._reader.checkpoint(target)
            except (
                BitbucketTargetInactive,
                BitbucketCredentialGenerationChanged,
                BitbucketBindingGenerationChanged,
                BitbucketCredentialUnavailable,
            ):
                raise RunCancelled("installation_unavailable") from None
            async with httpx.AsyncClient(
                transport=self._transports() if self._transports else None,
                trust_env=False,
                follow_redirects=False,
            ) as client:
                port = BitbucketPublicationHttp(
                    client,
                    self._credentials,
                    target,
                    pr_number=context.pr_number,
                    checkpoint=claim_checkpoint,
                )
                yield BitbucketPullRequestCommentGateway(
                    port,
                    run_id=context.run_id,
                    repository_uuid=target.repository_uuid,
                    pinned_target_branch=vcs.base_ref,
                )
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self._claims.release(claim)
