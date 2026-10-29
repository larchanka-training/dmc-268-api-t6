"""Bitbucket VCS reads remain pinned, bounded and bound to immutable repo UUIDs."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure import run_http
from app.modules.integrations.bitbucket.infrastructure.run_http import (
    BitbucketRunHttp,
    BitbucketRunHttpError,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint, failure_reason
from app.modules.reviews.application.diff_map import VcsProviderName, vcs_provider_name
from app.modules.reviews.application.run_failures import (
    RETRYABLE_ERROR_CODES,
    RunCancelled,
    RunFailure,
    classify_failure,
)
from app.modules.reviews.application.vcs_diff import (
    FetchVcsReviewInput,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider

DEST = UUID("11111111-1111-4111-8111-111111111111")
SOURCE = UUID("22222222-2222-4222-8222-222222222222")
TARGET = BoundBitbucketTarget(UUID(int=1), UUID(int=2), UUID(int=3), DEST, 4, 5)
HEAD = "a" * 40
BASE = "b" * 40
MERGE_BASE = "c" * 40
PREFIX = f"/2.0/repositories/{{}}/{{{DEST}}}"


class Credentials:
    async def credential_for(self, target: BoundBitbucketTarget) -> BitbucketRequestCredential:
        assert target == TARGET
        return BitbucketRequestCredential(target, "secret-token")


def _locator() -> PullRequestLocator:
    return PullRequestLocator(
        11,
        "old-name/repo",
        7,
        provider="bitbucket",
        origin_provider_installation_id=TARGET.installation_id,
        repository_external_id=99,
        repository_uuid=DEST,
    )


def _pr(*, head: str = HEAD, base: str = BASE, source: UUID = DEST) -> dict[str, object]:
    return {
        "id": 7,
        "state": "OPEN",
        "title": "Fix parser",
        "description": "Description",
        "author": {"nickname": "author"},
        "source": {
            "branch": {"name": "feature"},
            "commit": {"hash": head},
            "repository": {"uuid": "{" + str(source) + "}"},
        },
        "destination": {
            "branch": {"name": "main"},
            "commit": {"hash": base},
            "repository": {"uuid": "{" + str(DEST) + "}"},
        },
    }


def _stat(*, next_url: str | None = None) -> dict[str, object]:
    page: dict[str, object] = {
        "size": 1,
        "page": 1,
        "pagelen": 100,
        "values": [
            {
                "status": "modified",
                "lines_added": 1,
                "lines_removed": 1,
                "old": {"path": "src/a.py"},
                "new": {"path": "src/a.py", "size": 8},
            }
        ],
    }
    if next_url is not None:
        page["next"] = next_url
    return page


def test_bitbucket_provider_name_and_locator_fail_closed() -> None:
    assert vcs_provider_name("bitbucket") is VcsProviderName.BITBUCKET
    with pytest.raises(ValueError, match="provider"):
        vcs_provider_name("future-provider")
    assert _locator().repository_uuid == DEST
    with pytest.raises(ValueError, match="Bitbucket"):
        PullRequestLocator(11, "old-name/repo", 7, provider="bitbucket")


def test_metadata_edits_do_not_supersede_pinned_revision() -> None:
    reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            reads += 1
            data = _pr()
            if reads == 2:
                data["title"] = "Reworded title"
                data["description"] = "Reworded description"
                data["author"] = {"display_name": "New display name"}
                source = data["source"]
                assert isinstance(source, dict)
                source["branch"] = {"name": "renamed-feature"}
            return httpx.Response(200, json=data)
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            result = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert result.head_sha == HEAD
            assert result.meta.title == "Fix parser"
            assert reads == 2

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ("head", "superseded"),
        ("target_branch", "superseded"),
        ("source_repository", "superseded"),
        ("destination_repository", "installation_unavailable"),
        ("state", "pr_closed"),
    ],
)
def test_revision_and_repository_identity_changes_cancel(change: str, reason: str) -> None:
    reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            reads += 1
            data = _pr()
            if reads == 2:
                if change == "head":
                    side = data["source"]
                    assert isinstance(side, dict)
                    side["commit"] = {"hash": "d" * 40}
                elif change == "target_branch":
                    side = data["destination"]
                    assert isinstance(side, dict)
                    side["branch"] = {"name": "release"}
                elif change in {"source_repository", "destination_repository"}:
                    side = data["source" if change == "source_repository" else "destination"]
                    assert isinstance(side, dict)
                    side["repository"] = {"uuid": "{" + str(SOURCE) + "}"}
                else:
                    data["state"] = "DECLINED"
            return httpx.Response(200, json=data)
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunCancelled, match=reason):
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)

    asyncio.run(scenario())


@pytest.mark.parametrize("initial_draft", [False, True])
def test_destination_tip_advance_keeps_pinned_diff_and_draft_toggle(
    initial_draft: bool,
) -> None:
    reads = 0
    requested_diffstat: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            reads += 1
            data = _pr(base="d" * 12)
            data["draft"] = initial_draft if reads == 1 else not initial_draft
            return httpx.Response(200, json=data)
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            requested_diffstat.append(request.url.path)
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET),
                locator=_locator(),
                pinned_base_sha=BASE,
                pinned_target_branch="main",
            )
            fetched = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert fetched.pull_request.base_sha == BASE
            assert fetched.pull_request.meta.is_draft is initial_draft
            assert len(fetched.files) == 1

    asyncio.run(scenario())
    assert requested_diffstat == [f"{PREFIX}/diffstat/{HEAD}..{BASE}"]
    assert reads >= 3


def test_retarget_before_initial_fetch_cancels_pinned_run() -> None:
    requests: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request.url.path)
        data = _pr(base="d" * 40)
        destination = data["destination"]
        assert isinstance(destination, dict)
        destination["branch"] = {"name": "release"}
        return httpx.Response(200, json=data)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET),
                locator=_locator(),
                pinned_base_sha=BASE,
                pinned_target_branch="main",
            )
            with pytest.raises(RunCancelled, match="superseded"):
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )

    asyncio.run(scenario())
    assert requests == [f"{PREFIX}/pullrequests/7"]


def test_cloud_draft_and_account_without_nickname_are_preserved() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            data = _pr()
            data["draft"] = True
            data["author"] = {"type": "account", "display_name": "Review Author"}
            return httpx.Response(200, json=data)
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            result = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert result.meta.is_draft is True
            assert result.meta.author == "Review Author"

    asyncio.run(scenario())


def test_copied_file_keeps_source_path_and_reads_new_blob() -> None:
    requested_paths: list[tuple[str, ...]] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "values": [
                        {
                            "status": "copied",
                            "lines_added": 1,
                            "lines_removed": 1,
                            "old": {"path": "src/original.py"},
                            "new": {"path": "src/copied.py", "size": 4},
                        }
                    ],
                },
            )
        if path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            requested_paths.append(tuple(request.url.params.get_list("path")))
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/original.py b/src/copied.py\n"
                    "--- a/src/original.py\n+++ b/src/copied.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if path == f"{PREFIX}/src/{HEAD}/src/copied.py":
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            fetched = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            copied = fetched.files[0]
            assert copied.status == "copied"
            assert copied.filename == "src/copied.py"
            assert copied.previous_filename == "src/original.py"
            assert copied.patch is not None
            assert copied.blob_sha is not None
            assert (
                await vcs.get_blob(_locator(), copied.blob_sha, checkpoint=checkpoint) == b"new\n"
            )

    asyncio.run(scenario())
    assert requested_paths == [("src/original.py", "src/copied.py")]


def test_abbreviated_head_rejects_unrelated_resolved_commit_before_diffstat() -> None:
    requested: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=HEAD[:12]))
        if request.url.path == f"{PREFIX}/commit/{HEAD[:12]}":
            return httpx.Response(200, json={"hash": "c" * 40})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert error.value.error_code == "bitbucket_invalid_response"

    asyncio.run(scenario())
    assert requested == [f"{PREFIX}/pullrequests/7", f"{PREFIX}/commit/{HEAD[:12]}"]


def test_total_response_budget_is_distinct_from_one_oversized_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_http, "_TOTAL_BYTES", 500)

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 300)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            assert len(await http.get_bytes(DEST, "src/a", max_bytes=400)) == 300
            with pytest.raises(BitbucketRunHttpError) as error:
                await http.get_bytes(DEST, "src/b", max_bytes=400)
            assert error.value.kind == "total_budget_exceeded"
            assert error.value.error_code == "bitbucket_response_budget_exceeded"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES

    asyncio.run(scenario())


def test_exhausted_total_budget_fails_instead_of_omitting_one_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(run_http, "_TOTAL_BYTES", 2_200_000)

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/src/padding":
            return httpx.Response(200, content=b"x" * 2_195_000)
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(200, content=b"x" * 5_000)
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=_locator())
            await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            await http.get_bytes(DEST, "src/padding", max_bytes=2_200_000)
            with pytest.raises(RunFailure) as error:
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
            assert error.value.error_code == "bitbucket_response_budget_exceeded"

    asyncio.run(scenario())


def test_exhausted_request_budget_on_required_blob_fails_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen.append(path)
        if path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(200, text="diff --git a/src/a.py b/src/a.py\n")
        raise AssertionError(path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)

            class CapAtBlob(HttpBitbucketVcsProvider):
                async def get_diff(
                    self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
                ) -> tuple[VcsFile, ...]:
                    files = await super().get_diff(pull_request, checkpoint=checkpoint)
                    # Simulate a provider file that requires blob classification.
                    return tuple(replace(file, omission_reason=None) for file in files)

                async def get_blob(
                    self,
                    locator: PullRequestLocator,
                    sha: str,
                    *,
                    checkpoint: RunProviderCheckpoint,
                ) -> bytes:
                    monkeypatch.setattr(run_http, "_MAX_REQUESTS", http.request_count)
                    return await super().get_blob(locator, sha, checkpoint=checkpoint)

            vcs = CapAtBlob(http=http, locator=_locator())
            with pytest.raises(BitbucketRunHttpError) as error:
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
            assert error.value.error_code == "bitbucket_request_budget_exceeded"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES
            assert all("/src/" not in path for path in seen)

    asyncio.run(scenario())


def test_pinned_fork_diff_merge_base_and_blob_use_uuid_origin() -> None:
    seen: list[httpx.Request] = []
    events: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        events.append("request")
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer secret-token"
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=HEAD[:12], base=BASE[:12], source=SOURCE))
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/commit/{HEAD[:12]}":
            return httpx.Response(200, json={"hash": HEAD})
        if request.url.path == f"{PREFIX}/commit/{BASE[:12]}":
            return httpx.Response(200, json={"hash": BASE})
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/commit/{HEAD}":
            return httpx.Response(200, json={"hash": HEAD})
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            assert request.url.params["topic"] == "true"
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            assert request.url.params["topic"] == "true"
            assert request.url.params["path"] == "src/a.py"
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if request.url.path == f"{PREFIX}/merge-base/{HEAD}..{BASE}":
            return httpx.Response(200, json={"hash": MERGE_BASE})
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/src/{HEAD}/src/a.py":
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(f"unexpected Bitbucket path: {request.url.path}")

    async def scenario() -> None:
        async def checkpoint() -> None:
            events.append("checkpoint")

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=_locator())
            result = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert result.pull_request.meta.is_fork
            assert result.pull_request.meta.files_changed == 1
            assert result.changed_files[0].path == "src/a.py"
            assert result.omissions == ()
            assert (
                await vcs.get_merge_base(result.pull_request, checkpoint=checkpoint) == MERGE_BASE
            )
            blob_id = result.files[0].blob_sha
            assert blob_id is not None
            assert await vcs.get_blob(_locator(), blob_id, checkpoint=checkpoint) == b"new\n"
        assert sum(event == "request" for event in events) == len(seen)
        for index, event in enumerate(events):
            if event == "request":
                assert events[index - 1 : index + 2] == [
                    "checkpoint",
                    "request",
                    "checkpoint",
                ]
        assert all("old-name" not in str(request.url) for request in seen)

    asyncio.run(scenario())


@pytest.mark.parametrize("changed_head", [False, True])
def test_persisted_blob_id_survives_fresh_adapter_and_rejects_new_head(
    changed_head: bool,
) -> None:
    """A committed DiffSnapshot can be resumed without the first VCS instance."""
    requests: list[httpx.Request] = []
    current_head = HEAD

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(head=current_head, source=SOURCE))
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/commit/{current_head}":
            return httpx.Response(200, json={"hash": current_head})
        if request.url.path == f"{PREFIX}/diffstat/{current_head}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{current_head}..{BASE}":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/src/{current_head}/src/a.py":
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        nonlocal current_head

        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            first = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            review = await FetchVcsReviewInput(first, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            persisted_id = review.files[0].blob_sha
            assert persisted_id is not None
            requests.clear()
            if changed_head:
                current_head = "d" * 40
            resumed = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            if changed_head:
                with pytest.raises(RunCancelled):
                    await resumed.get_blob(_locator(), persisted_id, checkpoint=checkpoint)
            else:
                assert (
                    await resumed.get_blob(_locator(), persisted_id, checkpoint=checkpoint)
                    == b"new\n"
                )
                assert all("/diff/" not in request.url.path for request in requests)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "fault", ["redirect", "oversized", "foreign_next", "missing_patch", "oversized_path"]
)
def test_incomplete_or_untrusted_diff_never_becomes_clean(fault: str) -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            if fault == "redirect":
                return httpx.Response(302, headers={"Location": "https://evil.example/steal"})
            if fault == "oversized":
                return httpx.Response(200, content=b"x" * (2 * 1024 * 1024 + 1))
            if fault == "foreign_next":
                return httpx.Response(
                    200,
                    json=_stat(next_url="https://evil.example/2.0/repositories/steal?page=2"),
                )
            if fault == "oversized_path":
                stat = _stat()
                values = stat["values"]
                assert isinstance(values, list)
                values[0]["old"]["path"] = "src/" + "э" * 600
                values[0]["new"]["path"] = "src/" + "э" * 600
                return httpx.Response(200, json=stat)
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(200, text="diff --git a/src/a.py b/src/a.py\n")
        raise AssertionError(f"unexpected request {request.url.path}")

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            if fault == "missing_patch":
                review = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
                assert review.changed_files == ()
                assert len(review.omissions) == 1
            else:
                with pytest.raises(RunFailure):
                    await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                        _locator(), HEAD, BASE
                    )
        assert all(request.url.host == "api.bitbucket.org" for request in seen)

    asyncio.run(scenario())


def test_head_change_during_diff_cancels_and_does_not_use_stale_patch() -> None:
    pr_reads = 0
    diff_requested = False

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pr_reads, diff_requested
        if request.url.path == f"{PREFIX}/pullrequests/7":
            pr_reads += 1
            return httpx.Response(200, json=_pr(head="d" * 40 if diff_requested else HEAD))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            diff_requested = True
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunCancelled, match="superseded"):
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
        assert diff_requested
        assert pr_reads >= 2

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["truncated", "rate_limited", "invalid_retry", "missing_retry"])
def test_diffstat_count_and_rate_limit_are_fail_closed(fault: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            if fault in {"rate_limited", "invalid_retry", "missing_retry"}:
                return httpx.Response(
                    429,
                    headers=(
                        {"Retry-After": "17" if fault == "rate_limited" else "9" * 5000}
                        if fault != "missing_retry"
                        else {}
                    ),
                )
            page = _stat()
            page["size"] = 2
            return httpx.Response(200, json=page)
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            if fault == "rate_limited":
                assert error.value.retry_after is not None
                assert error.value.retry_after.total_seconds() == 17
            elif fault in {"invalid_retry", "missing_retry"}:
                assert error.value.rate_limited
                assert error.value.retry_after == timedelta(hours=1)

    asyncio.run(scenario())


def test_merge_base_429_preserves_retry_after() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/merge-base/{HEAD}..{BASE}":
            return httpx.Response(429, headers={"Retry-After": "4200"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            pr = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            with pytest.raises(BitbucketRunHttpError) as error:
                await vcs.get_merge_base(pr, checkpoint=checkpoint)
            assert error.value.kind == "rate_limited"
            assert error.value.retry_after is not None
            assert error.value.retry_after.total_seconds() == 4200

    asyncio.run(scenario())


def test_merge_base_request_budget_preserves_bitbucket_budget_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=_locator())
            pr = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            # The cached PR guard succeeds, then the required merge-base read reaches the cap.
            monkeypatch.setattr(run_http, "_MAX_REQUESTS", http.request_count)
            with pytest.raises(BitbucketRunHttpError) as error:
                await vcs.get_merge_base(pr, checkpoint=checkpoint)
            assert error.value.kind == "request_budget_exceeded"
            assert error.value.error_code == "bitbucket_request_budget_exceeded"

    asyncio.run(scenario())


def test_run_request_cap_fails_before_an_extra_http_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_http, "_MAX_REQUESTS", 1)
    calls = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"x")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            assert await http.get_bytes(DEST, f"src/{HEAD}/first", max_bytes=1) == b"x"
            with pytest.raises(RunFailure) as error:
                await http.get_bytes(DEST, f"src/{HEAD}/second", max_bytes=1)
            assert error.value.error_code == "bitbucket_request_budget_exceeded"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES
            assert failure_reason(error.value.error_code) == (
                "Лимит запросов к Bitbucket для ревью исчерпан."
            )

    asyncio.run(scenario())
    assert calls == 1


def test_bitbucket_budget_code_survives_external_failure_classification() -> None:
    class ProviderBudgetError(Exception):
        error_code = "bitbucket_request_budget_exceeded"

    assert classify_failure(ProviderBudgetError()).error_code == (
        "bitbucket_request_budget_exceeded"
    )


def test_run_http_failure_reports_bitbucket_source_instead_of_github_diff() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: httpx.Response(503))
        ) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            with pytest.raises(BitbucketRunHttpError) as error:
                await http.get_json(DEST, "pullrequests/7")
            assert error.value.kind == "unexpected_status"
            assert error.value.error_code == "bitbucket_fetch_failed"
            assert error.value.error_code in RETRYABLE_ERROR_CODES
            assert failure_reason(error.value.error_code) == (
                "Не удалось получить данные ревью из Bitbucket."
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("malformed_kind", ["pr", "commit", "diffstat", "json"])
def test_malformed_bitbucket_response_is_terminal(malformed_kind: str) -> None:
    malformed = _pr(head=HEAD[:12] if malformed_kind == "commit" else HEAD)
    if malformed_kind == "pr":
        malformed["author"] = {}

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            if malformed_kind == "json":
                return httpx.Response(200, content=b"not-json")
            return httpx.Response(200, json=malformed)
        if request.url.path == f"{PREFIX}/commit/{HEAD[:12]}":
            return httpx.Response(200, json={"hash": "invalid"})
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"values": "invalid"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert error.value.error_code == "bitbucket_invalid_response"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES
            assert failure_reason(error.value.error_code) == (
                "Bitbucket вернул некорректные данные для ревью."
            )

    asyncio.run(scenario())


@pytest.mark.parametrize("file_count", [100, 200])
def test_changed_blob_reads_have_bounded_pr_rechecks(file_count: int) -> None:
    pr_reads = 0

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pr_reads
        path = request.url.path
        if path == f"{PREFIX}/pullrequests/7":
            pr_reads += 1
            return httpx.Response(200, json=_pr())
        if path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            page = int(request.url.params["page"])
            start = (page - 1) * 100
            values = [
                {
                    "status": "modified",
                    "lines_added": 1,
                    "lines_removed": 1,
                    "old": {"path": f"f{index:03}.py"},
                    "new": {"path": f"f{index:03}.py", "size": 4},
                }
                for index in range(start, min(start + 100, file_count))
            ]
            data: dict[str, object] = {"size": file_count, "values": values}
            if page == 1 and file_count == 200:
                data["next"] = (
                    f"https://api.bitbucket.org{PREFIX}/diffstat/{HEAD}..{BASE}"
                    "?topic=true&pagelen=100&page=2"
                )
            return httpx.Response(200, json=data)
        if path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            filename = request.url.params["path"]
            return httpx.Response(
                200,
                text=(
                    f"diff --git a/{filename} b/{filename}\n"
                    f"--- a/{filename}\n+++ b/{filename}\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if f"{PREFIX}/src/{HEAD}/f" in path:
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            http = BitbucketRunHttp(client, Credentials(), TARGET)
            vcs = HttpBitbucketVcsProvider(http=http, locator=_locator())
            pr = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            files = await vcs.get_diff(pr, checkpoint=checkpoint)
            assert len(files) == file_count
            for file in files:
                assert file.blob_sha is not None
                assert (
                    await vcs.get_blob(_locator(), file.blob_sha, checkpoint=checkpoint) == b"new\n"
                )
            await vcs.verify_current(checkpoint=checkpoint)
            assert 2 * file_count <= http.request_count <= 2 * file_count + 6

    asyncio.run(scenario())
    assert 2 <= pr_reads <= 4


def test_fork_blob_redirect_is_not_followed_with_token() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=SOURCE))
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/commit/{HEAD}":
            return httpx.Response(200, json={"hash": HEAD})
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(200, text="diff --git a/src/a.py b/src/a.py\n")
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/src/{HEAD}/src/a.py":
            return httpx.Response(301, headers={"Location": "https://media.example/private"})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            review = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert review.changed_files == ()
            assert len(review.omissions) == 1
            blob_id = review.files[0].blob_sha
            assert blob_id is not None
            with pytest.raises(RunFailure):
                await vcs.get_blob(_locator(), blob_id, checkpoint=checkpoint)
        assert all(request.url.host == "api.bitbucket.org" for request in seen)
        assert any(request.url.path.endswith("/src/a.py") for request in seen)

    asyncio.run(scenario())


def test_inaccessible_private_fork_cannot_produce_a_clean_review() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=SOURCE))
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/commit/{HEAD}":
            return httpx.Response(403)
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json=_stat())
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/a.py b/src/a.py\n"
                    "--- a/src/a.py\n+++ b/src/a.py\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
            assert error.value.error_code == "bitbucket_source_unavailable"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES
        assert any(f"/{{{SOURCE}}}/commit/" in request.url.path for request in seen)
        assert all("/diff/" not in request.url.path for request in seen)

    asyncio.run(scenario())


def test_diffstat_page_limit_is_terminal() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            next_page = int(request.url.params["page"]) + 1
            next_url = (
                "https://api.bitbucket.org/2.0/repositories/"
                f"%7B%7D/%7B{DEST}%7D/diffstat/{HEAD}..{BASE}"
                f"?topic=true&pagelen=100&page={next_page}"
            )
            return httpx.Response(200, json={"values": [], "next": next_url})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert error.value.error_code == "bitbucket_review_limit_exceeded"
            assert error.value.error_code not in RETRYABLE_ERROR_CODES

    asyncio.run(scenario())


def test_declared_201_file_pr_fails_before_patch_reads() -> None:
    paths: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(200, json={"size": 201, "values": []})
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunFailure) as error:
                await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert error.value.error_code == "bitbucket_review_limit_exceeded"

    asyncio.run(scenario())
    assert paths == [f"{PREFIX}/pullrequests/7", f"{PREFIX}/diffstat/{HEAD}..{BASE}"]


@pytest.mark.parametrize("quoted", [False, True])
def test_space_and_unicode_patch_markers_keep_path_identity(quoted: bool) -> None:
    path = "src/naïve module.py"
    left = '"a/src/na\\303\\257ve module.py"' if quoted else f"a/{path}"
    right = '"b/src/na\\303\\257ve module.py"' if quoted else f"b/{path}"

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
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
                            "old": {"path": path},
                            "new": {"path": path},
                        }
                    ],
                },
            )
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            assert request.url.params["path"] == path
            return httpx.Response(
                200,
                text=(
                    f"diff --git {left} {right}\n"
                    f"--- {left}\t\n+++ {right}\t\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            review = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert len(review.changed_files) == 1
            assert review.omissions == ()

    asyncio.run(scenario())


@pytest.mark.parametrize("quoted_old", [False, True])
def test_mixed_quoted_rename_header_keeps_both_path_identities(quoted_old: bool) -> None:
    old_path = "src/é.py" if quoted_old else "src/ascii.py"
    new_path = "src/ascii.py" if quoted_old else "src/é.py"
    left = '"a/src/\\303\\251.py"' if quoted_old else f"a/{old_path}"
    right = f"b/{new_path}" if quoted_old else '"b/src/\\303\\251.py"'

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        {
                            "status": "renamed",
                            "lines_added": 1,
                            "lines_removed": 1,
                            "old": {"path": old_path},
                            "new": {"path": new_path},
                        }
                    ],
                },
            )
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            assert request.url.params.get_list("path") == [old_path, new_path]
            return httpx.Response(
                200,
                text=(
                    f"diff --git {left} {right}\n"
                    f"--- {left}\t\n+++ {right}\t\n"
                    "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            review = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert len(review.changed_files) == 1
            assert review.files[0].previous_filename == old_path
            assert review.omissions == ()

    asyncio.run(scenario())


def test_rename_uses_both_paths_and_rejects_mismatched_patch_identity() -> None:
    changed_marker = [False]

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            return httpx.Response(
                200,
                json={
                    "size": 1,
                    "page": 1,
                    "pagelen": 100,
                    "values": [
                        {
                            "status": "renamed",
                            "lines_added": 1,
                            "lines_removed": 1,
                            "old": {"path": "src/old.py"},
                            "new": {"path": "src/new.py"},
                        }
                    ],
                },
            )
        if request.url.path == f"{PREFIX}/diff/{HEAD}..{BASE}":
            assert request.url.params.get_list("path") == ["src/old.py", "src/new.py"]
            return httpx.Response(
                200,
                text=(
                    "diff --git a/src/old.py b/src/new.py\n"
                    "--- a/src/old.py\n"
                    + ("+++ b/src/wrong.py\n" if changed_marker[0] else "+++ b/src/new.py\n")
                    + "@@ -1 +1 @@\n-old\n+new\n"
                ),
            )
        if request.url.path == f"/2.0/repositories/{{}}/{{{SOURCE}}}/src/{HEAD}/src/new.py":
            return httpx.Response(200, content=b"new\n")
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            review = await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert review.files[0].previous_filename == "src/old.py"
            assert review.changed_files[0].path == "src/new.py"
            changed_marker[0] = True
            another = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            incomplete = await FetchVcsReviewInput(another, checkpoint=checkpoint).execute(
                _locator(), HEAD, BASE
            )
            assert incomplete.changed_files == ()
            assert len(incomplete.omissions) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("encoded", [False, True])
def test_diffstat_pagination_counts_every_file_before_review(encoded: bool) -> None:
    requested_pages: list[int] = []
    repo_path = f"%7B%7D/%7B{DEST}%7D" if encoded else f"{{}}/{{{DEST}}}"
    next_url = (
        "https://api.bitbucket.org/2.0/repositories/"
        f"{repo_path}/diffstat/{HEAD}..{BASE}"
        "?topic=true&pagelen=100&page=2"
    )

    def entry(number: int) -> dict[str, object]:
        path = f"src/{number}.py"
        return {
            "status": "added",
            "lines_added": 1,
            "lines_removed": 0,
            "old": None,
            "new": {"path": path},
        }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=DEST))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            page = int(request.url.params["page"])
            requested_pages.append(page)
            return httpx.Response(
                200,
                json={
                    "values": [entry(number) for number in range(100)]
                    if page == 1
                    else [entry(100)],
                    **({"next": next_url} if page == 1 else {}),
                },
            )
        raise AssertionError(request.url.path)

    async def scenario() -> None:
        async def checkpoint() -> None:
            return None

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vcs = HttpBitbucketVcsProvider(
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            result = await vcs.get_pull_request(_locator(), checkpoint=checkpoint)
            assert result.meta.files_changed == 101
            assert result.meta.lines_added == 101
            assert requested_pages == [1, 2]

    asyncio.run(scenario())
