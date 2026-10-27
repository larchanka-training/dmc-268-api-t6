"""Bounded, fenced Bitbucket Cloud build status publication."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from contextlib import suppress
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
from app.modules.integrations.bitbucket.infrastructure.models import BitbucketRunOrigin
from app.modules.repositories.infrastructure.models import ProviderInstallation, Repository
from app.modules.reviews.application.bitbucket_status import (
    BitbucketStatusSnapshot,
    bitbucket_status_payload,
)
from app.modules.reviews.infrastructure.bitbucket_run_origin import active_bitbucket_run_origin
from app.modules.reviews.infrastructure.bitbucket_status_ledger import (
    SqlAlchemyBitbucketStatusLedger,
    StatusClaim,
    status_key,
)
from app.modules.reviews.infrastructure.models import CodeChange, Run
from app.modules.reviews.infrastructure.run_origin import active_origin_condition

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
            select(Run, CodeChange, Repository, ProviderInstallation, BitbucketRunOrigin)
            .join(CodeChange, CodeChange.id == Run.code_change_id)
            .join(Repository, Repository.id == CodeChange.repository_id)
            .join(
                ProviderInstallation,
                ProviderInstallation.id == Run.origin_provider_installation_id,
            )
            .join(BitbucketRunOrigin, BitbucketRunOrigin.run_id == Run.id)
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
                active_bitbucket_run_origin(),
            )
        )
        async with self._sessions() as session:
            row = (await session.execute(statement)).one_or_none()
        if row is None:
            return None
        run, change, _, installation, origin = row
        return BitbucketStatusSnapshot(
            run.id,
            BoundBitbucketTarget(
                installation.id,
                installation.workspace_id,
                origin.repository_id,
                origin.repository_uuid,
                origin.credential_generation,
                origin.binding_generation,
            ),
            origin.pr_number,
            run.head_sha,
            change.source_branch,
            run.state.value,
            origin.run_order,
        )


class BitbucketStatusUnavailable(ValueError):
    pass


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
        ledger: SqlAlchemyBitbucketStatusLedger | None = None,
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
                    return

        heartbeat_task = asyncio.create_task(heartbeat())
        try:
            for _ in range(8):
                intent = await self._ledger.intent(claim)
                if intent is None:
                    raise BitbucketStatusLeaseLost("Bitbucket status lease lost")
                snapshot = await self._source.current(intent.run_id)
                if snapshot is None:
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
                try:
                    posted = await self._publish_once(snapshot, intent.run_url, checkpoint)
                except BitbucketStatusForbidden:
                    await self._ledger.forbid(claim, snapshot.target.credential_generation)
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
    ) -> bool:
        target = snapshot.target
        root = f"{_ORIGIN}/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D"
        async with httpx.AsyncClient(
            transport=self._transports() if self._transports else None
        ) as client:
            credential = await self._credentials.credential_for(target)
            pr = _mapping(
                await self._request(
                    client,
                    "GET",
                    f"{root}/pullrequests/{snapshot.pr_number}",
                    credential.token,
                    checkpoint=lambda: checkpoint(target),
                )
            )
            source = _mapping(pr.get("source"))
            destination = _mapping(pr.get("destination"))
            source_repo_uuid = _uuid(_mapping(source.get("repository")).get("uuid"))
            if (
                pr.get("id") != snapshot.pr_number
                or (
                    pr.get("state") != "OPEN"
                    and not (
                        snapshot.state == "cancelled" and pr.get("state") in {"DECLINED", "MERGED"}
                    )
                )
                or _uuid(_mapping(destination.get("repository")).get("uuid"))
                != target.repository_uuid
                or _mapping(source.get("commit")).get("hash") != snapshot.head_sha
                or _mapping(source.get("branch")).get("name") != snapshot.source_branch
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
            await self._request(
                client,
                "POST",
                status_root,
                fresh.token,
                payload,
                checkpoint=lambda: checkpoint(target),
            )
            return True


class BitbucketStatusReconciler:
    """Replay persisted intents from the worker leader loop."""

    def __init__(
        self,
        ledger: SqlAlchemyBitbucketStatusLedger,
        publisher: BitbucketBuildStatusPublisher,
    ) -> None:
        self._ledger = ledger
        self._publisher = publisher

    async def tick(self) -> None:
        for run_id, run_url in await self._ledger.pending():
            try:
                await self._publisher.replay(run_id, run_url)
            except Exception:
                _LOGGER.exception("Bitbucket status reconciliation failed for run %s", run_id)
