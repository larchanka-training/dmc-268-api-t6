"""Installed Bitbucket publication keeps credentials off proxy settings and fences lost claims."""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Self, cast
from uuid import UUID

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.credential_cipher import (
    BitbucketCredentialCipher,
)
from app.modules.reviews.application.bitbucket_publication_claims import PublicationClaim
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.publish_run_review import (
    ObsoletePublication,
    PublicationIdentity,
    PublishContext,
    PublishError,
    ReviewSubmission,
)
from app.modules.reviews.application.review_output import PublishedFinding
from app.modules.reviews.application.vcs_diff import PullRequestLocator
from app.modules.reviews.infrastructure.bitbucket_publication_resources import (
    BitbucketInstalledPublicationResources,
)

WORKSPACE = UUID("00000000-0000-4000-8000-000000000001")
INSTALLATION = UUID("00000000-0000-4000-8000-000000000002")
REPOSITORY = UUID("00000000-0000-4000-8000-000000000003")
REMOTE_REPOSITORY = UUID("00000000-0000-4000-8000-000000000004")
CHANGE = UUID("00000000-0000-4000-8000-000000000005")
RUN = UUID("00000000-0000-4000-8000-000000000006")
HEAD = "a" * 40
BASE = "b" * 40


class _Session:
    def __init__(self, origin: SimpleNamespace) -> None:
        self.origin = origin

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def scalar(self, _statement: object) -> SimpleNamespace:
        return self.origin


class _Reader:
    async def checkpoint(self, _target: object) -> None:
        return None


class _Claims:
    def __init__(self, *, fail_heartbeat: bool = False) -> None:
        self.claim = PublicationClaim(RUN, UUID(int=7), 1)
        self.fail_heartbeat = fail_heartbeat
        self.renewals = 0
        self.heartbeat_failed = asyncio.Event()
        self.released = False

    async def acquire(self, _run_id: UUID) -> PublicationClaim:
        return self.claim

    async def renew(self, _claim: PublicationClaim) -> None:
        self.renewals += 1
        if self.fail_heartbeat and self.renewals == 2:
            self.heartbeat_failed.set()
            raise RuntimeError("private-test-token")

    async def release(self, _claim: PublicationClaim) -> None:
        self.released = True


def _context() -> PublishContext:
    locator = PullRequestLocator(
        37,
        "team/repo",
        7,
        provider="bitbucket",
        origin_provider_installation_id=INSTALLATION,
        repository_external_id=42,
        repository_uuid=REMOTE_REPOSITORY,
    )
    return PublishContext(
        RUN,
        "publishing",
        False,
        HEAD,
        HEAD,
        True,
        True,
        37,
        "team/repo",
        7,
        "Review summary",
        (),
        "f" * 64,
        "bitbucket",
        42,
        WORKSPACE,
        RunVcsInput(CHANGE, REPOSITORY, HEAD, BASE, locator, base_ref="main"),
        PublicationIdentity(INSTALLATION, WORKSPACE, 37, "bitbucket", REPOSITORY, CHANGE, BASE),
    )


def _resources(
    monkeypatch: pytest.MonkeyPatch, claims: _Claims, *, heartbeat_interval_seconds: float = 0.01
) -> BitbucketInstalledPublicationResources:
    origin = SimpleNamespace(
        workspace_id=WORKSPACE,
        provider_installation_id=INSTALLATION,
        repository_id=REPOSITORY,
        repository_uuid=REMOTE_REPOSITORY,
        pr_number=7,
        head_sha=HEAD,
        credential_generation=1,
        binding_generation=1,
    )
    sessions = cast("async_sessionmaker[AsyncSession]", lambda: _Session(origin))
    resources = BitbucketInstalledPublicationResources(
        session_factory=sessions,
        cipher=BitbucketCredentialCipher(active_key_id="test", keys={"test": b"k" * 32}),
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )
    monkeypatch.setattr(resources, "_reader", _Reader())
    monkeypatch.setattr(resources, "_claims", claims)
    return resources


async def _checkpoint() -> None:
    return None


