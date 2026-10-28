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

TARGET = BoundBitbucketTarget(
    UUID(int=1),
    UUID(int=2),
    UUID(int=3),
    UUID(int=4),
    5,
    6,
    bitbucket_workspace_uuid=UUID(int=8),
)


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
        resolved_head = ["a" * 40]

        def respond(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "api.bitbucket.org"
            assert request.headers["Authorization"] == "Bearer test-token"
            paths.append(str(request.url))
            if "/%7B%7D/" in str(request.url):
                return httpx.Response(404)
            if "/pullrequests/" not in request.url.path and "/commit/" not in request.url.path:
                assert request.url.path.startswith(
                    f"/2.0/repositories/{{{TARGET.bitbucket_workspace_uuid}}}/"
                    f"{{{TARGET.repository_uuid}}}"
                )
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
            if request.url.path.endswith("/commit/aaaaaaaaaaaa"):
                return httpx.Response(200, json={"hash": resolved_head[0]})
            if request.url.path.endswith("/commit/bbbbbbbbbbbb"):
                return httpx.Response(200, json={"hash": "b" * 40})
            return httpx.Response(
                200,
                json={
                    "id": 17,
                    "state": "OPEN",
                    "title": "Fix",
                    "description": "Description",
                    "updated_on": "2026-10-26T10:00:00+00:00",
                    "author": {"nickname": "reviewer"},
                    "source": {
                        "branch": {"name": "feature"},
                        "commit": {"hash": "a" * 12},
                        "repository": {"uuid": "{" + str(TARGET.repository_uuid) + "}"},
                    },
                    "destination": {
                        "branch": {"name": "main"},
                        "commit": {"hash": "b" * 12},
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
            assert len(paths) == 4
            assert not any("/%7B%7D/" in path for path in paths)
            assert paths[0] == (
                "https://api.bitbucket.org/2.0/repositories/"
                "%7B00000000-0000-0000-0000-000000000008%7D/"
                "%7B00000000-0000-0000-0000-000000000004%7D"
            )
            assert (
                paths[1]
                == "https://api.bitbucket.org/2.0/repositories/team/renamed/pullrequests/17"
            )
            assert paths[2:] == [
                "https://api.bitbucket.org/2.0/repositories/"
                "%7B00000000-0000-0000-0000-000000000008%7D/"
                "%7B00000000-0000-0000-0000-000000000004%7D/commit/aaaaaaaaaaaa",
                "https://api.bitbucket.org/2.0/repositories/"
                "%7B00000000-0000-0000-0000-000000000008%7D/"
                "%7B00000000-0000-0000-0000-000000000004%7D/commit/bbbbbbbbbbbb",
            ]
            resolved_head[0] = "c" * 40
            with pytest.raises(BitbucketProviderError, match="invalid_provider_response"):
                await provider.get_current(TARGET, 17)

        def redirect(_: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"Location": "https://evil.example/"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(redirect)) as client:
            with pytest.raises(BitbucketProviderError):
                await BitbucketCurrentPullRequestProvider(client, credentials).get_current(
                    TARGET, 17
                )

    asyncio.run(scenario())
