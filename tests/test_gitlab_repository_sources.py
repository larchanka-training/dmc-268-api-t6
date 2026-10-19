"""Public GitLab sources with synthetic HTTP; no server/TLS/SQL proof."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttp
from app.modules.reviews.application.conventions import derive_languages
from app.modules.reviews.infrastructure.gitlab_policy_source import GitLabPolicyDocumentSource
from app.modules.reviews.infrastructure.gitlab_repository_tree import GitLabRepositoryTree
from app.modules.reviews.infrastructure.gitlab_revision_source import GitLabRevisionSource
from app.modules.reviews.infrastructure.gitlab_run_source import GitLabRunSource
from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
from tests.test_gitlab_run_http import INPUT, POLICY, Credentials
from tests.test_gitlab_vcs import Router, reply

RUN_ID = UUID(int=12)


async def checkpoint() -> None:
    pass


@dataclass
class SourceRouter:
    requests: list[httpx.Request] = field(default_factory=list)
    vcs: Router = field(default_factory=Router)
    files: dict[str, tuple[str, bytes]] = field(
        default_factory=lambda: {
            "src/app.py": ("d" * 40, b"new"),
            "web/app.ts": ("e" * 40, b"x"),
            "notes.unknown": ("f" * 40, b"unknown"),
            "empty.py": ("0" * 40, b""),
            "AGENTS.md": ("1" * 40, b"Use uv.\n"),
            ".review/rules.md": ("2" * 40, b"```review-ignore\ngenerated/**\n```\n"),
        }
    )

    def entry(self, path: str) -> dict[str, Any]:
        return {
            "id": self.files[path][0],
            "name": path.rsplit("/", 1)[-1],
            "path": path,
            "type": "blob",
            "mode": "100644",
        }

    def response(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/repository/tree"):
            path = request.url.params.get("path", "")
            recursive = request.url.params.get("recursive") == "true"
            if path == ".review":
                return reply([self.entry(".review/rules.md")])
            if recursive:
                return reply(
                    [
                        self.entry(name)
                        for name in ("src/app.py", "web/app.ts", "notes.unknown", "empty.py")
                    ]
                )
            return reply(
                [
                    self.entry("AGENTS.md"),
                    {
                        "id": "3" * 40,
                        "name": ".review",
                        "path": ".review",
                        "type": "tree",
                        "mode": "040000",
                    },
                ]
            )
        if "/repository/files/" in request.url.path:
            path = unquote(request.url.path.split("/repository/files/")[1])
            if path not in self.files:
                return reply(status=404)
            oid, raw = self.files[path]
            return reply(
                raw=b"",
                headers={
                    "X-Gitlab-Blob-Id": oid,
                    "X-Gitlab-Size": str(len(raw)),
                    "X-Gitlab-Ref": request.url.params["ref"],
                },
            )
        if "/repository/blobs/" in request.url.path:
            oid = request.url.path.split("/repository/blobs/")[1].split("/")[0]
            found = next((raw for sha, raw in self.files.values() if sha == oid), None)
            return reply(raw=found) if found is not None else reply(status=404)
        return self.vcs.response(request)


def test_shared_sources_discover_every_actual_size_including_zero_and_unknown() -> None:
    async def scenario() -> None:
        router = SourceRouter()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            source = GitLabRunSource(
                trees=trees, vcs=HttpGitLabVcsProvider(http=http), run_id=RUN_ID
            )
            tree = await source.fetch_tree(INPUT.repository_id)
            assert tree.listing_complete and tree.sizes_complete
            assert [(file.path, file.size) for file in tree.files] == [
                ("empty.py", 0),
                ("notes.unknown", 7),
                ("src/app.py", 3),
                ("web/app.ts", 1),
            ]
            assert derive_languages(tree.files) == {"Python": 75, "TypeScript": 25}
            before = len(router.requests)
            assert await source.fetch_tree(INPUT.repository_id) == tree
            assert len(router.requests) == before
            files = await source.fetch_files(
                INPUT.repository_id, ("src/app.py", "web/app.ts", "unlisted.py")
            )
            assert [(file.path, file.content) for file in files] == [
                ("src/app.py", "new"),
                ("web/app.ts", "x"),
            ]
        heads = [request for request in router.requests if request.method == "HEAD"]
        assert len(heads) == 4 and all("/projects/29/" in str(request.url) for request in heads)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "kind", ["two_pages", "full_without_link", "repeated", "foreign", "wrong_ref", "duplicate_path"]
)
def test_keyset_discovery_uses_only_valid_continuation_and_proves_completeness(kind: str) -> None:
    async def scenario() -> None:
        router = SourceRouter()
        pages: list[str | None] = []

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repository/tree"):
                token = request.url.params.get("page_token")
                pages.append(token)
                if kind == "full_without_link":
                    return reply(
                        [
                            {
                                "id": "d" * 40,
                                "name": f"f{i}.py",
                                "path": f"f{i}.py",
                                "type": "blob",
                                "mode": "100644",
                            }
                            for i in range(100)
                        ]
                    )
                if token is not None and kind != "repeated":
                    return reply(
                        [router.entry("src/app.py" if kind == "duplicate_path" else "web/app.ts")]
                    )
                url = str(request.url.copy_set_param("page_token", "next-token"))
                if kind == "foreign":
                    url = url.replace("gitlab.com", "foreign.invalid")
                if kind == "wrong_ref":
                    url = url.replace("a" * 40, "b" * 40)
                return reply([router.entry("src/app.py")], headers={"Link": f'<{url}>; rel="next"'})
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
            )
            result = await source.fetch_tree(INPUT.repository_id)
            assert result.listing_complete is (kind == "two_pages")
            assert result.sizes_complete is (kind == "two_pages")
            if kind == "two_pages":
                assert pages == [None, "next-token"] and len(result.files) == 2
            else:
                assert result.incomplete_reasons

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["missing", "wrong_oid", "wrong_ref"])
def test_partial_size_discovery_prevents_real_conventions_model_and_save(fault: str) -> None:
    from app.modules.reviews.application.conventions import (
        ActiveConventionsPrompt,
        GenerateRepoConventions,
    )
    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable
    from tests.test_repo_conventions import FakeModel, FakeStore, FakeUnitOfWorkFactory, draft

    async def scenario() -> None:
        router = SourceRouter()
        model = FakeModel(draft())
        store = FakeStore()

        def external(request: httpx.Request) -> httpx.Response:
            if request.method == "HEAD" and "notes.unknown" in request.url.path:
                if fault == "missing":
                    return reply(status=404)
                return reply(
                    raw=b"",
                    headers={
                        "X-Gitlab-Blob-Id": ("9" if fault == "wrong_oid" else "f") * 40,
                        "X-Gitlab-Ref": ("b" * 40 if fault == "wrong_ref" else "a" * 40),
                        "X-Gitlab-Size": "7",
                    },
                )
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
            )
            result = await source.fetch_tree(INPUT.repository_id)
            assert result.listing_complete and not result.sizes_complete
            assert all(file.path != "notes.unknown" for file in result.files)
            with pytest.raises(RequiredSourceUnavailable):
                await GenerateRepoConventions(source, model, FakeUnitOfWorkFactory(store)).execute(
                    repository_id=INPUT.repository_id,
                    conventions_prompt=ActiveConventionsPrompt(UUID(int=19), "system"),
                    run_id=RUN_ID,
                    changed_files=("app/main.py",),
                )
        assert not model.calls and not store.saved

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (401, "policy_access_denied"),
        (403, "policy_access_denied"),
        (404, "policy_revision_unavailable"),
        (429, "policy_rate_limited"),
        (500, "policy_fetch_transient"),
    ],
)
def test_policy_failure_is_never_optional_absence(status: int, code: str) -> None:
    from app.modules.reviews.application.policy_documents import LoadPolicyDocuments
    from app.modules.reviews.application.policy_errors import PolicyError

    async def scenario() -> None:
        router = SourceRouter()

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/repository/tree"):
                return reply(status=status, headers={"Retry-After": "300"})
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            source = GitLabPolicyDocumentSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                policy_base_sha=INPUT.base_sha,
            )
            with pytest.raises(PolicyError) as error:
                await LoadPolicyDocuments(source, policy_base_sha=INPUT.base_sha).execute()
            assert error.value.error_code == code and "GitHub" not in str(error.value)
            if status == 429:
                assert (
                    error.value.retry_after is not None
                    and error.value.retry_after.total_seconds() == 300
                )

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"x" * 65536, None),
        (b"x" * 65537, "policy_document_too_large"),
        (b"\xff", "policy_document_decode_error"),
        (b"version https://git-lfs.github.com/spec/v1\n", "policy_document_type_unsupported"),
    ],
)
def test_policy_exact_raw_limit_and_shared_normalization(raw: bytes, code: str | None) -> None:
    from app.modules.reviews.application.policy_documents import LoadPolicyDocuments
    from app.modules.reviews.application.policy_errors import PolicyError

    async def scenario() -> None:
        router = SourceRouter()
        router.files["AGENTS.md"] = ("1" * 40, raw)
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            source = GitLabPolicyDocumentSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                policy_base_sha=INPUT.base_sha,
            )
            if code is None:
                result = await LoadPolicyDocuments(source, policy_base_sha=INPUT.base_sha).execute()
                assert result.agents.raw == raw and result.agents.document.blob_oid == "1" * 40
            else:
                with pytest.raises(PolicyError) as error:
                    await LoadPolicyDocuments(source, policy_base_sha=INPUT.base_sha).execute()
                assert error.value.error_code == code

    asyncio.run(scenario())


@pytest.mark.parametrize("empty", [True, False])
def test_only_complete_parent_proves_policy_absence_and_unknown_directory_never_routes(
    empty: bool,
) -> None:
    from app.modules.reviews.application.policy_documents import LoadPolicyDocuments
    from app.modules.reviews.application.policy_errors import PolicyError
    from app.modules.reviews.application.policy_snapshot import DocumentPresence

    async def scenario() -> None:
        router = SourceRouter()

        def external(request: httpx.Request) -> httpx.Response:
            if empty and request.url.path.endswith("/repository/tree"):
                return reply([])
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            source = GitLabPolicyDocumentSource(trees=trees, policy_base_sha=INPUT.base_sha)
            loaded = await LoadPolicyDocuments(source, policy_base_sha=INPUT.base_sha).execute()
            assert (loaded.agents.document.presence is DocumentPresence.MISSING) == empty
            before = len(router.requests)
            with pytest.raises(PolicyError):
                await source.list_entries("9" * 40)
            assert len(router.requests) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("matching", [True, False])
def test_distributed_revision_cache_requires_actual_pinned_head_before_raw(matching: bool) -> None:
    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabRunHttpError
    from app.modules.reviews.application.ast_resolution import RevisionTree

    class Cache:
        async def get(self, revision: str) -> RevisionTree | None:
            return RevisionTree(revision, {"src/app.py": ("d" if matching else "9") * 40}, True)

        async def put(self, tree: RevisionTree) -> None:
            raise AssertionError("a valid distributed hit is not rewritten")

    async def scenario() -> None:
        router = SourceRouter()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            revision = GitLabRevisionSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                locator=INPUT.locator,
                tree_cache=Cache(),
            )
            result = await revision.tree(INPUT.head_sha)
            assert result.complete
            if matching:
                assert await revision.read_blob("d" * 40) == b"new"
                before = len(router.requests)
                assert (
                    await revision.read_blob("d" * 40) == b"new" and len(router.requests) == before
                )
            else:
                with pytest.raises(GitLabRunHttpError):
                    await revision.read_blob("9" * 40)
        assert not any(request.url.path.endswith("/repository/tree") for request in router.requests)
        heads = [i for i, request in enumerate(router.requests) if request.method == "HEAD"]
        raws = [
            i
            for i, request in enumerate(router.requests)
            if "/repository/blobs/" in request.url.path
        ]
        assert len(heads) == 1 and (heads[0] < raws[0] if matching else not raws)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "method",
    [
        "tree",
        "files",
        "agents",
        "metadata",
        "revision_tree",
        "revision_blob",
        "policy_list",
        "policy_blob",
    ],
)
@pytest.mark.parametrize("control", ["cancel", "deadline", "required", "task"])
def test_all_source_public_calls_preserve_caller_control_before_cached_or_wire_return(
    method: str, control: str
) -> None:
    from app.modules.reviews.application.run_failures import (
        RequiredSourceUnavailable,
        RunCancelled,
        RunFailure,
    )

    async def scenario() -> None:
        router = SourceRouter()
        failure: BaseException | None = None

        async def caller() -> None:
            if failure is not None:
                raise failure

        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=caller)
            source = GitLabRunSource(
                trees=trees, vcs=HttpGitLabVcsProvider(http=http), run_id=RUN_ID
            )
            revision = GitLabRevisionSource(trees=trees, locator=INPUT.locator)
            policy = GitLabPolicyDocumentSource(trees=trees, policy_base_sha=INPUT.base_sha)
            await source.fetch_tree(INPUT.repository_id)
            await source.fetch_files(INPUT.repository_id, ("src/app.py",))
            await revision.tree(INPUT.head_sha)
            await policy.list_entries(None)
            failure = {
                "cancel": RunCancelled("cancelled_by_user"),
                "deadline": RunFailure("deadline_exceeded", "deadline"),
                "required": RequiredSourceUnavailable("diff_fetch_failed"),
                "task": asyncio.CancelledError(),
            }[control]
            before = len(router.requests)
            with pytest.raises(type(failure)) as error:
                if method == "tree":
                    await source.fetch_tree(INPUT.repository_id)
                elif method == "files":
                    await source.fetch_files(INPUT.repository_id, ("src/app.py",))
                elif method == "agents":
                    await source.fetch_agents_md(INPUT.repository_id)
                elif method == "metadata":
                    await source.get_pull_request_meta(RUN_ID)
                elif method == "revision_tree":
                    await revision.tree(INPUT.head_sha)
                elif method == "revision_blob":
                    await revision.read_blob("d" * 40)
                elif method == "policy_list":
                    await policy.list_entries(None)
                else:
                    await policy.read_blob("1" * 40)
            assert error.value is failure and len(router.requests) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["run", "revision", "policy"])
def test_cached_tree_retains_actual_bound_authority_fence(method: str) -> None:
    from app.modules.reviews.application.run_failures import RunCancelled

    async def scenario() -> None:
        router = SourceRouter()
        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            run = GitLabRunSource(trees=trees, vcs=HttpGitLabVcsProvider(http=http), run_id=RUN_ID)
            await run.fetch_tree(INPUT.repository_id)
            credentials.failure = RunCancelled("installation_unavailable")
            before = len(router.requests)
            with pytest.raises(RunCancelled) as error:
                if method == "run":
                    await run.fetch_tree(INPUT.repository_id)
                elif method == "revision":
                    await GitLabRevisionSource(trees=trees, locator=INPUT.locator).tree(
                        INPUT.head_sha
                    )
                else:
                    await GitLabPolicyDocumentSource(
                        trees=trees, policy_base_sha=INPUT.base_sha
                    ).list_entries(None)
            assert error.value is credentials.failure and len(router.requests) == before

    asyncio.run(scenario())


def test_already_spent_shared_send_budget_cannot_be_reset_by_tree_reader() -> None:
    from app.modules.integrations.gitlab.infrastructure.run_http import GitLabMergeRequestResource

    async def scenario() -> None:
        router = SourceRouter()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            for _ in range(511):
                await http.get_json(GitLabMergeRequestResource(), max_bytes=1048576)
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
            )
            result = await source.fetch_tree(INPUT.repository_id)
            assert not result.listing_complete and result.incomplete_reasons == (
                "budget_exhausted",
            )
            assert len(router.requests) == 512

    asyncio.run(scenario())


def test_policy_raw_oid_must_belong_to_its_own_complete_target_listing() -> None:
    from app.modules.reviews.application.policy_errors import PolicyError

    async def scenario() -> None:
        router = SourceRouter()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            trees = GitLabRepositoryTree(http=http, checkpoint=checkpoint)
            await trees.tree(INPUT.head_sha, side="head")
            policy = GitLabPolicyDocumentSource(trees=trees, policy_base_sha=INPUT.base_sha)
            await policy.list_entries(None)
            before = len(router.requests)
            with pytest.raises(PolicyError):
                await policy.read_blob("d" * 40)
            assert len(router.requests) == before

    asyncio.run(scenario())


@pytest.mark.parametrize("base", [False, True])
def test_run_agents_blob_identity_metadata_and_conventions_revision_are_pinned(base: bool) -> None:
    from app.modules.reviews.application.run_failures import RunCancelled

    async def scenario() -> None:
        router = SourceRouter()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
                conventions_from_base=base,
            )
            agents = await source.fetch_agents_md(INPUT.repository_id)
            assert (agents.content, agents.sha) == ("Use uv.\n", "1" * 40)
            await source.fetch_tree(INPUT.repository_id)
            meta = await source.get_pull_request_meta(RUN_ID)
            assert (
                meta is not None
                and meta.commit_messages == ("Subject",)
                and not meta.commit_messages_incomplete
            )
            before = len(router.requests)
            with pytest.raises(RunCancelled):
                await source.get_pull_request_meta(UUID(int=999))
            assert len(router.requests) == before
        listings = [
            request for request in router.requests if request.url.path.endswith("/repository/tree")
        ]
        assert listings[0].url.params["ref"] == INPUT.base_sha and "/projects/17/" in str(
            listings[0].url
        )
        recursive = next(
            request for request in listings if request.url.params["recursive"] == "true"
        )
        assert recursive.url.params["ref"] == (INPUT.base_sha if base else INPUT.head_sha)
        assert ("/projects/17/" if base else "/projects/29/") in str(recursive.url)

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["os", "sql"])
def test_operational_caller_checkpoint_is_required_source_failure(kind: str) -> None:
    from sqlalchemy.exc import OperationalError

    from app.modules.reviews.application.run_failures import RequiredSourceUnavailable

    async def scenario() -> None:
        router = SourceRouter()

        async def failing() -> None:
            if kind == "os":
                raise OSError("unavailable")
            raise OperationalError("statement", {}, OSError("unavailable"))

        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            source = GitLabRunSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=failing),
                vcs=HttpGitLabVcsProvider(http=http),
                run_id=RUN_ID,
            )
            with pytest.raises(RequiredSourceUnavailable) as error:
                await source.fetch_tree(INPUT.repository_id)
            assert error.value.error_code == "diff_fetch_failed"
        assert router.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("complete", [True, False])
def test_revision_cache_saves_only_complete_actual_tree(complete: bool) -> None:
    from app.modules.reviews.application.ast_resolution import RevisionTree

    class Cache:
        def __init__(self) -> None:
            self.saved: list[RevisionTree] = []

        async def get(self, revision: str) -> RevisionTree | None:
            return None

        async def put(self, tree: RevisionTree) -> None:
            self.saved.append(tree)

    async def scenario() -> None:
        router = SourceRouter()
        cache = Cache()

        def external(request: httpx.Request) -> httpx.Response:
            if not complete and request.url.path.endswith("/repository/tree"):
                return reply(
                    [
                        {
                            "id": "d" * 40,
                            "name": f"f{i}.py",
                            "path": f"f{i}.py",
                            "type": "blob",
                            "mode": "100644",
                        }
                        for i in range(100)
                    ]
                )
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            source = GitLabRevisionSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                locator=INPUT.locator,
                tree_cache=cache,
            )
            tree = await source.tree(INPUT.head_sha)
            assert tree.complete is complete
            assert cache.saved == ([tree] if complete else [])

    asyncio.run(scenario())


@pytest.mark.parametrize("path", ["\ud800.py", "\udfff.py"])
def test_malformed_unicode_cached_path_recovers_from_authoritative_tree(path: str) -> None:
    from app.modules.reviews.application.ast_resolution import RevisionTree

    class Cache:
        def __init__(self) -> None:
            self.saved: list[RevisionTree] = []

        async def get(self, revision: str) -> RevisionTree | None:
            return RevisionTree(revision, {path: "d" * 40}, True)

        async def put(self, tree: RevisionTree) -> None:
            self.saved.append(tree)

    async def scenario() -> None:
        router = SourceRouter()
        cache = Cache()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            revision = GitLabRevisionSource(
                trees=GitLabRepositoryTree(http=http, checkpoint=checkpoint),
                locator=INPUT.locator,
                tree_cache=cache,
            )
            tree = await revision.tree(INPUT.head_sha)
            assert tree.complete and path not in tree.blob_shas
            assert tree.blob_shas["src/app.py"] == "d" * 40
            assert cache.saved == [tree]
            assert await revision.read_blob("d" * 40) == b"new"
        assert any(
            request.url.path == "/api/v4/projects/29/repository/tree"
            and request.url.params["ref"] == INPUT.head_sha
            for request in router.requests
        )

    asyncio.run(scenario())
