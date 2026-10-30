"""Installed Bitbucket publication keeps credentials off proxy settings and fences lost claims."""

from __future__ import annotations

import asyncio
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
    monkeypatch: pytest.MonkeyPatch, claims: _Claims
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
        heartbeat_interval_seconds=0.01,
    )
    monkeypatch.setattr(resources, "_reader", _Reader())
    monkeypatch.setattr(resources, "_claims", claims)
    return resources


async def _checkpoint() -> None:
    return None


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
