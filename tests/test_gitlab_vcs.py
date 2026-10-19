"""Coherent public GitLab VCS/Fetch proof; MockTransport is synthetic, not TLS."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabRunHttp,
    GitLabRunHttpError,
)
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    OmissionReason,
    VcsPullRequest,
)
from app.modules.reviews.infrastructure.gitlab_vcs import HttpGitLabVcsProvider
from tests.test_gitlab_run_http import INPUT, POLICY, Credentials, Stream


async def checkpoint() -> None:
    pass


def refs() -> dict[str, str]:
    return {"head_sha": "a" * 40, "start_sha": "b" * 40, "base_sha": "c" * 40}


def version_refs() -> dict[str, str]:
    return {"head_commit_sha": "a" * 40, "start_commit_sha": "b" * 40, "base_commit_sha": "c" * 40}


@dataclass
class Router:
    requests: list[httpx.Request] = field(default_factory=list)
    canonical: dict[str, Any] = field(
        default_factory=lambda: {
            "id": 901,
            "iid": 12,
            "project_id": 17,
            "target_project_id": 17,
            "source_project_id": 29,
            "state": "opened",
            "diff_refs": refs(),
            "changes_count": "2",
            "title": "Title",
            "description": "Description",
            "author": {"username": "author"},
            "source_branch": "feature",
            "target_branch": "main",
            "labels": ["review"],
            "draft": False,
        }
    )
    files: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "old_path": "src/old.py",
                "new_path": "src/new.py",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": True,
                "diff": "@@ -1 +1 @@\n-old\n+new\n",
            },
            {
                "old_path": "asset.dat",
                "new_path": "asset.dat",
                "new_file": False,
                "deleted_file": False,
                "renamed_file": False,
                "diff": "",
            },
        ]
    )
    streams: list[Stream] = field(default_factory=list)

    def response(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path
        headers: dict[str, str] = {"Content-Type": "application/json"}
        status = 200
        data: Any = {}
        raw: bytes | None = None
        if path.endswith("/merge_requests/12"):
            data = self.canonical
        elif path.endswith("/versions"):
            data = [{"id": 9, **version_refs()}]
            headers["X-Next-Page"] = ""
        elif path.endswith("/versions/9"):
            data = {
                "id": 9,
                **version_refs(),
                "state": "collected",
                "real_size": str(len(self.files)),
                "diffs": self.files,
            }
        elif "/repository/files/" in path:
            name = unquote(path.split("/repository/files/")[1])
            oid = "d" * 40 if name == "src/new.py" else "e" * 40
            headers = {
                "X-Gitlab-Blob-Id": oid,
                "X-Gitlab-Size": "3",
                "X-Gitlab-Ref": request.url.params["ref"],
            }
            raw = b""
        elif "/repository/blobs/" in path:
            raw = b"\x00xy" if "/" + "e" * 40 + "/" in path else b"new"
        elif path.endswith("/commits"):
            data = [{"id": "f" * 40, "message": "Subject"}]
            headers["X-Next-Page"] = ""
        else:
            status = 501
        stream = Stream((raw if raw is not None else json.dumps(data).encode(),))
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)


def test_shared_fetch_legacy_rename_binary_uses_pinned_version_and_fork_oids() -> None:
    async def scenario() -> None:
        router = Router()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            result = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                INPUT.locator, INPUT.head_sha, INPUT.base_sha
            )
            assert [(item.filename, item.status) for item in result.files] == [
                ("src/new.py", "renamed"),
                ("asset.dat", "modified"),
            ]
            assert result.omissions[0].reason is OmissionReason.BINARY
            assert result.changed_files[0].path == "src/new.py"
            assert (
                result.pull_request.base_sha,
                result.pull_request.meta.lines_added,
                result.pull_request.meta.lines_removed,
            ) == ("b" * 40, 1, 1)
            assert await vcs.get_merge_base(result.pull_request, checkpoint=checkpoint) == "c" * 40
            assert await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint) == b"new"
        paths = [str(request.url) for request in router.requests]
        assert any("/projects/29/repository/files/" in path for path in paths)
        assert any("/projects/29/repository/blobs/" in path for path in paths)
        assert not any("/diffs" in path or "/changes" in path for path in paths)
        assert all(stream.closed for stream in router.streams)

    asyncio.run(scenario())


def reply(
    data: Any = None,
    *,
    status: int = 200,
    headers: dict[str, str] | None = None,
    raw: bytes | None = None,
) -> httpx.Response:
    return httpx.Response(
        status,
        headers={"Content-Type": "application/json", **(headers or {})},
        stream=Stream((json.dumps(data).encode() if raw is None else raw,)),
    )


def detail(router: Router, **changes: Any) -> dict[str, Any]:
    return {
        "id": 9,
        **version_refs(),
        "state": "collected",
        "real_size": str(len(router.files)),
        "diffs": router.files,
        **changes,
    }


@pytest.mark.parametrize(
    ("state", "outcome"),
    [
        ("closed", "pr_closed"),
        ("merged", "pr_closed"),
        ("locked", "fetch"),
        ("unknown", "fetch"),
        (None, "fetch"),
        ({}, "fetch"),
    ],
)
def test_canonical_state_control_is_precise(state: Any, outcome: str) -> None:
    async def scenario() -> None:
        router = Router()
        router.canonical["state"] = state
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            if outcome == "pr_closed":
                with pytest.raises(RunCancelled, match="pr_closed"):
                    await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
            else:
                with pytest.raises(RunFailure) as error:
                    await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
                assert error.value.error_code == "diff_fetch_failed"
                assert "GitHub" not in str(error.value)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("field", "value", "cancel"),
    [
        ("iid", 99, False),
        ("project_id", 99, False),
        ("target_project_id", 99, False),
        ("source_project_id", True, False),
        ("diff_refs", None, False),
        ("diff_refs", {**refs(), "head_sha": "f" * 40}, True),
        ("diff_refs", {**refs(), "start_sha": "f" * 40}, True),
    ],
)
def test_canonical_identity_and_pinned_refs_fail_before_source_access(
    field: str, value: Any, cancel: bool
) -> None:
    async def scenario() -> None:
        router = Router()
        router.canonical[field] = value
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            with pytest.raises(RunCancelled if cancel else RunFailure):
                await HttpGitLabVcsProvider(http=http).get_pull_request(
                    INPUT.locator, checkpoint=checkpoint
                )
        assert all("/projects/29/" not in str(request.url) for request in router.requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault",
    [
        "overflow",
        "without_files",
        "timeout",
        "wrong_count",
        "wrong_version",
        "wrong_start",
        "wrong_base",
        "duplicate_path",
        "bad_flag",
        "bad_patch",
        "bad_type",
        "too_many",
    ],
)
def test_saved_version_uncertainty_never_becomes_an_empty_complete_diff(fault: str) -> None:
    async def scenario() -> None:
        router = Router()

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/versions/9"):
                data = detail(router)
                if fault in {"overflow", "without_files", "timeout"}:
                    data["state"] = fault
                elif fault == "wrong_count":
                    data["real_size"] = "3"
                elif fault == "wrong_version":
                    data["id"] = 10
                elif fault == "wrong_start":
                    data["start_commit_sha"] = "f" * 40
                elif fault == "wrong_base":
                    data["base_commit_sha"] = "f" * 40
                elif fault == "duplicate_path":
                    data["diffs"] = [router.files[0], router.files[0]]
                elif fault == "bad_flag":
                    data["diffs"] = [{**router.files[0], "collapsed": "true"}, router.files[1]]
                elif fault == "bad_patch":
                    data["diffs"] = [
                        {**router.files[0], "diff": "@@ -1,2 +1 @@\n-old\n+new"},
                        router.files[1],
                    ]
                elif fault == "bad_type":
                    data["diffs"] = [{**router.files[0], "diff": 3}, router.files[1]]
                else:
                    router.canonical["changes_count"] = "201"
                    data["real_size"] = "201"
                return reply(data)
            return router.response(request)

        if fault == "too_many":
            router.canonical["changes_count"] = "201"
        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            with pytest.raises(RunFailure) as error:
                await FetchVcsReviewInput(
                    HttpGitLabVcsProvider(http=http), checkpoint=checkpoint
                ).execute(INPUT.locator, INPUT.head_sha, INPUT.base_sha)
            assert error.value.error_code == "diff_fetch_failed" and "GitHub" not in str(
                error.value
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("lookup", ["positive", "repeated", "exhausted"])
def test_bounded_positive_version_lookup_stops_at_match_and_rejects_observed_repetition(
    lookup: str,
) -> None:
    async def scenario() -> None:
        router = Router()
        pages: list[int] = []

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/versions"):
                page = int(request.url.params["page"])
                pages.append(page)
                if lookup == "positive" and page == 2:
                    return reply([{"id": 9, **version_refs()}], headers={"X-Next-Page": "3"})
                return reply(
                    [
                        {
                            "id": 8 if lookup == "repeated" else page + 20,
                            **version_refs(),
                            "start_commit_sha": "f" * 40,
                        }
                    ],
                    headers={"X-Next-Page": str(page + 1)},
                )
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            if lookup == "positive":
                assert (
                    await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
                ).meta.files_changed == 2
                assert pages == [1, 2]
            else:
                with pytest.raises(RunFailure):
                    await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
                assert pages == ([1, 2] if lookup == "repeated" else [1, 2, 3, 4, 5])

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("flag", "reason"),
    [
        ("too_large", OmissionReason.TOO_LARGE),
        ("collapsed", OmissionReason.MISSING_PATCH),
        ("generated_file", OmissionReason.GENERATED),
    ],
)
def test_newer_flags_are_real_omissions_without_invented_size_or_line_stats(
    flag: str, reason: OmissionReason
) -> None:
    async def scenario() -> None:
        router = Router()
        router.files = [{**router.files[0], flag: True}]
        router.canonical["changes_count"] = "1"
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            value = await FetchVcsReviewInput(
                HttpGitLabVcsProvider(http=http), checkpoint=checkpoint
            ).execute(INPUT.locator, INPUT.head_sha, INPUT.base_sha)
        assert value.omissions[0].reason is reason
        assert value.files[0].size == 3
        assert value.pull_request.meta.files_changed == 1
        assert (value.pull_request.meta.lines_added, value.pull_request.meta.lines_removed) == (
            0,
            0,
        )
        assert value.changed_files == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["empty", "deleted"])
def test_exact_zero_files_and_deleted_old_paths_are_distinct(variant: str) -> None:
    async def scenario() -> None:
        router = Router()
        if variant == "empty":
            router.files = []
            router.canonical["changes_count"] = "0"
        else:
            router.files = [
                {
                    "old_path": "gone.py",
                    "new_path": "gone.py",
                    "new_file": False,
                    "deleted_file": True,
                    "renamed_file": False,
                    "diff": "@@ -1 +0,0 @@\n-old\n",
                }
            ]
            router.canonical["changes_count"] = "1"
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            value = await FetchVcsReviewInput(
                HttpGitLabVcsProvider(http=http), checkpoint=checkpoint
            ).execute(INPUT.locator, INPUT.head_sha, INPUT.base_sha)
            assert value.pull_request.meta.files_changed == (0 if variant == "empty" else 1)
            if variant == "deleted":
                assert value.files[0].blob_sha is None and value.files[0].status == "removed"
        assert not any("/repository/files/" in str(request.url) for request in router.requests)

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["metadata", "diff", "blob", "merge_base", "commits"])
@pytest.mark.parametrize("control", ["cancel", "deadline", "required", "task"])
def test_every_public_caller_checkpoint_preserves_control_before_wire(
    method: str, control: str
) -> None:
    async def scenario() -> None:
        router = Router()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            pr = VcsPullRequest(
                INPUT.locator,
                INPUT.head_sha,
                INPUT.base_sha,
                PullRequestMeta(
                    "Title", None, "author", "feature", "main", (), 0, 0, 0, False, True
                ),
            )
            primary: BaseException = {
                "cancel": RunCancelled("stop"),
                "deadline": RunFailure("deadline_exceeded"),
                "required": RequiredSourceUnavailable("diff_fetch_failed"),
                "task": asyncio.CancelledError(),
            }[control]

            async def stop() -> None:
                raise primary

            with pytest.raises(type(primary)) as error:
                if method == "metadata":
                    await vcs.get_pull_request(INPUT.locator, checkpoint=stop)
                elif method == "diff":
                    await vcs.get_diff(pr, checkpoint=stop)
                elif method == "blob":
                    await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=stop)
                elif method == "merge_base":
                    await vcs.get_merge_base(pr, checkpoint=stop)
                else:
                    await vcs.get_commit_messages(INPUT.locator, checkpoint=stop)
            assert error.value is primary
        assert router.requests == []

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["metadata", "diff", "blob", "merge_base"])
def test_cached_results_keep_public_authority_fence_without_extra_wire(method: str) -> None:
    async def scenario() -> None:
        router = Router()
        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials,
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            pr = await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
            await vcs.get_diff(pr, checkpoint=checkpoint)
            await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint)
            count = len(router.requests)
            credentials.failure = RunCancelled("installation_unavailable")
            with pytest.raises(RunCancelled):
                if method == "metadata":
                    await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
                elif method == "diff":
                    await vcs.get_diff(pr, checkpoint=checkpoint)
                elif method == "blob":
                    await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint)
                else:
                    await vcs.get_merge_base(pr, checkpoint=checkpoint)
            assert len(router.requests) == count

    asyncio.run(scenario())


def test_head_size_mismatch_is_not_returned_or_cached_as_raw_blob() -> None:
    async def scenario() -> None:
        router = Router()

        def external(request: httpx.Request) -> httpx.Response:
            if "/repository/blobs/" in request.url.path:
                return reply(raw=b"wrong length")
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            pr = await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
            await vcs.get_diff(pr, checkpoint=checkpoint)
            with pytest.raises(RunFailure):
                await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint)
            with pytest.raises(RunFailure):
                await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint)

    asyncio.run(scenario())


def test_empty_raw_bytes_are_a_cache_hit_and_unknown_oid_never_selects_project() -> None:
    async def scenario() -> None:
        router = Router()
        router.files = router.files[:1]
        router.canonical["changes_count"] = "1"
        raw_calls = 0

        def external(request: httpx.Request) -> httpx.Response:
            nonlocal raw_calls
            if request.method == "HEAD":
                return reply(
                    raw=b"",
                    headers={
                        "X-Gitlab-Blob-Id": "d" * 40,
                        "X-Gitlab-Size": "0",
                        "X-Gitlab-Ref": "a" * 40,
                    },
                )
            if "/repository/blobs/" in request.url.path:
                raw_calls += 1
                return reply(raw=b"")
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            pr = await vcs.get_pull_request(INPUT.locator, checkpoint=checkpoint)
            await vcs.get_diff(pr, checkpoint=checkpoint)
            assert await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint) == b""
            assert await vcs.get_blob(INPUT.locator, "d" * 40, checkpoint=checkpoint) == b""
            with pytest.raises(RunFailure):
                await vcs.get_blob(INPUT.locator, "f" * 40, checkpoint=checkpoint)
            assert raw_calls == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [401, 403, 404, 429, 500, 302])
def test_required_http_failure_is_provider_correct_and_sanitized(status: int) -> None:
    async def scenario() -> None:
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(
                lambda request: reply(
                    status=status,
                    raw=b"provider-secret-canary",
                    headers={"Retry-After": "300", "Location": "https://evil.invalid"},
                )
            ),
        ) as http:
            with pytest.raises(RunFailure) as error:
                await HttpGitLabVcsProvider(http=http).get_pull_request(
                    INPUT.locator, checkpoint=checkpoint
                )
            assert error.value.error_code == "diff_fetch_failed"
            assert (
                "GitLab" in str(error.value)
                and "GitHub" not in str(error.value)
                and "canary" not in str(error.value)
            )
            assert error.value.retry_after == timedelta(seconds=300)

    asyncio.run(scenario())


@pytest.mark.parametrize("variant", ["full", "moved", "evil_link"])
def test_commit_prefix_api_order_limits_tail_and_remote_head_recheck(variant: str) -> None:
    async def scenario() -> None:
        router = Router()
        commit_calls = 0

        def external(request: httpx.Request) -> httpx.Response:
            nonlocal commit_calls
            if request.url.path.endswith("/commits"):
                commit_calls += 1
                page = int(request.url.params["page"])
                batch = [
                    {
                        "id": f"{(page - 1) * 10 + i + 1:040x}",
                        "message": str((page - 1) * 10 + i) + "x" * 700,
                    }
                    for i in range(10)
                ]
                headers = {"X-Next-Page": "2" if page == 1 else ""}
                if variant == "evil_link":
                    headers["Link"] = (
                        "<https://evil.invalid/api/v4/projects/17/merge_requests/12/commits"
                        '?page=2&per_page=10>; rel="next"'
                    )
                if variant == "moved" and page == 2:
                    router.canonical["diff_refs"] = {**refs(), "head_sha": "f" * 40}
                return reply(batch, headers=headers)
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            if variant == "full":
                value = await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)
                assert (
                    len(value.messages) == 20
                    and len(value.messages[0]) == 500
                    and value.messages[0].startswith("0x")
                    and value.messages[10].startswith("10x")
                    and value.incomplete
                )
                assert commit_calls == 2
            else:
                with pytest.raises(RunCancelled if variant == "moved" else RunFailure):
                    await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)

    asyncio.run(scenario())


def test_ordinary_blob_fault_is_optional_but_required_marker_is_preserved() -> None:
    async def scenario() -> None:
        router = Router()

        def external(request: httpx.Request) -> httpx.Response:
            if "/repository/blobs/" in request.url.path:
                return reply(status=500, raw=b"HTML-secret-canary")
            return router.response(request)

        credentials = Credentials()
        async with GitLabRunHttp(
            credentials=credentials, origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            result = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                INPUT.locator, INPUT.head_sha, INPUT.base_sha
            )
            assert result.omissions[0].reason is OmissionReason.MISSING_PATCH
            primary = RequiredSourceUnavailable("diff_fetch_failed")
            credentials.failure = primary
            with pytest.raises(RequiredSourceUnavailable) as error:
                await vcs.get_blob(INPUT.locator, "e" * 40, checkpoint=checkpoint)
            assert error.value is primary

    asyncio.run(scenario())


def test_target_merge_base_provenance_and_lower_absolute_byte_ceiling() -> None:
    async def scenario() -> None:
        router = Router()
        async with GitLabRunHttp(
            credentials=Credentials(),
            origin_policy=POLICY,
            transport=httpx.MockTransport(router.response),
        ) as http:
            await http.get_canonical_merge_request()
            await http.head_file("src/new.py", "c" * 40, side="target")
            assert await http.get_blob("d" * 40, max_bytes=3) == b"new"
            assert "/projects/17/repository/files/" in str(router.requests[-2].url)
            assert "/projects/17/repository/blobs/" in str(router.requests[-1].url)
        # A lower object ceiling is retained across failures, without resetting shared bytes.
        stream = Stream((b"xxxx",))
        calls = 0

        def bounded(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, stream=stream)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(bounded)
        ) as http:
            with pytest.raises(GitLabRunHttpError):
                await http.get_blob("d" * 40, max_bytes=10, max_total_bytes=3)
            with pytest.raises(GitLabRunHttpError):
                await http.get_blob("d" * 40, max_bytes=10, max_total_bytes=3)
            assert calls == 1 and stream.closed

    asyncio.run(scenario())


@pytest.mark.parametrize("method", ["diff", "commits"])
def test_public_worker_retries_real_gitlab_rate_fault_then_dead_letters(method: str) -> None:
    from app.modules.reviews.application.handle_review_run import ClaimedAttempt, DeliveryOutcome
    from tests.test_review_run_worker import RUN, Clock, FakeRun, RetryQueue, handler

    run = FakeRun()
    retry = RetryQueue()
    requests: list[httpx.Request] = []

    async def pipeline(claimed: ClaimedAttempt) -> bool:
        router = Router()

        def external(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if method == "diff" or request.url.path.endswith("/commits"):
                return reply(status=429, headers={"Retry-After": "300"})
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            vcs = HttpGitLabVcsProvider(http=http)
            if method == "diff":
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    INPUT.locator, INPUT.head_sha, INPUT.base_sha
                )
            else:
                await vcs.get_commit_messages(INPUT.locator, checkpoint=checkpoint)
        return True

    outcomes = []
    for _ in range(3):
        outcomes.append(
            asyncio.run(
                handler(run, pipeline, retry=retry, clock=Clock(run.available_at)).execute(RUN)
            )
        )
    assert outcomes == [DeliveryOutcome.ACK, DeliveryOutcome.ACK, DeliveryOutcome.DEAD_LETTER]
    assert retry.published == [("10m", 2), ("10m", 3)]
    code = "diff_fetch_failed" if method == "diff" else "commit_messages_fetch_failed"
    assert (run.state, run.attempt, run.error_code) == ("failed", 3, code)
    assert run.error_message == "GitLab review source is unavailable"
    assert (
        len(
            [
                request
                for request in requests
                if method == "diff" or request.url.path.endswith("/commits")
            ]
        )
        == 3
    )


@pytest.mark.parametrize(
    "fault",
    [
        "foreign",
        "malformed",
        "query",
        "conflict",
        "repeated",
        "terminal_conflict",
        "missing_link_next",
    ],
)
def test_matching_version_page_validates_all_supplied_pagination_before_detail(fault: str) -> None:
    async def scenario() -> None:
        router = Router()
        calls: list[str] = []

        def external(request: httpx.Request) -> httpx.Response:
            calls.append(request.url.path)
            if request.url.path.endswith("/versions"):
                link = (
                    "<https://gitlab.com/api/v4/projects/17/merge_requests/12/versions"
                    '?page=2&per_page=100>; rel="next"'
                )
                headers = {"Link": link, "X-Next-Page": "2"}
                if fault == "foreign":
                    headers["Link"] = link.replace("gitlab.com", "foreign.invalid")
                elif fault == "malformed":
                    headers["Link"] = "malformed"
                elif fault == "query":
                    headers["Link"] = link.replace("page=2&", "page&")
                elif fault == "conflict":
                    headers["X-Next-Page"] = "3"
                elif fault == "repeated":
                    headers["X-Next-Page"] = "1"
                elif fault == "terminal_conflict":
                    headers["X-Next-Page"] = ""
                else:
                    headers["Link"] = link.replace('rel="next"', 'rel="last"')
                return reply([{"id": 9, **version_refs()}], headers=headers)
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            with pytest.raises(RunFailure) as error:
                await HttpGitLabVcsProvider(http=http).get_pull_request(
                    INPUT.locator, checkpoint=checkpoint
                )
            assert error.value.error_code == "diff_fetch_failed"
        assert not any(path.endswith("/versions/9") for path in calls)

    asyncio.run(scenario())


@pytest.mark.parametrize("positive", ["full_without_headers", "page_five_next_six"])
def test_positive_version_does_not_require_terminal_history_after_valid_matching_page(
    positive: str,
) -> None:
    async def scenario() -> None:
        router = Router()
        pages: list[int] = []

        def external(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/versions"):
                page = int(request.url.params["page"])
                pages.append(page)
                if positive == "full_without_headers":
                    rows = [
                        {"id": number + 100, **version_refs(), "start_commit_sha": "f" * 40}
                        for number in range(99)
                    ]
                    return reply([*rows, {"id": 9, **version_refs()}])
                if page == 5:
                    return reply([{"id": 9, **version_refs()}], headers={"X-Next-Page": "6"})
                return reply(
                    [{"id": page + 100, **version_refs(), "start_commit_sha": "f" * 40}],
                    headers={"X-Next-Page": str(page + 1)},
                )
            return router.response(request)

        async with GitLabRunHttp(
            credentials=Credentials(), origin_policy=POLICY, transport=httpx.MockTransport(external)
        ) as http:
            result = await HttpGitLabVcsProvider(http=http).get_pull_request(
                INPUT.locator, checkpoint=checkpoint
            )
            assert result.meta.files_changed == 2
        assert pages == ([1] if positive == "full_without_headers" else [1, 2, 3, 4, 5])

    asyncio.run(scenario())
