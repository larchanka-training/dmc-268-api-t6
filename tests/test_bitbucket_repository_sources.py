"""Pinned Bitbucket policy, conventions and context source reads."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttp
from app.modules.reviews.application.policy_documents import LoadPolicyDocuments
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RunFailure
from app.modules.reviews.infrastructure.bitbucket_policy_source import BitbucketPolicyDocumentSource
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_revision_source import BitbucketRevisionSource
from app.modules.reviews.infrastructure.bitbucket_run_source import BitbucketRunSource
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider
from tests.test_bitbucket_vcs import BASE, DEST, HEAD, PREFIX, TARGET, Credentials, _locator, _pr

RUN_ID = UUID(int=44)
INPUT = RunVcsInput(UUID(int=45), TARGET.repository_id, HEAD, BASE, _locator())


async def checkpoint() -> None:
    return None


def test_pinned_policy_conventions_and_context_source_use_uuid_routes() -> None:
    seen: list[httpx.Request] = []
    files = {
        "AGENTS.md": b"Use uv.\n",
        ".review/rules.md": b"No secrets.\n",
        "src/app.py": b"new\n",
    }

    def entry(path: str) -> dict[str, object]:
        return {
            "type": "commit_file",
            "path": path,
            "size": len(files[path]),
            "commit": {"hash": BASE},
            "attributes": [],
        }

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer secret-token"
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "page": 1, "pagelen": 100, "values": []})
        if path == f"{PREFIX}/pullrequests/7/commits":
            return httpx.Response(
                200,
                json={"values": [{"hash": "e" * 40, "message": "Fix parser\n\nDetails"}]},
            )
        if path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 3,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        entry("AGENTS.md"),
                        {"type": "commit_directory", "path": ".review", "commit": {"hash": BASE}},
                        {"type": "commit_directory", "path": "src", "commit": {"hash": BASE}},
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/.review/":
            return httpx.Response(
                200,
                json={"size": 1, "page": 1, "pagelen": 100, "values": [entry(".review/rules.md")]},
            )
        if path == f"{PREFIX}/src/{BASE}/src/":
            return httpx.Response(
                200, json={"size": 1, "page": 1, "pagelen": 100, "values": [entry("src/app.py")]}
            )
        if path == f"{PREFIX}/src/{BASE}/AGENTS.md":
            return httpx.Response(200, content=files["AGENTS.md"])
        if path == f"{PREFIX}/src/{BASE}/.review/rules.md":
            return httpx.Response(200, content=files[".review/rules.md"])
        if path == f"{PREFIX}/src/{BASE}/src/app.py":
            return httpx.Response(200, content=files["src/app.py"])
        if path == f"{PREFIX}/src/{HEAD}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        {"type": "commit_directory", "path": "src", "commit": {"hash": HEAD}}
                    ],
                },
            )
        if path == f"{PREFIX}/src/{HEAD}/src/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [{**entry("src/app.py"), "commit": {"hash": HEAD}}],
                },
            )
        if path == f"{PREFIX}/src/{HEAD}/src/app.py":
            return httpx.Response(200, content=files["src/app.py"])
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            loaded = await LoadPolicyDocuments(policy, policy_base_sha=BASE).execute()
            assert loaded.agents.document.text == "Use uv.\n"
            assert loaded.rules.document.text == "No secrets.\n"
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=RUN_ID)
            assert (await source.fetch_agents_md(INPUT.repository_id)).content == "Use uv.\n"
            listing = await source.fetch_tree(INPUT.repository_id)
            assert listing.listing_complete and listing.sizes_complete
            assert {file.path for file in listing.files} == {
                "AGENTS.md",
                ".review/rules.md",
                "src/app.py",
            }
            assert (await source.fetch_files(INPUT.repository_id, ("src/app.py",)))[
                0
            ].content == "new\n"
            meta = await source.get_pull_request_meta(RUN_ID)
            assert meta is not None
            assert meta.commit_messages == ("Fix parser\n\nDetails",)
            assert not meta.commit_messages_incomplete
            revision = BitbucketRevisionSource(trees=trees, locator=INPUT.locator)
            head = await revision.tree(HEAD)
            assert head.complete
            assert "src/app.py" in head.blob_shas
            assert await revision.read_blob(head.blob_shas["src/app.py"]) == b"new\n"
        assert all("old-name" not in str(request.url) for request in seen)

    asyncio.run(scenario())


def test_untrusted_policy_tree_pagination_fails_closed() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "page": 1, "pagelen": 100, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 2,
                    "page": 1,
                    "pagelen": 100,
                    "values": [],
                    "next": "https://evil.example/steal?page=2",
                },
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            with pytest.raises(RunFailure):
                await LoadPolicyDocuments(policy, policy_base_sha=BASE).execute()

    asyncio.run(scenario())


def test_agents_policy_file_on_opaque_second_page_is_read() -> None:
    seen: list[httpx.Request] = []
    first = [
        {
            "type": "commit_file",
            "path": f"f{index:03}.txt",
            "size": 1,
            "commit": {"hash": BASE},
        }
        for index in range(100)
    ]

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer secret-token"
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "page": 1, "pagelen": 100, "values": []})
        if path == f"{PREFIX}/src/{BASE}/":
            if request.url.params.get("page") == "cursor/abc":
                return httpx.Response(
                    200,
                    json={
                        "values": [
                            {
                                "type": "commit_file",
                                "path": "AGENTS.md",
                                "size": 8,
                                "commit": {"hash": BASE},
                            }
                        ]
                    },
                )
            assert request.url.params.get("page") == "1"
            return httpx.Response(
                200,
                json={
                    "pagelen": 100,
                    "values": first,
                    "next": f"https://api.bitbucket.org{PREFIX}/src/{BASE}/"
                    "?pagelen=100&page=cursor%2Fabc",
                },
            )
        if path == f"{PREFIX}/src/{BASE}/AGENTS.md":
            return httpx.Response(200, content=b"Use uv.\n")
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            entries = await policy.list_entries(None)
            assert "AGENTS.md" in entries
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=RUN_ID)
            assert (await source.fetch_agents_md(INPUT.repository_id)).content == "Use uv.\n"
        assert any(request.url.params.get("page") == "cursor/abc" for request in seen)

    asyncio.run(scenario())


def test_commit_prefix_is_bounded_and_unavailable_source_fails_explicitly() -> None:
    def page(start: int) -> list[dict[str, str]]:
        return [
            {"hash": f"{index + 1:040x}", "message": f"Commit {index}"}
            for index in range(start, start + 10)
        ]

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "page": 1, "pagelen": 100, "values": []})
        if path == f"{PREFIX}/pullrequests/7/commits":
            if request.url.params.get("page") == "opaque/second":
                return httpx.Response(200, json={"values": page(10)})
            assert request.url.params.get("page") == "1"
            return httpx.Response(
                200,
                json={
                    "values": page(0),
                    "next": f"https://api.bitbucket.org{PREFIX}/pullrequests/7/commits"
                    "?pagelen=10&page=opaque%2Fsecond",
                },
            )
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=INPUT.locator
            )
            prefix = await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)
            assert len(prefix.messages) == 20
            assert prefix.messages[0] == "Commit 0"
            assert prefix.messages[-1] == "Commit 19"
            assert prefix.incomplete

        def unavailable(request: httpx.Request) -> httpx.Response:
            if request.url.path == f"{PREFIX}/pullrequests/7":
                return httpx.Response(200, json=_pr(source=DEST))
            if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
                return httpx.Response(
                    200, json={"size": 0, "page": 1, "pagelen": 100, "values": []}
                )
            if request.url.path == f"{PREFIX}/pullrequests/7/commits":
                return httpx.Response(403)
            raise AssertionError(request.url.path)

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=INPUT.locator
            )
            with pytest.raises(RunFailure, match="commit_messages_fetch_failed"):
                await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)

    asyncio.run(scenario())
