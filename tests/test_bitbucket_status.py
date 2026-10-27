"""Bitbucket build status identity and transition contract."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
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


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("queued", "INPROGRESS"),
        ("running", "INPROGRESS"),
        ("publishing", "INPROGRESS"),
        ("succeeded", "SUCCESSFUL"),
        ("failed", "FAILED"),
        ("cancelled", "FAILED"),
        ("skipped", "FAILED"),
    ],
)
def test_status_transitions_reuse_key_for_pr_head(state: str, expected: str) -> None:
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
        snapshot = BitbucketStatusSnapshot(run_id, target, 7, HEAD, "feature/review", "running")
        statuses: list[dict[str, str]] = []
        requests: list[httpx.Request] = []
        ambiguous = True
        source_uuid = remote
        remote_head = HEAD
        remote_state = "OPEN"
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
                return BitbucketRequestCredential(target, "test-token")

        async def respond(request: httpx.Request) -> httpx.Response:
            nonlocal ambiguous
            requests.append(request)
            assert request.url.host == "api.bitbucket.org"
            assert request.headers["Authorization"] == "Bearer test-token"
            if request.method == "GET":
                if get_status != 200:
                    return httpx.Response(get_status, headers={"Location": "https://evil.example"})
                return httpx.Response(
                    200,
                    json={
                        "id": 7,
                        "state": remote_state,
                        "source": {
                            "commit": {"hash": remote_head},
                            "branch": {"name": "feature/review"},
                            "repository": {"uuid": f"{{{source_uuid}}}"},
                        },
                        "destination": {"repository": {"uuid": f"{{{remote}}}"}},
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
        with pytest.raises(httpx.RequestError):
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
        assert statuses[-1]["key"] == statuses[0]["key"]

        remote_head = "b" * 40
        snapshot = replace(snapshot, state="succeeded")
        with pytest.raises(BitbucketStatusUnavailable, match="head or identity changed"):
            await publisher.upsert(run_id, RUN_URL)
        assert len(statuses) == 3

        remote_head = HEAD
        post_status = 403
        with pytest.raises(BitbucketStatusForbidden):
            await publisher.upsert(run_id, RUN_URL)
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
        assert statuses[-1]["state"] == "FAILED"
        assert statuses[-1]["key"] == statuses[0]["key"]

    asyncio.run(scenario())
