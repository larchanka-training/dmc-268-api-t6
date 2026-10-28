"""Pinned Bitbucket policy, conventions and context source reads."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.infrastructure import run_http
from app.modules.integrations.bitbucket.infrastructure.run_http import (
    BitbucketRunHttp,
    BitbucketRunHttpError,
)
from app.modules.reviews.application.diff_map import VcsProviderName, build_diff_map
from app.modules.reviews.application.policy_documents import LoadPolicyDocuments, missing_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    PromptBindings,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.review_context import BuildReviewContext, ContextLevel
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import FetchVcsReviewInput
from app.modules.reviews.infrastructure.bitbucket_policy_source import BitbucketPolicyDocumentSource
from app.modules.reviews.infrastructure.bitbucket_repository_tree import BitbucketRepositoryTree
from app.modules.reviews.infrastructure.bitbucket_revision_source import BitbucketRevisionSource
from app.modules.reviews.infrastructure.bitbucket_run_source import BitbucketRunSource
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider
from tests.test_bitbucket_vcs import BASE, DEST, HEAD, PREFIX, TARGET, Credentials, _locator, _pr
from tests.test_review_context import InProcessParser, Words

RUN_ID = UUID(int=44)
INPUT = RunVcsInput(UUID(int=45), TARGET.repository_id, HEAD, BASE, _locator())


async def checkpoint() -> None:
    return None


@pytest.mark.parametrize("file_count", [100, 200])
def test_tree_walk_rechecks_pr_only_at_stage_boundary(file_count: int) -> None:
    pr_reads = 0
    listing_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pr_reads, listing_reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            pr_reads += 1
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            listing_reads += 1
            values = [
                {
                    "type": "commit_directory",
                    "path": f"d{index:02}",
                    "commit": {"hash": BASE},
                }
                for index in range(20)
            ]
            return httpx.Response(200, json={"size": 20, "values": values})
        if f"{PREFIX}/src/{BASE}/d" in request.url.path:
            listing_reads += 1
            directory = request.url.path.rsplit("/", 2)[-2]
            file_values = [
                {
                    "type": "commit_file",
                    "path": f"{directory}/f{index:03}.py",
                    "size": 1,
                    "commit": {"hash": BASE},
                }
                for index in range(file_count // 20)
            ]
            return httpx.Response(200, json={"size": len(file_values), "values": file_values})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            listing = await trees.walk(BASE)
            assert listing.complete
            assert sum(entry.kind == "file" for entry in listing.entries) == file_count

    asyncio.run(scenario())
    assert listing_reads == 21
    assert pr_reads == 3


def test_tree_rate_limit_propagates_retry_delay() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(429, headers={"Retry-After": "120"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            with pytest.raises(RunFailure) as error:
                await trees.walk(BASE)
            assert error.value.retry_after == timedelta(seconds=120)

    asyncio.run(scenario())


def test_tree_walk_does_not_convert_request_budget_to_partial_listing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if path == f"{PREFIX}/src/{BASE}/":
            monkeypatch.setattr(run_http, "_MAX_REQUESTS", len(seen))
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {"type": "commit_directory", "path": "sub", "commit": {"hash": BASE}}
                    ],
                },
            )
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            with pytest.raises(BitbucketRunHttpError) as caught:
                await trees.walk(BASE)
            assert caught.value.error_code == "bitbucket_request_budget_exceeded"

    asyncio.run(scenario())
    assert seen[-1] == f"{PREFIX}/src/{BASE}/"


def test_tree_context_skips_symlink_and_non_utf8_bytes() -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 3,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": name,
                            "size": 1,
                            "attributes": ["link"] if name == "linked.py" else [],
                            "commit": {"hash": BASE},
                        }
                        for name in ("linked.py", "invalid.py", "valid.py")
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/invalid.py":
            return httpx.Response(200, content=b"\xff")
        if path == f"{PREFIX}/src/{BASE}/valid.py":
            return httpx.Response(200, content=b"x")
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=RUN_ID)
            tree = await source.fetch_tree(INPUT.repository_id)
            assert tuple(file.path for file in tree.files) == ("invalid.py", "valid.py")
            files = await source.fetch_files(
                INPUT.repository_id, ("linked.py", "invalid.py", "valid.py")
            )
            assert tuple((file.path, file.content) for file in files) == (("valid.py", "x"),)

    asyncio.run(scenario())
    assert f"{PREFIX}/src/{BASE}/linked.py" not in seen


def test_optional_file_429_stops_context_fetch() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "app.py",
                            "size": 1,
                            "commit": {"hash": BASE},
                        }
                    ],
                },
            )
        if request.url.path == f"{PREFIX}/src/{BASE}/app.py":
            return httpx.Response(429, headers={"Retry-After": "3600"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            source = BitbucketRunSource(trees=trees, vcs=vcs, run_id=RUN_ID)
            assert (await source.fetch_tree(INPUT.repository_id)).listing_complete
            with pytest.raises(RunFailure) as error:
                await source.fetch_files(INPUT.repository_id, ("app.py",))
            assert error.value.retry_after == timedelta(hours=1)

    asyncio.run(scenario())


def test_commit_messages_429_keeps_retry_after() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/pullrequests/7/commits":
            return httpx.Response(429, headers={"Retry-After": "300"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=INPUT.locator
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)
            assert error.value.retry_after == timedelta(minutes=5)

    asyncio.run(scenario())


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


def test_changed_bitbucket_diff_blob_reaches_l3_context_with_real_adapters() -> None:
    source = b"def compute():\n    return 2\n"
    requests: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=HEAD[:12], base=BASE[:12], source=DEST))
        if path == f"{PREFIX}/commit/{HEAD[:12]}":
            return httpx.Response(200, json={"hash": HEAD})
        if path == f"{PREFIX}/commit/{BASE[:12]}":
            return httpx.Response(200, json={"hash": BASE})
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        {
                            "status": "modified",
                            "lines_added": 1,
                            "lines_removed": 1,
                            "old": {"path": "src/app.py"},
                            "new": {"path": "src/app.py", "size": len(source)},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/app.py b/src/app.py\n"
                    "--- a/src/app.py\n+++ b/src/app.py\n"
                    "@@ -1,2 +1,2 @@\n def compute():\n-    return 1\n+    return 2\n"
                ),
            )
        if path == f"{PREFIX}/src/{HEAD}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
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
                    "values": [
                        {
                            "type": "commit_file",
                            "path": "src/app.py",
                            "size": len(source),
                            "commit": {"hash": HEAD},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/src/{HEAD}/src/app.py":
            return httpx.Response(200, content=source)
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            fetched = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                INPUT.locator, HEAD, BASE
            )
            diff_map = build_diff_map(
                fetched.pull_request,
                fetched.files,
                provider=VcsProviderName.BITBUCKET,
                repository_id=INPUT.repository_id,
                diff_old_sha=BASE,
            )
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            revision = BitbucketRevisionSource(trees=trees, locator=INPUT.locator)
            snapshot = build_policy_snapshot(
                RunPolicyPins(
                    UUID(int=2),
                    INPUT.repository_id,
                    BASE,
                    UUID(int=4),
                    "2" * 64,
                    PromptBindings("3" * 64, "", ""),
                    ExecutionProfile.fast("model-a", ""),
                ),
                rules=missing_document(RULES_DOCUMENT_PATH),
                agents=missing_document(AGENTS_DOCUMENT_PATH),
                ignore=parse_review_ignore(None),
                snapshot_id=UUID(int=3),
            )
            matcher = IgnoreMatcher(parse_review_ignore(None))
            payload = await BuildReviewContext(
                source=revision, parser=InProcessParser(), counter=Words()
            ).execute(
                diff_map=diff_map,
                snapshot=snapshot,
                matcher=matcher,
                ignore=apply_ignore(matcher, diff_map),
                token_budget=100_000,
            )
            assert ContextLevel.L3 in payload.levels_present
            assert any(
                "return 2" in block.text
                for block in payload.blocks
                if block.level is ContextLevel.L3
            )
            with pytest.raises(RunCancelled, match="superseded"):
                await revision.read_blob("0" * 64)

    asyncio.run(scenario())
    assert all(request.url.host == "api.bitbucket.org" for request in requests)


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


@pytest.mark.parametrize(
    ("status", "expected_code"),
    [
        (401, PolicyErrorCode.ACCESS_DENIED),
        (403, PolicyErrorCode.ACCESS_DENIED),
        (404, PolicyErrorCode.REVISION_UNAVAILABLE),
        (500, PolicyErrorCode.FETCH_TRANSIENT),
    ],
)
def test_policy_tree_http_error_keeps_status_and_classification(
    status: int, expected_code: PolicyErrorCode
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(status)
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            with pytest.raises(PolicyError) as error:
                await policy.list_entries(None)
            assert error.value.code is expected_code
            assert error.value.http_status == status
            assert error.value.pin_head is (status == 401)

    asyncio.run(scenario())


def test_oversized_policy_tree_listing_is_revision_unavailable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            with pytest.raises(PolicyError) as error:
                await policy.list_entries(None)
            assert error.value.code is PolicyErrorCode.REVISION_UNAVAILABLE
            assert error.value.limit is None

    asyncio.run(scenario())


def test_malformed_policy_tree_listing_is_retryable() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if request.url.path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(200, content=b"not-json")
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            with pytest.raises(PolicyError) as error:
                await policy.list_entries(None)
            assert error.value.code is PolicyErrorCode.FETCH_TRANSIENT
            assert error.value.retryable

    asyncio.run(scenario())


@pytest.mark.parametrize("blob_response", ["unauthorized", "overflow"])
def test_policy_blob_failure_keeps_diagnostic_after_document_location(blob_response: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 0, "values": []})
        if path == f"{PREFIX}/src/{BASE}/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_directory",
                            "path": ".review",
                            "commit": {"hash": BASE},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/.review/":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "type": "commit_file",
                            "path": ".review/rules.md",
                            "size": 10,
                            "commit": {"hash": BASE},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/src/{BASE}/.review/rules.md":
            if blob_response == "overflow":
                return httpx.Response(200, content=b"x" * 65_537)
            return httpx.Response(401)
        raise AssertionError(path)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=INPUT.locator)
            trees = BitbucketRepositoryTree(
                http=http, expected_input=INPUT, vcs=vcs, checkpoint=checkpoint
            )
            policy = BitbucketPolicyDocumentSource(trees=trees, policy_base_sha=BASE)
            with pytest.raises(PolicyError) as error:
                await LoadPolicyDocuments(policy, policy_base_sha=BASE).execute()
            assert error.value.source_path == ".review/rules.md"
            if blob_response == "overflow":
                assert error.value.code is PolicyErrorCode.DOCUMENT_TOO_LARGE
                assert error.value.limit == 65_536
                assert error.value.actual == 65_537
                assert error.value.actual_is_lower_bound
                assert "actual more than 65536" in str(error.value)
            else:
                assert error.value.code is PolicyErrorCode.ACCESS_DENIED
                assert error.value.http_status == 401
                assert error.value.pin_head

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
