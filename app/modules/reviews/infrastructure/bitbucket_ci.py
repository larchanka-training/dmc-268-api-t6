"""Bounded, fenced Bitbucket Cloud build status publication."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
from datetime import timedelta
from typing import Protocol
from uuid import UUID

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import aliased

from app.common.infrastructure.db.enums import CodeChangeState, RunState
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestAuthority,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.commit_hash import (
    pr_commit_hash,
    resolved_commit_hash,
)
from app.modules.integrations.bitbucket.infrastructure.models import (
    BitbucketConnection,
    BitbucketRunOrigin,
)
from app.modules.integrations.bitbucket.infrastructure.retry_after import parse_retry_after
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.bitbucket_status import (
    BitbucketStatusSnapshot,
    bitbucket_status_payload,
)
from app.modules.reviews.application.bitbucket_status_intents import (
    BitbucketStatusIntents,
    StatusClaim,
    status_key,
)
from app.modules.reviews.infrastructure.bitbucket_run_origin import active_bitbucket_run_origin
from app.modules.reviews.infrastructure.models import CodeChange, Finding, Run
from app.modules.reviews.infrastructure.run_origin import active_origin_condition
from app.modules.reviews.infrastructure.saved_review_results import load_review_results

_ORIGIN = "https://api.bitbucket.org"
_MAX_BODY_BYTES = 64 * 1024
_LOGGER = logging.getLogger(__name__)


class BitbucketStatusSource(Protocol):
    async def current(self, run_id: UUID) -> BitbucketStatusSnapshot | None: ...


class SqlAlchemyBitbucketStatusSource:
    """Detach a validated Run snapshot before any provider request."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    async def current(self, run_id: UUID) -> BitbucketStatusSnapshot | None:
        newer = aliased(Run)
        newer_origin = aliased(BitbucketRunOrigin)
        newer_same_head = (
            select(newer.id)
            .join(newer_origin, newer_origin.run_id == newer.id)
            .where(
                newer.code_change_id == Run.code_change_id,
                newer.head_sha == Run.head_sha,
                newer_origin.run_order > BitbucketRunOrigin.run_order,
            )
            .exists()
        )
        statement = (
            select(
                Run,
                CodeChange,
                Repository,
                ProviderInstallation,
                BitbucketRunOrigin,
                BitbucketConnection,
            )
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Run.origin_provider_installation_id,
            )
            .join(BitbucketRunOrigin, BitbucketRunOrigin.run_id == Run.id)
            .join(
                BitbucketConnection,
                and_(
                    BitbucketConnection.provider_installation_id == ProviderInstallation.id,
                    BitbucketConnection.workspace_id == ProviderInstallation.workspace_id,
                    BitbucketConnection.revoked_at.is_(None),
                ),
            )
            .where(
                Run.id == run_id,
                ProviderInstallation.provider == "bitbucket",
                or_(
                    CodeChange.state == CodeChangeState.OPEN,
                    and_(
                        Run.state == RunState.CANCELLED,
                        CodeChange.state.in_([CodeChangeState.CLOSED, CodeChangeState.MERGED]),
                    ),
                ),
                CodeChange.head_sha == Run.head_sha,
                ~newer_same_head,
                active_origin_condition(),
                active_bitbucket_run_origin(require_current_credential=False),
            )
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).one_or_none()
            coverage_status = None
            if row is not None and row[0].state == RunState.SUCCEEDED:
                run = row[0]
                severities = [
                    severity.value
                    for severity in (
                        await session.scalars(
                            select(Finding.severity).where(
                                Finding.run_id == run.id,
                                Finding.drop_reason.is_(None),
                            )
                        )
                    ).all()
                ]
                result = (
                    await load_review_results(session, {run.id: run.head_sha}, {run.id: severities})
                )[run.id]
                coverage_status = result.coverage.status.value
        if row is None:
            return None
        run, change, _, installation, origin, connection = row
        if run.base_ref is None:
            return None
        return BitbucketStatusSnapshot(
            run.id,
            BoundBitbucketTarget(
                installation.id,
                installation.workspace_id,
                origin.repository_id,
                origin.repository_uuid,
                connection.credential_generation,
                origin.binding_generation,
            ),
            origin.pr_number,
            run.head_sha,
            change.source_branch,
            run.state.value,
            origin.run_order,
            coverage_status,
            run.base_ref,
        )


