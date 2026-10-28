"""Status reconciliation heals missed transitions without monopolizing the worker."""

from __future__ import annotations

import asyncio
import json
import logging
from uuid import UUID, uuid4

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.reviews.application.bitbucket_status import (
    BitbucketStatusSnapshot,
    bitbucket_status_payload,
)
from app.modules.reviews.application.bitbucket_status_intents import (
    StatusClaim,
    StatusIntent,
    status_key,
)
from app.modules.reviews.infrastructure.bitbucket_ci import (
    BitbucketBuildStatusPublisher,
    BitbucketStatusLeaseLost,
    BitbucketStatusReconciler,
)


def test_terminal_recheck_posts_only_after_remote_mismatch() -> None:
    run_id, installation, workspace, repository, remote_repo = (uuid4() for _ in range(5))
    target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
    snapshot = BitbucketStatusSnapshot(
        run_id, target, 7, "a" * 40, "feature/review", "succeeded", 1
    )
    run_url = "https://review.example/runs/one"
    payload = bitbucket_status_payload(
        workspace_id=workspace,
        repository_uuid=remote_repo,
        pr_number=7,
        head_sha=snapshot.head_sha,
        source_branch=snapshot.source_branch,
        run_url=run_url,
        state="succeeded",
    )
    remote_status = payload.copy()
    status_missing = False
    requests: list[httpx.Request] = []

    class Source:
        async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
            assert selected == run_id
            return snapshot

    class Credentials:
        async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
            assert selected == target

        async def credential_for(
            self, selected: BoundBitbucketTarget
        ) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(selected, "test-token")

    claim = StatusClaim(status_key(snapshot), uuid4(), 1)

    class Ledger:
        applied = 0

        async def register(self, current: BitbucketStatusSnapshot, url: str) -> None:
            assert current == snapshot and url == run_url

        async def acquire(self, key: object) -> StatusClaim | None:
            assert key == claim.key
            return claim

        async def renew(self, selected: StatusClaim) -> bool:
            assert selected == claim
            return True

        async def intent(self, selected: StatusClaim) -> StatusIntent | None:
            return StatusIntent(claim.key, run_id, "succeeded", run_url, run_id, "succeeded")

        async def mark_applied(self, selected: StatusClaim, intent: StatusIntent) -> bool:
            self.applied += 1
            return True

        async def release(self, selected: StatusClaim) -> None:
            assert selected == claim

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and "/pullrequests/" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "state": "OPEN",
                    "source": {
                        "commit": {"hash": snapshot.head_sha},
                        "branch": {"name": snapshot.source_branch},
                        "repository": {"uuid": f"{{{remote_repo}}}"},
                    },
                    "destination": {"repository": {"uuid": f"{{{remote_repo}}}"}},
                },
            )
        if request.method == "GET":
            assert request.url.path.endswith(f"/statuses/build/{payload['key']}")
            if status_missing:
                return httpx.Response(404, json={"error": "not found"})
            return httpx.Response(200, json=remote_status)
        assert request.method == "POST"
        assert json.loads(request.content) == payload
        return httpx.Response(201, json=payload)

    async def scenario() -> None:
        nonlocal status_missing
        ledger = Ledger()
        publisher = BitbucketBuildStatusPublisher(
            Source(),
            Credentials(),
            ledger=ledger,  # type: ignore[arg-type]
            transport_factory=lambda: httpx.MockTransport(respond),
        )
        await publisher.replay(run_id, run_url)
        assert [request.method for request in requests] == ["GET", "GET"]
        assert ledger.applied == 1
        remote_status["state"] = "INPROGRESS"
        await publisher.replay(run_id, run_url)
        assert [request.method for request in requests] == ["GET", "GET", "GET", "GET", "POST"]
        status_missing = True
        await publisher.replay(run_id, run_url)
        assert [request.method for request in requests][-3:] == ["GET", "GET", "POST"]

    asyncio.run(scenario())


