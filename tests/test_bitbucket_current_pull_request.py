"""Current Bitbucket PR reads use UUID identity and a fixed API origin."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.current_pull_request import (
    BitbucketCurrentPullRequestProvider,
)
from app.modules.integrations.bitbucket.infrastructure.repository_provider import (
    BitbucketProviderError,
)

TARGET = BoundBitbucketTarget(UUID(int=1), UUID(int=2), UUID(int=3), UUID(int=4), 5, 6)


@dataclass
class Credentials:
    calls: int = 0

    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        assert target == TARGET
        self.calls += 1
        return BitbucketRequestCredential(target, "test-token")


def test_reads_current_pr_after_uuid_lookup_and_rejects_redirects() -> None:
    async def scenario() -> None:
        paths: list[str] = []

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.bitbucket.org"
            assert request.headers["Authorization"] == "Bearer test-token"
            paths.append(str(request.url))
            if len(paths) == 1:
                return httpx.Response(
                    200,
                    json={
                        "uuid": "{" + str(TARGET.repository_uuid) + "}",
                        "workspace": {"uuid": "{" + str(UUID(int=8)) + "}", "slug": "team"},
                        "slug": "renamed",
                        "full_name": "team/renamed",
                        "name": "Renamed",
                        "is_private": True,
                        "mainbranch": {"name": "main"},
                        "links": {"html": {"href": "https://bitbucket.org/team/renamed"}},
                    },
                )
            return httpx.Response(
                200,
                json={
                    "id": 17,
                    "state": "OPEN",
                    "title": "Fix",
                    "description": "Description",
                    "updated_on": "2026-10-26T10:00:00+00:00",
                    "author": {"nickname": "reviewer"},
                    "source": {"branch": {"name": "feature"}, "commit": {"hash": "a" * 40}},
                    "destination": {
                        "branch": {"name": "main"},
                        "commit": {"hash": "b" * 40},
                        "repository": {"uuid": "{" + str(TARGET.repository_uuid) + "}"},
                    },
                },
            )

        credentials = Credentials()
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            provider = BitbucketCurrentPullRequestProvider(client, credentials)
            current = await provider.get_current(TARGET, 17)
            assert (current.number, current.state, current.head_sha, current.base_sha) == (
                17,
                "open",
                "a" * 40,
                "b" * 40,
            )
            assert current.web_url == "https://bitbucket.org/team/renamed/pull-requests/17"
            assert credentials.calls == 1
            assert len(paths) == 2
            assert (
                paths[1]
                == "https://api.bitbucket.org/2.0/repositories/team/renamed/pullrequests/17"
            )

        def redirect(_: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://evil.example/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            with pytest.raises(BitbucketProviderError):
                await BitbucketCurrentPullRequestProvider(client, credentials).get_current(
                    TARGET, 17
                )

    asyncio.run(scenario())
