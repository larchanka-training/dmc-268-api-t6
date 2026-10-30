"""Independent provider fixtures for Bitbucket's identity and diff guards."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.infrastructure.run_http import (
    BitbucketRunHttp,
    BitbucketRunHttpError,
)
from app.modules.reviews.application.commit_prefix_limits import COMMIT_PAGE_SIZE
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider
from tests.test_bitbucket_vcs import (
    BASE,
    HEAD,
    PREFIX,
    TARGET,
    Credentials,
    _locator,
    _pr,
    _stat,
)


async def checkpoint() -> None:
    return None


def _provider(client: httpx.AsyncClient) -> HttpBitbucketVcsProvider:
    return HttpBitbucketVcsProvider(
        http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
    )


@pytest.mark.parametrize(
    "origin",
    ["https://other.example", "http://api.bitbucket.org"],
    ids=["foreign-host", "insecure-scheme"],
)
def test_diffstat_next_rejects_only_changed_origin(origin: str) -> None:
    next_url = f"{origin}{PREFIX}/diffstat/{HEAD}..{BASE}?topic=true&pagelen=100&page=2"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat(next_url=next_url))
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(RunFailure, match="diffstat is incomplete"):
                await _provider(client).get_pull_request(_locator(), checkpoint=checkpoint)

    asyncio.run(scenario())
    assert all(request.url.host == "api.bitbucket.org" for request in requests)
    assert len(requests) == 2


@pytest.mark.parametrize(
    "origin",
    ["https://other.example", "http://api.bitbucket.org"],
    ids=["foreign-host", "insecure-scheme"],
)
def test_commit_page_next_rejects_only_changed_origin(origin: str) -> None:
    next_url = f"{origin}{PREFIX}/pullrequests/7/commits?pagelen={COMMIT_PAGE_SIZE}&page=2"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/pullrequests/7/commits":
            return httpx.Response(
                200,
                json={
                    "values": [{"hash": HEAD, "message": "Fix parser"}],
                    "next": next_url,
                },
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(RunFailure, match="commit messages are unavailable"):
                await _provider(client).get_commit_messages(_locator(), checkpoint=checkpoint)

    asyncio.run(scenario())
    assert all(
        request.url.scheme == "https" and request.url.host == "api.bitbucket.org"
        for request in requests
    )
    assert len([request for request in requests if request.url.path.endswith("/commits")]) == 1


@pytest.mark.parametrize(
    "origin",
    ["https://other.example", "http://api.bitbucket.org"],
    ids=["foreign-host", "insecure-scheme"],
)
def test_tree_page_next_rejects_only_changed_origin(origin: str) -> None:
    next_url = f"{origin}{PREFIX}/src/{BASE}/?pagelen=100&page=2"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "a.py",
                            "size": 4,
                            "commit": {"hash": BASE},
                        }
                    ],
                    "next": next_url,
                },
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            input = RunVcsInput(UUID(int=5), TARGET.repository_id, HEAD, BASE, _locator())
            tree = BitbucketRepositoryTree(
                http=http,
                expected_input=input,
                vcs=HttpBitbucketVcsProvider(http=http, locator=input.locator),
                checkpoint=checkpoint,
            )
            with pytest.raises(BitbucketRunHttpError) as error:
                await tree.directory(BASE)
            assert error.value.kind == "invalid_response"

    asyncio.run(scenario())
    assert all(request.url.host == "api.bitbucket.org" for request in requests)
    assert len([request for request in requests if request.url.path.endswith(f"/src/{BASE}/")]) == 1


@pytest.mark.parametrize(
    "patch",
    [
        (
            "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
            "@@ -1 +1,2 @@\n-old\n+new\n+extra\n"
        ),
        (
            "diff --git a/src/a.py b/src/a.py\n"
            "diff --git a/src/b.py b/src/b.py\n"
            "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
        ),
    ],
    ids=["diffstat-line-count", "second-git-header"],
)
def test_malformed_single_file_patch_is_omitted(patch: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(200, text=patch)
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = _provider(client)
            pr = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            files = await vcs.get_diff(pr, checkpoint=checkpoint)
            assert len(files) == 1
            assert files[0].patch is None
            assert files[0].omission_reason == "missing_patch"

    asyncio.run(scenario())


def test_destination_uuid_mismatch_cancels_before_diffstat() -> None:
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == f"{PREFIX}/pullrequests/7"
        data = _pr()
        destination = data["destination"]
        assert isinstance(destination, dict)
        destination["repository"] = {"uuid": "{" + str(UUID(int=999)) + "}"}
        return httpx.Response(200, json=data)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(RunCancelled, match="installation_unavailable"):
                await _provider(client).get_pull_request(_locator(), checkpoint=checkpoint)

    asyncio.run(scenario())
    assert len(requests) == 1


def test_blob_stage_rechecks_head_after_the_bytes_arrive() -> None:
    head = HEAD
    blob_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal head, blob_reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=head))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if request.url.path == f"{PREFIX}/src/{HEAD}/src/a.py":
            blob_reads += 1
            head = "d" * 40
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = _provider(client)
            pr = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            files = await vcs.get_diff(pr, checkpoint=checkpoint)
            assert files[0].blob_sha is not None
            assert (
                await vcs.get_blob(_locator(), files[0].blob_sha, checkpoint=checkpoint) == b"new\n"
            )
            with pytest.raises(RunCancelled, match="superseded"):
                await vcs.verify_current(checkpoint=checkpoint)

    asyncio.run(scenario())
    assert blob_reads == 1
