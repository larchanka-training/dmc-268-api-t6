"""Bitbucket build status identity and transition contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from unittest.mock import patch
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
    update_bitbucket_status,
)
from app.modules.reviews.infrastructure.bitbucket_ci import (
    BitbucketBuildStatusPublisher,
    BitbucketStatusForbidden,
    BitbucketStatusUnavailable,
)

HEAD = "a" * 40
RUN_URL = "https://review.example/runs/one"


def test_status_key_has_the_bitbucket_cloud_length_and_stable_literal() -> None:
    payload = bitbucket_status_payload(
        workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
        repository_uuid=UUID("22222222-2222-4222-8222-222222222222"),
        pr_number=7,
        head_sha=HEAD,
        source_branch="feature/review",
        run_url=RUN_URL,
        state="running",
    )
    assert payload["key"] == "AI-REVIEW-356f9b3b40276a07ce72380b745f6c"
    assert len(payload["key"]) == 40


@pytest.mark.parametrize(
    ("state", "expected", "description"),
    [
        ("queued", "INPROGRESS", "AI review running"),
        ("running", "INPROGRESS", "AI review running"),
        ("publishing", "INPROGRESS", "AI review running"),
        ("succeeded", "SUCCESSFUL", "AI review complete"),
        ("failed", "STOPPED", "AI review did not complete"),
        ("cancelled", "STOPPED", "AI review cancelled"),
        ("skipped", "STOPPED", "AI review skipped"),
    ],
)
def test_status_transitions_reuse_key_for_pr_head(
    state: str, expected: str, description: str
) -> None:
    workspace_id = uuid4()
    repository_uuid = uuid4()
    payload = bitbucket_status_payload(
        workspace_id=workspace_id,
        repository_uuid=repository_uuid,
        pr_number=7,
        head_sha=HEAD,
        source_branch="feature/review",
        run_url=RUN_URL,
        state=state,
    )

    assert payload["state"] == expected
    assert payload["description"] == description
    assert payload["name"] == "AI Code Reviewer"
    assert payload["refname"] == "feature/review"
    assert payload["url"] == RUN_URL
    assert (
        payload["key"]
        == bitbucket_status_payload(
            workspace_id=workspace_id,
            repository_uuid=repository_uuid,
            pr_number=7,
            head_sha=HEAD,
            source_branch="feature/review",
            run_url="https://review.example/runs/two",
            state="running",
        )["key"]
    )
    assert (
        payload["key"]
        != bitbucket_status_payload(
            workspace_id=workspace_id,
            repository_uuid=repository_uuid,
            pr_number=8,
            head_sha=HEAD,
            source_branch="feature/review",
            run_url=RUN_URL,
            state=state,
        )["key"]
    )
    assert (
        payload["key"]
        != bitbucket_status_payload(
            workspace_id=uuid4(),
            repository_uuid=repository_uuid,
            pr_number=7,
            head_sha=HEAD,
            source_branch="feature/review",
            run_url=RUN_URL,
            state=state,
        )["key"]
    )


@pytest.mark.parametrize(
    ("coverage_status", "description"),
    [
        ("none", "AI review: nothing reviewable"),
        ("partial", "AI review incomplete"),
    ],
)
def test_succeeded_status_describes_coverage(coverage_status: str, description: str) -> None:
    payload = bitbucket_status_payload(
        workspace_id=UUID("11111111-1111-4111-8111-111111111111"),
        repository_uuid=UUID("22222222-2222-4222-8222-222222222222"),
        pr_number=7,
        head_sha=HEAD,
        source_branch="feature/review",
        run_url=RUN_URL,
        state="succeeded",
        coverage_status=coverage_status,
    )
    assert payload["state"] == "SUCCESSFUL"
    assert payload["description"] == description


def test_invalid_status_identity_is_rejected() -> None:
    with pytest.raises(ValueError):
        bitbucket_status_payload(
            workspace_id=uuid4(),
            repository_uuid=uuid4(),
            pr_number=7,
            head_sha="not-a-sha",
            source_branch="feature/review",
            run_url=RUN_URL,
            state="succeeded",
        )


def test_status_429_exposes_retry_after_and_ignores_host_proxy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime, timedelta
    from email.utils import format_datetime

    retry_date = format_datetime(datetime.now(UTC) + timedelta(minutes=3), usegmt=True)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:9")

    async def scenario() -> None:
        run_id, installation, workspace, repository, remote = (uuid4() for _ in range(5))
        target = BoundBitbucketTarget(installation, workspace, repository, remote, 1, 1)
        snapshot = BitbucketStatusSnapshot(
            run_id, target, 7, HEAD, "feature/review", "running", target_branch="main"
        )

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

        publisher = BitbucketBuildStatusPublisher(
            Source(),
            Credentials(),
            transport_factory=lambda: httpx.MockTransport(
                lambda _: httpx.Response(429, headers={"Retry-After": retry_date})
            ),
        )
        with (
            patch.object(httpx, "AsyncClient", wraps=httpx.AsyncClient) as client_factory,
            pytest.raises(BitbucketStatusUnavailable) as error,
        ):
            await publisher.upsert(run_id, RUN_URL)
        assert client_factory.call_args.kwargs["trust_env"] is False
        assert error.value.retry_after is not None
        assert 170 <= error.value.retry_after.total_seconds() <= 180

    asyncio.run(scenario())


def test_status_update_is_best_effort_and_never_changes_the_run() -> None:
    class Gateway:
        calls: list[tuple[UUID, str | None]] = []

        async def upsert(self, run_id: UUID, run_url: str | None) -> None:
            self.calls.append((run_id, run_url))
            raise httpx.ReadTimeout("provider timeout")

    gateway = Gateway()
    run_id = uuid4()
    asyncio.run(update_bitbucket_status(gateway, run_id, RUN_URL))
    assert gateway.calls == [(run_id, RUN_URL)]


def test_status_gateway_updates_one_key_after_ambiguous_write_and_checks_pr_head() -> None:
    async def scenario() -> None:
        run_id, installation, workspace, repository, remote = (uuid4() for _ in range(5))
        target = BoundBitbucketTarget(installation, workspace, repository, remote, 1, 1)
        snapshot = BitbucketStatusSnapshot(
            run_id, target, 7, HEAD, "feature/review", "running", target_branch="main"
        )
        statuses: list[dict[str, str]] = []
        requests: list[httpx.Request] = []
        ambiguous = True
        source_uuid = remote
        remote_head = HEAD
        resolved_head = HEAD
        remote_state = "OPEN"
        remote_target_branch = "main"
        get_status = 200
        post_status = 201

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
                assert selected == target
                return BitbucketRequestCredential(target, "target-repo-token")

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal ambiguous
            requests.append(request)
            assert request.url.host == "api.bitbucket.org"
            assert request.headers["Authorization"] == "Bearer target-repo-token"
            if request.method == "GET" and "/commit/" in request.url.path:
                assert request.url.path.endswith(f"/commit/{remote_head[:12]}")
                return httpx.Response(200, json={"hash": resolved_head})
            if request.method == "GET":
                if get_status != 200:
                    return httpx.Response(get_status, headers={"Location": "https://evil.example"})
                return httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "state": remote_state,
                        "source": {
                            "commit": {"hash": remote_head[:12]},
                            "branch": {"name": "feature/review"},
                            "repository": {"uuid": f"{{{source_uuid}}}"},
                        },
                        "destination": {
                            "branch": {"name": remote_target_branch},
                            "commit": {"hash": "d" * 12},
                            "repository": {"uuid": f"{{{remote}}}"},
                        },
                    },
                )
            payload: dict[str, str] = json.loads(request.content)
            statuses.append(payload)
            if ambiguous:
                ambiguous = False
                raise httpx.ReadTimeout("ambiguous")
            return httpx.Response(post_status, json=payload)

        publisher = BitbucketBuildStatusPublisher(
            Source(), Credentials(), transport_factory=lambda: httpx.MockTransport(respond)
        )
        with pytest.raises(BitbucketStatusUnavailable, match="transport unavailable"):
            await publisher.upsert(run_id, RUN_URL)
        snapshot = replace(snapshot, state="succeeded")
        await publisher.upsert(run_id, RUN_URL)

        assert len(statuses) == 2
        assert statuses[0]["state"] == "INPROGRESS"
        assert statuses[1]["state"] == "SUCCESSFUL"
        assert statuses[0]["key"] == statuses[1]["key"]
        assert statuses[0]["refname"] == "feature/review"
        assert statuses[1]["url"] == RUN_URL
        assert all(not request.url.path.startswith("//") for request in requests)

        # Fork source identity is taken only from the current verified PR.
        source_uuid = uuid4()
        snapshot = replace(snapshot, state="failed")
        post_status = 200  # Existing keys may be returned as an update response.
        await publisher.upsert(run_id, RUN_URL)
        assert f"%7B{source_uuid}%7D" in str(requests[-1].url)
        assert requests[-1].headers["Authorization"] == "Bearer target-repo-token"
        assert statuses[-1]["state"] == "STOPPED"
        assert statuses[-1]["key"] == statuses[0]["key"]

        remote_target_branch = "release"
        snapshot = replace(snapshot, state="cancelled")
        await publisher.upsert(run_id, RUN_URL)
        assert statuses[-1]["state"] == "STOPPED"
        assert statuses[-1]["key"] == statuses[0]["key"]
        snapshot = replace(snapshot, state="succeeded")
        with pytest.raises(BitbucketStatusUnavailable, match="head or identity changed"):
            await publisher.upsert(run_id, RUN_URL)
        assert len(statuses) == 4
        remote_target_branch = "main"

        remote_head = "b" * 40
        resolved_head = remote_head
        snapshot = replace(snapshot, state="succeeded")
        with pytest.raises(BitbucketStatusUnavailable, match="head or identity changed"):
            await publisher.upsert(run_id, RUN_URL)
        assert len(statuses) == 4

        remote_head = HEAD
        resolved_head = "c" * 40
        with pytest.raises(BitbucketStatusUnavailable, match="head is malformed"):
            await publisher.upsert(run_id, RUN_URL)
        assert len(statuses) == 4
        resolved_head = HEAD
        post_status = 403
        with pytest.raises(BitbucketStatusForbidden):
            await publisher.upsert(run_id, RUN_URL)
        assert f"%7B{source_uuid}%7D" in str(requests[-1].url)
        assert requests[-1].headers["Authorization"] == "Bearer target-repo-token"
        assert statuses[-1]["key"] == statuses[0]["key"]

        get_status = 302
        with pytest.raises(BitbucketStatusForbidden):
            await publisher.upsert(run_id, RUN_URL)
        assert all(request.url.host == "api.bitbucket.org" for request in requests)

        # A closed PR still receives the cancelled terminal state on its pinned head.
        get_status = 200
        post_status = 200
        remote_state = "DECLINED"
        snapshot = replace(snapshot, state="cancelled")
        await publisher.upsert(run_id, RUN_URL)
        assert statuses[-1]["state"] == "STOPPED"
        assert statuses[-1]["key"] == statuses[0]["key"]

    asyncio.run(scenario())