class BitbucketStatusUnavailable(ValueError):
    def __init__(
        self,
        message: str,
        *,
        retry_after: timedelta | None = None,
        reason: str = "status_unavailable",
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after
        self.reason = reason


class BitbucketStatusForbidden(BitbucketStatusUnavailable):
    pass


class BitbucketStatusLeaseLost(BitbucketStatusUnavailable):
    pass


def _mapping(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise BitbucketStatusUnavailable("Bitbucket status response is malformed")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise BitbucketStatusUnavailable("Bitbucket PR repository identity is malformed")
    try:
        return UUID(value.strip("{}"))
    except ValueError:
        raise BitbucketStatusUnavailable("Bitbucket PR repository identity is malformed") from None


class BitbucketBuildStatusPublisher:
    """POST the same key on each transition; never use a second key after an error."""

    def __init__(
        self,
        source: BitbucketStatusSource,
        credentials: BitbucketRequestAuthority,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport] | None = None,
        ledger: BitbucketStatusIntents | None = None,
        heartbeat_interval_seconds: float = 20.0,
    ) -> None:
        if heartbeat_interval_seconds <= 0:
            raise ValueError("invalid Bitbucket status heartbeat interval")
        self._source = source
        self._credentials = credentials
        self._transports = transport_factory
        self._ledger = ledger
        self._heartbeat_interval_seconds = heartbeat_interval_seconds

    @staticmethod
    async def _request(
        client: httpx.AsyncClient,
        method: str,
        url: str,
        token: str,
        body: dict[str, str] | None = None,
        checkpoint: Callable[[], Awaitable[None]] | None = None,
        missing_ok: bool = False,
        not_found_reason: str = "status_unavailable",
    ) -> object:
        if checkpoint is not None:
            await checkpoint()
        async with client.stream(
            method,
            url,
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            follow_redirects=False,
            timeout=10.0,
        ) as response:
            if missing_ok and response.status_code == 404:
                return None
            if response.status_code == 404:
                raise BitbucketStatusUnavailable(
                    "Bitbucket status resource is missing", reason=not_found_reason
                )
            if response.status_code == 429:
                raise BitbucketStatusUnavailable(
                    "Bitbucket status endpoint is rate limited",
                    retry_after=parse_retry_after(response.headers.get("Retry-After"))
                    or timedelta(hours=1),
                )
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > _MAX_BODY_BYTES:
                    raise BitbucketStatusUnavailable("Bitbucket status response is too large")
                chunks.append(chunk)
            status = response.status_code
        if status in {401, 403} or 300 <= status < 400:
            raise BitbucketStatusForbidden("Bitbucket status author or access denied")
        if status not in ({200} if method == "GET" else {200, 201}):
            raise BitbucketStatusUnavailable("Bitbucket status endpoint unavailable")
        try:
            return json.loads(b"".join(chunks))
        except (ValueError, RecursionError):
            raise BitbucketStatusUnavailable("Bitbucket status response is malformed") from None

    async def upsert(self, run_id: UUID, run_url: str | None) -> None:
        snapshot = await self._source.current(run_id)
        if snapshot is None or run_url is None:
            return
        if self._ledger is None:
            await self._publish_once(snapshot, run_url, self._credentials.checkpoint)
            return
        await self._ledger.register(snapshot, run_url)
        claim = await self._ledger.acquire(status_key(snapshot))
        if claim is None:
            return
        await self._publish_claimed(claim)

    async def replay(self, run_id: UUID, run_url: str) -> None:
        """Retry a durable intent, including a periodic terminal reassertion."""
        if self._ledger is None:
            return
        snapshot = await self._source.current(run_id)
        if snapshot is None:
            await self._ledger.retire(run_id)
            return
        await self._ledger.register(snapshot, run_url)
        claim = await self._ledger.acquire(status_key(snapshot))
        if claim is not None:
            await self._publish_claimed(claim, reassert_terminal=True)

    async def _publish_claimed(
        self, claim: StatusClaim, *, reassert_terminal: bool = False
    ) -> None:
        assert self._ledger is not None
        ledger = self._ledger
        lost = asyncio.Event()

        async def checkpoint(target: BoundBitbucketTarget) -> None:
            if lost.is_set() or not await ledger.renew(claim):
                lost.set()
                raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
            await self._credentials.checkpoint(target)

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(self._heartbeat_interval_seconds)
                try:
                    if not await ledger.renew(claim):
                        lost.set()
                        return
                except Exception:
                    lost.set()
                    _LOGGER.exception(
                        "Bitbucket status lease heartbeat failed for run %s", claim.key
                    )
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            for _ in range(8):
                intent = await self._ledger.intent(claim)
                if intent is None:
                    raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
                snapshot = await self._source.current(intent.run_id)
                if snapshot is None:
                    await ledger.retire(intent.run_id)
                    return
                if snapshot.state != intent.desired_state:
                    await self._ledger.register(snapshot, intent.run_url)
                    continue
                if (
                    intent.applied_run_id == intent.run_id
                    and intent.applied_state == intent.desired_state
                    and not (
                        reassert_terminal
                        and snapshot.state in {"succeeded", "failed", "cancelled", "skipped"}
                    )
                ):
                    return
                if (
                    intent.applied_run_id == intent.run_id
                    and intent.applied_state in {"running", "publishing"}
                    and intent.desired_state == "queued"
                ):
                    # Requeue keeps the already published INPROGRESS payload.
                    if not await ledger.mark_applied(claim, intent):
                        raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
                    newest = await self._source.current(intent.run_id)
                    if newest is not None and newest.state != intent.desired_state:
                        await ledger.register(newest, intent.run_url)
                        continue
                    return
                try:
                    posted = await self._publish_once(
                        snapshot,
                        intent.run_url,
                        checkpoint,
                        verify_remote=(
                            reassert_terminal
                            and intent.applied_run_id == intent.run_id
                            and intent.applied_state == intent.desired_state
                        ),
                    )
                except BitbucketStatusForbidden:
                    await self._ledger.forbid(claim, snapshot.target.credential_generation)
                    raise
                except BitbucketStatusLeaseLost:
                    raise
                except BitbucketStatusUnavailable as error:
                    if error.retry_after is not None:
                        await self._ledger.defer_rate_limit(claim, error.retry_after)
                    else:
                        await self._ledger.defer_failure(claim, error.reason, timedelta(minutes=5))
                    raise
                if not posted:
                    continue
                if not await self._ledger.mark_applied(claim, intent):
                    raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
                newest = await self._source.current(intent.run_id)
                if newest is not None and newest.state != snapshot.state:
                    await self._ledger.register(newest, intent.run_url)
                    continue
                next_intent = await self._ledger.intent(claim)
                if next_intent is None:
                    raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
                if (next_intent.run_id, next_intent.desired_state) == (
                    intent.run_id,
                    intent.desired_state,
                ):
                    return
            raise BitbucketStatusUnavailable("Bitbucket status changed too often")
        finally:
            heartbeat_task.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat_task
            await self._ledger.release(claim)

    async def _publish_once(
        self,
        snapshot: BitbucketStatusSnapshot,
        run_url: str,
        checkpoint: Callable[[BoundBitbucketTarget], Awaitable[None]],
        *,
        verify_remote: bool = False,
    ) -> bool:
        target = snapshot.target
        root = f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D"
        async with httpx.AsyncClient(
            transport=self._transports() if self._transports else None,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            credential = await self._credentials.credential_for(target)
            pr = _mapping(
                await self._request(
                    client,
                    "GET",
                    f"{root}/pullrequests/{snapshot.pr_number}",
                    credential.token,
                    checkpoint=lambda: checkpoint(target),
                    not_found_reason="pr_missing",
                )
            )
            source = _mapping(pr.get("source"))
            destination = _mapping(pr.get("destination"))
            source_repo_uuid = _uuid(_mapping(source.get("repository")).get("uuid"))
            try:
                prefix = pr_commit_hash(_mapping(source.get("commit")).get("hash"))
            except ValueError:
                raise BitbucketStatusUnavailable("Bitbucket PR head is malformed") from None
            if len(prefix) == 12:
                commit = _mapping(
                    await self._request(
                        client,
                        "GET",
                        f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{source_repo_uuid}%7D/commit/{prefix}",
                        credential.token,
                        checkpoint=lambda: checkpoint(target),
                    )
                )
                try:
                    head = resolved_commit_hash(commit.get("hash"), prefix)
                except ValueError:
                    raise BitbucketStatusUnavailable("Bitbucket PR head is malformed") from None
            else:
                head = prefix
            if pr.get("state") != "OPEN" and not (
                snapshot.state == "cancelled" and pr.get("state") in {"DECLINED", "MERGED"}
            ):
                raise BitbucketStatusUnavailable(
                    "Bitbucket PR is no longer open", reason="pr_not_open"
                )
            if (
                pr.get("id") != snapshot.pr_number
                or _uuid(_mapping(destination.get("repository")).get("uuid"))
                != target.repository_uuid
                or head != snapshot.head_sha
                or _mapping(source.get("branch")).get("name") != snapshot.source_branch
                or (
                    snapshot.target_branch is not None
                    and _mapping(destination.get("branch")).get("name") != snapshot.target_branch
                )
            ):
                raise BitbucketStatusUnavailable("Bitbucket PR head or identity changed")
            payload = bitbucket_status_payload(
                workspace_id=target.workspace_id,
                repository_uuid=target.repository_uuid,
                pr_number=snapshot.pr_number,
                head_sha=snapshot.head_sha,
                source_branch=snapshot.source_branch,
                run_url=run_url,
                state=snapshot.state,
                coverage_status=snapshot.coverage_status,
            )
            if await self._source.current(snapshot.run_id) != snapshot:
                return False
            await checkpoint(target)
            fresh = await self._credentials.credential_for(target)
            await checkpoint(target)
            status_root = (
                f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{source_repo_uuid}%7D/"
                f"commit/{snapshot.head_sha}/statuses/build"
            )
            if verify_remote:
                remote = await self._request(
                    client,
                    "GET",
                    f"{status_root}/{payload['key']}",
                    fresh.token,
                    checkpoint=lambda: checkpoint(target),
                    missing_ok=True,
                )
                if remote is not None:
                    status = _mapping(remote)
                    if all(
                        status.get(field) == payload[field]
                        for field in ("key", "state", "url", "refname", "name", "description")
                    ):
                        return True
            await self._request(
                client,
                "POST",
                status_root,
                fresh.token,
                payload,
                checkpoint=lambda: checkpoint(target),
            )
            return True


class StatusReplayLedger(Protocol):
    async def retire_expired(self, *, limit: int = 1000) -> int: ...

    async def pending(self, *, limit: int = 100) -> list[tuple[UUID, str]]: ...


class StatusReplayPublisher(Protocol):
    async def replay(self, run_id: UUID, run_url: str) -> None: ...


class BitbucketStatusReconciler:
    """Replay persisted intents from the worker leader loop."""

    def __init__(
        self,
        ledger: StatusReplayLedger,
        publisher: StatusReplayPublisher,
        *,
        deadline_seconds: float = 20.0,
    ) -> None:
        if deadline_seconds <= 0:
            raise ValueError("invalid Bitbucket status reconciliation deadline")
        self._ledger = ledger
        self._publisher = publisher
        self._deadline_seconds = deadline_seconds

    async def tick(self) -> None:
        try:
            async with asyncio.timeout(self._deadline_seconds):
                await self._ledger.retire_expired()
                for run_id, run_url in await self._ledger.pending():
                    try:
                        await self._publisher.replay(run_id, run_url)
                    except Exception:
                        _LOGGER.exception(
                            "Bitbucket status reconciliation failed for run %s", run_id
                        )
        except TimeoutError:
            _LOGGER.warning("Bitbucket status reconciliation tick exceeded its deadline")
