"""Bitbucket VCS reads remain pinned, bounded and bound to immutable repo UUIDs."""

from __future__ import annotations

import asyncio
from uuid import UUID

import httpx
import pytest

from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestCredential,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.run_http import BitbucketRunHttp
from app.modules.reviews.application.diff_map import VcsProviderName, vcs_provider_name
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import FetchVcsReviewInput, PullRequestLocator
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


def _pr(*, head: str = HEAD, source: UUID = DEST) -> dict[str, object]:
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
            "commit": {"hash": BASE},
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


def test_pinned_fork_diff_merge_base_and_blob_use_uuid_origin() -> None:
    seen: list[httpx.Request] = []

    def respond(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.url.host == "api.bitbucket.org"
        assert request.headers["Authorization"] == "Bearer secret-token"
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr(source=SOURCE))
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
        checkpoints = 0

        async def checkpoint() -> None:
            nonlocal checkpoints
            checkpoints += 1

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
        assert checkpoints >= len(seen) * 2
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

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal pr_reads
        if request.url.path == f"{PREFIX}/pullrequests/7":
            pr_reads += 1
            return httpx.Response(200, json=_pr(head="d" * 40 if pr_reads >= 3 else HEAD))
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
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
                http=BitbucketRunHttp(client, Credentials(), TARGET), locator=_locator()
            )
            with pytest.raises(RunCancelled, match="superseded"):
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
        assert pr_reads == 3

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["truncated", "rate_limited", "invalid_retry"])
def test_diffstat_count_and_rate_limit_are_fail_closed(fault: str) -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"{PREFIX}/pullrequests/7":
            return httpx.Response(200, json=_pr())
        if request.url.path == f"{PREFIX}/diffstat/{HEAD}..{BASE}":
            if fault in {"rate_limited", "invalid_retry"}:
                return httpx.Response(
                    429,
                    headers={"Retry-After": "17" if fault == "rate_limited" else "9" * 5000},
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

    asyncio.run(scenario())


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
            with pytest.raises(RunFailure):
                await FetchVcsReviewInput(vcs, checkpoint=checkpoint).execute(
                    _locator(), HEAD, BASE
                )
        assert any(f"/{{{SOURCE}}}/commit/" in request.url.path for request in seen)
        assert all("/diff/" not in request.url.path for request in seen)

    asyncio.run(scenario())


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
            assert request.url.params["path"] == new_path
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
            assert request.url.params["path"] == "src/new.py"
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


def test_diffstat_pagination_counts_every_file_before_review() -> None:
    requested_pages: list[int] = []
    next_url = (
        "https://api.bitbucket.org/2.0/repositories/"
        f"%7B%7D/%7B{DEST}%7D/diffstat/{HEAD}..{BASE}"
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
                    "size": 101,
                    "page": page,
                    "pagelen": 100,
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