@pytest.mark.parametrize("finding_count", [0, 1, 8])
def test_publication_request_and_claim_cost_is_bounded_by_findings(
    monkeypatch: pytest.MonkeyPatch, finding_count: int
) -> None:
    claims = _Claims()
    resources = _resources(monkeypatch, claims, heartbeat_interval_seconds=3600)
    requests: list[httpx.Request] = []
    comments: list[dict[str, object]] = []

    class Credentials:
        async def checkpoint(self, _target: BoundBitbucketTarget) -> None:
            return None

        async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(target, "test-token")

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "GET" and request.url.path.endswith("/user"):
            return httpx.Response(200, json={"uuid": f"{{{RUN}}}"})
        if request.method == "GET" and request.url.path.endswith("/comments"):
            return httpx.Response(200, json={"values": list(comments)})
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "id": 7,
                    "state": "OPEN",
                    "source": {
                        "commit": {"hash": HEAD},
                        "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                    },
                    "destination": {
                        "branch": {"name": "main"},
                        "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                    },
                },
            )
        payload = json.loads(request.content)
        if request.method == "PUT":
            comment = comments[-1]
            comment.update(payload)
            return httpx.Response(200, json=comment)
        assert request.method == "POST"
        comment = {"id": len(comments) + 1, "user": {"uuid": f"{{{RUN}}}"}, **payload}
        comments.append(comment)
        return httpx.Response(201, json=comment)

    monkeypatch.setattr(resources, "_credentials", Credentials())
    monkeypatch.setattr(resources, "_transports", lambda: httpx.MockTransport(respond))
    findings = tuple(
        PublishedFinding(
            path="src/example.py",
            line=10 + index,
            start_line=None,
            severity="high",
            category="correctness",
            title="Fix guard",
            body="The guard is inverted.",
            suggestion=None,
            confidence=0.9,
            rule_name=None,
            finding_id=UUID(int=100 + index),
        )
        for index in range(finding_count)
    )

    async def scenario() -> None:
        async with resources.open(_context(), checkpoint=_checkpoint) as gateway:
            result = await gateway.submit_review(
                ReviewSubmission(37, "team/repo", 7, HEAD, "COMMENT", "Summary", findings, "f" * 64)
            )
            assert result.review_id == finding_count + 1

    asyncio.run(scenario())
    assert [request.method for request in requests].count("GET") == finding_count + 4
    assert (
        sum(
            request.method == "GET" and request.url.path.endswith("/comments")
            for request in requests
        )
        == 1
    )
    assert [request.method for request in requests].count("POST") == finding_count + 1
    assert [request.method for request in requests].count("PUT") == 0
    assert claims.renewals <= finding_count + 2
    assert claims.released

    first_attempt_requests = len(requests)
    first_attempt_renewals = claims.renewals

    async def update_summary() -> None:
        async with resources.open(_context(), checkpoint=_checkpoint) as gateway:
            result = await gateway.submit_review(
                ReviewSubmission(
                    37, "team/repo", 7, HEAD, "COMMENT", "Updated summary", findings, "e" * 64
                )
            )
            assert result.review_id == finding_count + 1

    asyncio.run(update_summary())
    updated_requests = requests[first_attempt_requests:]
    assert [request.method for request in updated_requests].count("GET") == 4
    assert (
        sum(
            request.method == "GET" and request.url.path.endswith("/comments")
            for request in updated_requests
        )
        == 1
    )
    assert [request.method for request in updated_requests].count("POST") == 0
    assert [request.method for request in updated_requests].count("PUT") == 1
    assert claims.renewals - first_attempt_renewals <= 2