def test_status_heartbeat_failure_logs_and_does_not_defer_a_lost_lease(
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_id, installation, workspace, repository, remote_repo = (uuid4() for _ in range(5))
    target = BoundBitbucketTarget(installation, workspace, repository, remote_repo, 1, 1)
    snapshot = BitbucketStatusSnapshot(run_id, target, 7, "a" * 40, "feature/review", "running", 1)
    claim = StatusClaim(status_key(snapshot), uuid4(), 1)
    heartbeat_failed = asyncio.Event()

    class Source:
        async def current(self, selected: UUID) -> BitbucketStatusSnapshot | None:
            assert selected == run_id
            return snapshot

    class Credentials:
        async def checkpoint(self, selected: BoundBitbucketTarget) -> None:
            assert selected == target

        async def credential_for(
            self, selected: BoundBitbucketTarget
        ) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(selected, "test-token")

    class Ledger:
        renewals = 0
        deferred = 0

        async def register(self, current: BitbucketStatusSnapshot, url: str) -> None:
            assert current == snapshot

        async def acquire(self, key: object) -> StatusClaim | None:
            assert key == claim.key
            return claim

        async def intent(self, selected: StatusClaim) -> StatusIntent | None:
            return StatusIntent(
                claim.key, run_id, "running", "https://review.example/run", None, None
            )

        async def renew(self, selected: StatusClaim) -> bool:
            self.renewals += 1
            if self.renewals == 1:
                return True
            heartbeat_failed.set()
            raise RuntimeError("heartbeat database unavailable")

        async def defer_failure(self, selected: StatusClaim, reason: str, delay: object) -> None:
            self.deferred += 1

        async def release(self, selected: StatusClaim) -> None:
            assert selected == claim

    async def respond(request: httpx.Request) -> httpx.Response:
        await heartbeat_failed.wait()
        assert request.method == "GET"
        return httpx.Response(
            200,
            json={
                "id": 7,
                "state": "OPEN",
                "source": {
                    "commit": {"hash": snapshot.head_sha},
                    "branch": {"name": snapshot.source_branch},
                    "repository": {"uuid": f"{{{remote_repo}}}"},
                },
                "destination": {"repository": {"uuid": f"{{{remote_repo}}}"}},
            },
        )

    async def scenario() -> None:
        ledger = Ledger()
        publisher = BitbucketBuildStatusPublisher(
            Source(),
            Credentials(),
            ledger=ledger,  # type: ignore[arg-type]
            transport_factory=lambda: httpx.MockTransport(respond),
            heartbeat_interval_seconds=0.001,
        )
        with pytest.raises(BitbucketStatusLeaseLost):
            await publisher.upsert(run_id, "https://review.example/run")
        assert ledger.deferred == 0
        assert ledger.renewals >= 2

    with caplog.at_level(logging.ERROR):
        asyncio.run(scenario())
    assert any(
        "Bitbucket status lease heartbeat failed" in record.message for record in caplog.records
    )


def test_reconciler_has_a_total_deadline() -> None:
    first = (uuid4(), "https://review.example/slow")
    second = (uuid4(), "https://review.example/next")

    class Ledger:
        async def retire_expired(self, *, limit: int = 1000) -> int:
            return 0

        async def pending(self, *, limit: int = 100) -> list[tuple[UUID, str]]:
            return [first, second]

    class Publisher:
        called: list[UUID] = []

        async def replay(self, run_id: UUID, run_url: str) -> None:
            self.called.append(run_id)
            await asyncio.sleep(1)

    async def scenario() -> None:
        publisher = Publisher()
        reconciler = BitbucketStatusReconciler(
            Ledger(),
            publisher,
            deadline_seconds=0.02,
        )
        await asyncio.wait_for(reconciler.tick(), timeout=0.2)
        assert publisher.called == [first[0]]

    asyncio.run(scenario())