def test_lost_claim_after_comment_listing_blocks_first_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = _Claims()
    resources = _resources(monkeypatch, claims, heartbeat_interval_seconds=3600)
    writes: list[httpx.Request] = []

    class Credentials:
        async def checkpoint(self, _target: BoundBitbucketTarget) -> None:
            return None

        async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(target, "test-token")

    original_renew = claims.renew
    claim_lost = False

    async def renew(claim: PublicationClaim) -> None:
        if claim_lost:
            raise ObsoletePublication
        await original_renew(claim)

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal claim_lost
        if request.method != "GET":
            writes.append(request)
            raise AssertionError("lost claim reached provider write")
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"uuid": f"{{{RUN}}}"})
        if request.url.path.endswith("/comments"):
            claim_lost = True
            return httpx.Response(200, json={"values": []})
        return httpx.Response(
            200,
            json={
                "id": 7,
                "state": "OPEN",
                "source": {
                    "commit": {"hash": HEAD},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
                "destination": {
                    "branch": {"name": "main"},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
            },
        )

    monkeypatch.setattr(resources, "_credentials", Credentials())
    monkeypatch.setattr(resources, "_transports", lambda: httpx.MockTransport(respond))
    monkeypatch.setattr(claims, "renew", renew)

    async def scenario() -> None:
        async with resources.open(_context(), checkpoint=_checkpoint) as gateway:
            with pytest.raises(ObsoletePublication):
                await gateway.submit_review(
                    ReviewSubmission(37, "team/repo", 7, HEAD, "COMMENT", "Summary", (), "f" * 64)
                )

    asyncio.run(scenario())
    assert writes == []
    assert claims.released


def test_heartbeat_loss_during_final_credential_check_blocks_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = _Claims()
    resources = _resources(monkeypatch, claims)
    writes: list[httpx.Request] = []

    class Credentials:
        def __init__(self) -> None:
            self.block_final = False
            self.final_entered = asyncio.Event()
            self.release = asyncio.Event()

        async def checkpoint(self, _target: BoundBitbucketTarget) -> None:
            if self.block_final:
                self.final_entered.set()
                await self.release.wait()

        async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(target, "test-token")

    credentials = Credentials()
    publication_task: asyncio.Task[object] | None = None
    original_renew = claims.renew

    async def renew(claim: PublicationClaim) -> None:
        await original_renew(claim)
        if asyncio.current_task() is publication_task:
            credentials.block_final = True
        elif credentials.final_entered.is_set():
            claims.heartbeat_failed.set()
            raise RuntimeError("heartbeat failed")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            writes.append(request)
            return httpx.Response(201, json={"id": 1})
        if request.url.path.endswith("/user"):
            return httpx.Response(200, json={"uuid": f"{{{RUN}}}"})
        if request.url.path.endswith("/comments"):
            return httpx.Response(200, json={"values": []})
        return httpx.Response(
            200,
            json={
                "id": 7,
                "state": "OPEN",
                "source": {
                    "commit": {"hash": HEAD},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
                "destination": {
                    "branch": {"name": "main"},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
            },
        )

    monkeypatch.setattr(resources, "_credentials", credentials)
    monkeypatch.setattr(resources, "_transports", lambda: httpx.MockTransport(respond))
    monkeypatch.setattr(claims, "renew", renew)

    async def scenario() -> None:
        nonlocal publication_task
        async with resources.open(_context(), checkpoint=_checkpoint) as gateway:
            publication_task = asyncio.create_task(
                gateway.submit_review(
                    ReviewSubmission(37, "team/repo", 7, HEAD, "COMMENT", "Summary", (), "f" * 64)
                )
            )
            try:
                await asyncio.wait_for(credentials.final_entered.wait(), timeout=2)
                await asyncio.wait_for(claims.heartbeat_failed.wait(), timeout=2)
            finally:
                credentials.release.set()
            with pytest.raises(ObsoletePublication):
                await publication_task

    asyncio.run(scenario())
    assert writes == []
    assert claims.released


def test_installed_publication_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.example.test:8080")
    monkeypatch.setenv("NO_PROXY", "")

    def unexpected_proxy_lookup() -> dict[str, str]:
        raise AssertionError("credential client consulted environment proxy settings")

    monkeypatch.setattr("httpx._client.get_environment_proxies", unexpected_proxy_lookup)
    claims = _Claims()
    resources = _resources(monkeypatch, claims)

    async def scenario() -> None:
        async with resources.open(_context(), checkpoint=_checkpoint):
            pass

    asyncio.run(scenario())
    assert claims.released


def test_installed_publication_wires_pinned_target_branch_into_comment_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claims = _Claims()
    resources = _resources(monkeypatch, claims)
    writes: list[httpx.Request] = []

    class Credentials:
        async def checkpoint(self, _target: BoundBitbucketTarget) -> None:
            return None

        async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
            return BitbucketRequestCredential(target, "test-token")

    def respond(request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            writes.append(request)
            raise AssertionError("retargeted Run must not publish")
        return httpx.Response(
            200,
            json={
                "id": 7,
                "state": "OPEN",
                "source": {
                    "commit": {"hash": HEAD},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
                "destination": {
                    "branch": {"name": "release"},
                    "repository": {"uuid": f"{{{REMOTE_REPOSITORY}}}"},
                },
            },
        )

    monkeypatch.setattr(resources, "_credentials", Credentials())
    monkeypatch.setattr(resources, "_transports", lambda: httpx.MockTransport(respond))

    async def scenario() -> None:
        context = _context()
        assert context.vcs_input is not None and context.vcs_input.base_ref == "main"
        async with resources.open(context, checkpoint=_checkpoint) as gateway:
            submission = ReviewSubmission(
                37, "team/repo", 7, HEAD, "COMMENT", "Summary", (), "f" * 64
            )
            with pytest.raises(PublishError, match="destination branch"):
                await gateway.submit_review(submission)

    asyncio.run(scenario())
    assert writes == []
    assert claims.released


def test_publication_heartbeat_logs_renewal_error_without_secret_and_fences_write(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    claims = _Claims(fail_heartbeat=True)
    resources = _resources(monkeypatch, claims)

    async def scenario() -> None:
        async with resources.open(_context(), checkpoint=_checkpoint) as gateway:
            await asyncio.wait_for(claims.heartbeat_failed.wait(), timeout=1)
            await asyncio.sleep(0)
            submission = ReviewSubmission(
                37, "team/repo", 7, HEAD, "COMMENT", "Summary", (), "f" * 64
            )
            with pytest.raises(ObsoletePublication):
                await gateway.submit_review(submission)

    with caplog.at_level(logging.WARNING):
        asyncio.run(scenario())
    assert claims.released
    assert "Bitbucket publication claim renewal failed" in caplog.text
    assert "private-test-token" not in caplog.text
