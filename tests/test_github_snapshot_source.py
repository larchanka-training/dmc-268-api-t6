"""GitHub snapshot source of the indexer: HEAD, compare, tarball and the error mapping."""

from __future__ import annotations

import asyncio
import logging
import tracemalloc
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

import httpx
import pytest

from app.common.infrastructure.github_repository_path import InvalidGitHubPathSegment
from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveUnsafe,
    BranchHead,
    ChangedPath,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    RepositorySnapshotSource,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.infrastructure.github_snapshot_source import (
    GitHubRepositorySnapshotSource,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import DocumentPresence

_BASE = "b" * 40
_HEAD = "c" * 40
_CODELOAD = f"https://codeload.github.com/octo/repo/legacy.tar.gz/{_HEAD}?token=temporary"
_MAX = 1_000


@dataclass
class Tokens:
    error: Exception | None = None
    calls: int = 0

    async def get_installation_access_token(self, installation_external_id: int) -> str:
        assert installation_external_id == 7
        self.calls += 1
        if self.error is not None:
            raise self.error
        return "token"


@dataclass
class GitHub:
    """A mock GitHub: one handler per path; every request it saw, in order."""

    routes: dict[str, Callable[[httpx.Request], httpx.Response]]
    seen: list[httpx.Request] = field(default_factory=list)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        key = f"{request.url.host}{request.url.raw_path.decode().split('?')[0]}"
        return self.routes[key](request)


def _run(
    github: GitHub,
    action: Callable[[GitHubRepositorySnapshotSource], Awaitable[Any]],
    *,
    tokens: Tokens | None = None,
    auth: httpx.Auth | None = None,
) -> tuple[Any, int]:
    """Run `action`; return its result (or the raised error) and the request count."""

    async def exercise() -> tuple[Any, int]:
        # The client follows redirects and sends credentials on its own: the adapter must
        # not rely on its defaults.
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(github),
            base_url="https://api.github.com",
            follow_redirects=True,
            headers={"Authorization": "Bearer client-default"},
            auth=auth,
        ) as client:
            source = GitHubRepositorySnapshotSource(
                client=client,
                token_provider=tokens or Tokens(),
                installation_external_id=7,
                repository_full_name="octo/repo",
            )
            port: RepositorySnapshotSource = source
            try:
                result: Any = await action(source)
            except Exception as error:
                result = error
            return result, port.request_count

    return asyncio.run(exercise())


def _repository(branch: object = "main") -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, json={"full_name": "octo/repo", "default_branch": branch}
    )


def _sha(sha: str = _HEAD) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(
        200, text=sha, headers={"Content-Type": "application/vnd.github.sha"}
    )


def _answer(status: int, **kwargs: Any) -> Callable[[httpx.Request], httpx.Response]:
    return lambda request: httpx.Response(status, **kwargs)


# --- head ---------------------------------------------------------------------------------


def test_head_reads_the_default_branch_and_its_commit_sha() -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository(),
            "api.github.com/repos/octo/repo/commits/main": _sha(),
        }
    )

    head, requests = _run(github, lambda source: source.head())

    assert head == BranchHead(branch="main", commit_sha=_HEAD)
    assert requests == 2
    assert [request.headers["Authorization"] for request in github.seen] == ["Bearer token"] * 2
    assert github.seen[1].headers["Accept"] == "application/vnd.github.sha"
    assert github.seen[1].headers["X-GitHub-Api-Version"] == "2022-11-28"


def test_head_puts_a_branch_with_a_slash_into_one_path_segment() -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository("release/1.x"),
            "api.github.com/repos/octo/repo/commits/release%2F1.x": _sha(),
        }
    )

    head, requests = _run(github, lambda source: source.head())

    assert head == BranchHead(branch="release/1.x", commit_sha=_HEAD)
    assert requests == 2


@pytest.mark.parametrize(
    "answer",
    [
        _answer(409, json={"message": "Git Repository is empty."}),
        _answer(404, json={"message": "Not Found"}),
        _answer(422, json={"message": "No commit found for SHA: main"}),
    ],
    ids=["409-empty-repository", "404-branch", "422-branch"],
)
def test_head_is_none_for_an_empty_repository_or_a_missing_branch(
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository(),
            "api.github.com/repos/octo/repo/commits/main": answer,
        }
    )

    head, requests = _run(github, lambda source: source.head())

    assert head is None
    assert requests == 2


@pytest.mark.parametrize("branch", [None, ""])
def test_head_is_none_without_a_default_branch(branch: object) -> None:
    github = GitHub({"api.github.com/repos/octo/repo": _repository(branch)})

    head, requests = _run(github, lambda source: source.head())

    assert head is None
    assert requests == 1


def test_head_another_409_is_not_an_empty_repository() -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository(),
            "api.github.com/repos/octo/repo/commits/main": _answer(
                409, json={"message": "Conflict"}
            ),
        }
    )

    error, requests = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.code == "vcs_unavailable"
    assert requests == 2


def test_head_a_lost_repository_is_access_denied() -> None:
    github = GitHub({"api.github.com/repos/octo/repo": _answer(404, json={"message": "x"})})

    error, requests = _run(github, lambda source: source.head())

    assert isinstance(error, VcsAccessDenied)
    assert error.code == "vcs_access_denied"
    assert requests == 1


@pytest.mark.parametrize("body", ["not-a-sha", "c" * 39, ""])
def test_head_an_unreadable_commit_sha_is_unavailable(body: str) -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository(),
            "api.github.com/repos/octo/repo/commits/main": _sha(body),
        }
    )

    error, _ = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.code == "vcs_unavailable"


def test_head_an_unreadable_repository_answer_is_unavailable() -> None:
    github = GitHub({"api.github.com/repos/octo/repo": _answer(200, text="<html>")})

    error, _ = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)


# --- error mapping (PIPELINE_SPEC §5.2) ---------------------------------------------------


@pytest.mark.parametrize(
    ("status", "headers", "code", "retry_after"),
    [
        (403, {"Retry-After": "30"}, "vcs_rate_limited", timedelta(seconds=30)),
        (403, {"X-RateLimit-Remaining": "0"}, "vcs_rate_limited", None),
        (429, {}, "vcs_rate_limited", None),
        (429, {"Retry-After": "120"}, "vcs_rate_limited", timedelta(seconds=120)),
        (500, {}, "vcs_unavailable", None),
        (503, {"Retry-After": "5"}, "vcs_unavailable", timedelta(seconds=5)),
        (400, {}, "vcs_unavailable", None),
        # As in `reviews/infrastructure/vcs_failures.py`: a fresh installation token can
        # heal a 401.
        (401, {}, "vcs_unavailable", None),
    ],
)
def test_transient_answers_are_unavailable(
    status: int, headers: dict[str, str], code: str, retry_after: timedelta | None
) -> None:
    github = GitHub({"api.github.com/repos/octo/repo": _answer(status, headers=headers)})

    error, requests = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.code == code
    assert error.rate_limited is (code == "vcs_rate_limited")
    assert error.retry_after == retry_after
    assert requests == 1


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.parametrize(
    "retry_after",
    ["\u00b2", "-5", "1" * 4301, "soon", "Wed, 21 Oct 2026 07:28:00 GMT"],
    # `int()` refuses a string of more than 4300 digits with `ValueError`.
    ids=["superscript-two", "negative", "over-4300-digits", "word", "http-date"],
)
def test_an_unreadable_retry_after_is_no_delay(status: int, retry_after: str) -> None:
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _answer(
                status, headers=[(b"Retry-After", retry_after.encode())]
            )
        }
    )

    error, _ = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.rate_limited is (status == 429)
    assert error.retry_after is None


@pytest.mark.parametrize(
    ("retry_after", "seconds"),
    [("86400", 86_400), ("86401", 86_400), ("10000000000000", 86_400), ("9" * 30, 86_400)],
    ids=["one-day", "one-day-and-a-second", "ten-to-the-13", "30-digits"],
)
def test_a_retry_after_over_a_day_waits_one_day(retry_after: str, seconds: int) -> None:
    github = GitHub(
        {"api.github.com/repos/octo/repo": _answer(503, headers={"Retry-After": retry_after})}
    )

    error, _ = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.retry_after == timedelta(seconds=seconds)


@pytest.mark.parametrize("status", [403, 404])
def test_access_answers_are_access_denied(status: int) -> None:
    github = GitHub({"api.github.com/repos/octo/repo": _answer(status)})

    error, requests = _run(github, lambda source: source.head())

    assert isinstance(error, VcsAccessDenied)
    assert requests == 1


def test_a_timeout_is_unavailable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    github = GitHub({"api.github.com/repos/octo/repo": timeout})

    error, requests = _run(github, lambda source: source.head())

    assert isinstance(error, VcsUnavailable)
    assert error.code == "vcs_unavailable"
    assert requests == 1


@pytest.mark.parametrize(
    ("status", "error_type"),
    [(404, VcsAccessDenied), (502, VcsUnavailable), (401, VcsUnavailable)],
)
def test_a_failed_token_exchange_is_mapped_and_not_counted(
    status: int, error_type: type[Exception]
) -> None:
    request = httpx.Request("POST", "https://api.github.com/app/installations/7/access_tokens")
    tokens = Tokens(
        error=httpx.HTTPStatusError(
            "token", request=request, response=httpx.Response(status, request=request)
        )
    )
    github = GitHub({})

    error, requests = _run(github, lambda source: source.head(), tokens=tokens)

    assert isinstance(error, error_type)
    assert requests == 0
    assert github.seen == []


# --- compare ------------------------------------------------------------------------------

_COMPARE = f"api.github.com/repos/octo/repo/compare/{_BASE}...{_HEAD}"


def _entry(name: str, status: str = "modified", previous: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {
        "sha": "d" * 40,
        "filename": name,
        "status": status,
        "additions": 1,
        "deletions": 0,
        "changes": 1,
        "blob_url": "https://github.com/x",
        "raw_url": "https://github.com/x",
        "contents_url": "https://api.github.com/x",
    }
    if previous is not None:
        item["previous_filename"] = previous
    return item


def _comparison(status: str = "ahead", files: list[dict[str, Any]] | None = None) -> Any:
    body: dict[str, Any] = {"status": status, "ahead_by": 2, "behind_by": 0, "commits": []}
    if files is not None:
        body["files"] = files
    return _answer(200, json=body)


def test_compare_maps_the_change_set() -> None:
    github = GitHub(
        {
            _COMPARE: _comparison(
                files=[
                    _entry("src/new.py", "added"),
                    _entry("src/app.py", "modified"),
                    _entry("src/old.py", "removed"),
                    _entry("src/moved.py", "renamed", "src/before.py"),
                    _entry("src/copy.py", "copied", "src/app.py"),
                    _entry("bin/run", "changed"),
                    _entry("docs/a.md", "unchanged"),
                ]
            )
        }
    )

    comparison, requests = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison == Comparison(
        ancestry=Ancestry.AHEAD,
        files=(
            ChangedPath("src/new.py", ChangeStatus.ADDED),
            ChangedPath("src/app.py", ChangeStatus.MODIFIED),
            ChangedPath("src/old.py", ChangeStatus.REMOVED),
            ChangedPath("src/moved.py", ChangeStatus.RENAMED, "src/before.py"),
            ChangedPath("src/copy.py", ChangeStatus.COPIED, "src/app.py"),
            ChangedPath("bin/run", ChangeStatus.CHANGED),
            ChangedPath("docs/a.md", ChangeStatus.UNCHANGED),
        ),
        complete=True,
    )
    assert requests == 1
    # The changed files come with the first page; one commit per page keeps it small.
    assert github.seen[0].url.params["per_page"] == "1"
    assert github.seen[0].headers["Authorization"] == "Bearer token"


@pytest.mark.parametrize(
    ("status", "ancestry"),
    [
        ("ahead", Ancestry.AHEAD),
        ("identical", Ancestry.IDENTICAL),
        ("behind", Ancestry.BEHIND),
        ("diverged", Ancestry.DIVERGED),
    ],
)
def test_compare_maps_the_ancestry(status: str, ancestry: Ancestry) -> None:
    github = GitHub({_COMPARE: _comparison(status, files=[])})

    comparison, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison == Comparison(ancestry=ancestry, files=(), complete=True)


@pytest.mark.parametrize(("count", "complete"), [(299, True), (300, False)])
def test_compare_with_300_files_or_more_is_incomplete(count: int, complete: bool) -> None:
    github = GitHub({_COMPARE: _comparison(files=[_entry(f"f{n}.py") for n in range(count)])})

    comparison, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison.complete is complete
    assert len(comparison.files) == count


@pytest.mark.parametrize(("status", "complete"), [("ahead", False), ("identical", True)])
def test_compare_without_a_file_list(status: str, complete: bool) -> None:
    github = GitHub({_COMPARE: _comparison(status)})

    comparison, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison.files == ()
    assert comparison.complete is complete


def test_compare_an_unknown_base_is_none() -> None:
    github = GitHub({_COMPARE: _answer(404, json={"message": "Not Found"})})

    comparison, requests = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison is None
    assert requests == 1


def test_compare_maps_other_failures() -> None:
    github = GitHub({_COMPARE: _answer(429, headers={"Retry-After": "60"})})

    error, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert isinstance(error, VcsUnavailable)
    assert error.code == "vcs_rate_limited"
    assert error.retry_after == timedelta(seconds=60)


def test_compare_declared_over_8_mib_is_incomplete_without_reading() -> None:
    github = GitHub({_COMPARE: _answer(200, headers={"Content-Length": str(8 * 1024 * 1024 + 1)})})

    comparison, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison == Comparison(ancestry=None, files=(), complete=False)


def test_compare_streamed_over_8_mib_is_incomplete() -> None:
    async def body() -> AsyncIterator[bytes]:
        yield b'{"status": "ahead", "files": [], "pad": "'
        for _ in range(8):
            yield b"x" * (1024 * 1024)
        yield b'"}'

    github = GitHub({_COMPARE: _answer(200, content=body())})

    comparison, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert comparison == Comparison(ancestry=None, files=(), complete=False)


@pytest.mark.parametrize(
    "answer",
    [
        _comparison(files=[_entry("src/moved.py", "renamed")]),
        _comparison("sideways", files=[]),
        _answer(200, text="not json"),
    ],
    ids=["rename-without-previous", "unknown-status", "not-json"],
)
def test_compare_an_unreadable_answer_is_unavailable(
    answer: Callable[[httpx.Request], httpx.Response],
) -> None:
    github = GitHub({_COMPARE: answer})

    error, _ = _run(github, lambda source: source.compare(_BASE, _HEAD))

    assert isinstance(error, VcsUnavailable)


def test_compare_refuses_a_sha_of_another_shape_before_any_request() -> None:
    github = GitHub({})

    error, requests = _run(github, lambda source: source.compare("../x", _HEAD))

    assert isinstance(error, InvalidGitHubPathSegment)
    assert requests == 0


# --- archive ------------------------------------------------------------------------------

_TARBALL = f"api.github.com/repos/octo/repo/tarball/{_HEAD}"
_CODELOAD_KEY = f"codeload.github.com/octo/repo/legacy.tar.gz/{_HEAD}"


def _redirect(location: str | None = _CODELOAD) -> Callable[[httpx.Request], httpx.Response]:
    headers = {} if location is None else {"Location": location}
    return _answer(302, headers=headers)


def test_archive_follows_the_redirect_to_codeload_without_auth() -> None:
    github = GitHub(
        {
            _TARBALL: _redirect(),
            _CODELOAD_KEY: _answer(
                200, content=b"\x1f\x8bgzip", headers={"Content-Type": "application/x-gzip"}
            ),
        }
    )

    archive, requests = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert archive == SourceArchive(commit_sha=_HEAD, content=b"\x1f\x8bgzip")
    assert requests == 2
    tarball, download = github.seen
    assert tarball.headers["Authorization"] == "Bearer token"
    assert "Authorization" not in download.headers
    assert str(download.url) == _CODELOAD
    assert download.headers["Accept-Encoding"] == "identity"


def test_archive_download_ignores_the_auth_of_the_client() -> None:
    github = GitHub({_TARBALL: _redirect(), _CODELOAD_KEY: _answer(200, content=b"\x1f\x8b")})

    archive, _ = _run(
        github,
        lambda source: source.archive(_HEAD, max_bytes=_MAX),
        auth=httpx.BasicAuth("client-user", "client-pass"),
    )

    assert archive == SourceArchive(commit_sha=_HEAD, content=b"\x1f\x8b")
    download = github.seen[-1]
    assert download.url.host == "codeload.github.com"
    assert "Authorization" not in download.headers


def _httpx_lines(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "httpx"]


def test_the_httpx_log_line_of_the_download_has_no_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    github = GitHub({_TARBALL: _redirect(), _CODELOAD_KEY: _answer(200, content=b"\x1f\x8b")})

    archive, _ = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert isinstance(archive, SourceArchive)
    lines = _httpx_lines(caplog)
    assert any(
        f"GET https://codeload.github.com/octo/repo/legacy.tar.gz/{_HEAD}?redacted " in line
        for line in lines
    )
    assert not any("token" in line or "temporary" in line for line in lines)


def test_the_httpx_log_keeps_a_url_outside_codeload_as_it_is(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="httpx")
    github = GitHub({_COMPARE: _comparison()})

    _run(github, lambda source: source.compare(_BASE, _HEAD))

    url = f"https://api.github.com/repos/octo/repo/compare/{_BASE}...{_HEAD}?per_page=1"
    assert any(f"GET {url} " in line for line in _httpx_lines(caplog))


def test_the_httpx_log_filter_is_installed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = logging.getLogger("httpx")
    monkeypatch.setattr(logger, "filters", [])

    for _ in range(2):
        _run(GitHub({}), lambda source: asyncio.sleep(0))

    assert len(logger.filters) == 1


def test_archive_holds_the_download_once() -> None:
    size = 32 * 1_048_576

    async def body() -> AsyncIterator[bytes]:
        for _ in range(32):
            yield b"x" * 1_048_576

    github = GitHub({_TARBALL: _redirect(), _CODELOAD_KEY: _answer(200, content=body())})
    tracemalloc.start()
    try:
        archive, _ = _run(github, lambda source: source.archive(_HEAD, max_bytes=size))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert len(archive.content) == size
    # A growing buffer copied out at the end peaks at twice the archive.
    assert peak < 1.5 * size


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/octo/repo/legacy.tar.gz",
        f"http://codeload.github.com/octo/repo/legacy.tar.gz/{_HEAD}",
        f"https://codeload.github.com.evil.example/octo/repo/legacy.tar.gz/{_HEAD}",
        f"https://user@codeload.github.com/octo/repo/legacy.tar.gz/{_HEAD}",
        f"https://codeload.github.com:8443/octo/repo/legacy.tar.gz/{_HEAD}",
        f"/octo/repo/legacy.tar.gz/{_HEAD}",
        None,
    ],
    ids=["foreign-host", "http", "suffix-host", "userinfo", "port", "relative", "no-location"],
)
def test_archive_refuses_a_redirect_outside_codeload(location: str | None) -> None:
    github = GitHub({_TARBALL: _redirect(location)})

    error, requests = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert isinstance(error, VcsUnavailable)
    assert error.code == "vcs_unavailable"
    assert requests == 1
    assert len(github.seen) == 1


def test_archive_without_a_redirect_is_unavailable() -> None:
    github = GitHub({_TARBALL: _answer(200, content=b"\x1f\x8b")})

    error, requests = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert isinstance(error, VcsUnavailable)
    assert requests == 1


@pytest.mark.parametrize(("size", "fits"), [(_MAX, True), (_MAX + 1, False)])
def test_archive_declared_size_against_the_download_limit(size: int, fits: bool) -> None:
    github = GitHub({_TARBALL: _redirect(), _CODELOAD_KEY: _answer(200, content=b"x" * size)})

    result, requests = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    if fits:
        assert result == SourceArchive(commit_sha=_HEAD, content=b"x" * size)
    else:
        assert isinstance(result, IndexLimitExceeded)
        assert result.code == "limit_exceeded:download_bytes"
    assert requests == 2


@pytest.mark.parametrize(("size", "fits"), [(_MAX, True), (_MAX + 1, False)])
def test_archive_streamed_size_against_the_download_limit(size: int, fits: bool) -> None:
    async def body() -> AsyncIterator[bytes]:
        for _ in range(size // 100):
            yield b"x" * 100
        yield b"x" * (size % 100)

    github = GitHub({_TARBALL: _redirect(), _CODELOAD_KEY: _answer(200, content=body())})

    result, _ = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    if fits:
        assert result == SourceArchive(commit_sha=_HEAD, content=b"x" * size)
    else:
        assert isinstance(result, IndexLimitExceeded)
        assert result.code == "limit_exceeded:download_bytes"


def test_archive_refuses_a_content_encoding() -> None:
    github = GitHub(
        {
            _TARBALL: _redirect(),
            _CODELOAD_KEY: _answer(200, content=b"x", headers={"Content-Encoding": "gzip"}),
        }
    )

    error, _ = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert isinstance(error, VcsUnavailable)


@pytest.mark.parametrize(
    ("tarball", "download", "error_type", "requests"),
    [
        (_answer(404), None, VcsAccessDenied, 1),
        (_answer(403, headers={"X-RateLimit-Remaining": "0"}), None, VcsUnavailable, 1),
        (_redirect(), _answer(404), VcsAccessDenied, 2),
        (_redirect(), _answer(503), VcsUnavailable, 2),
    ],
    ids=["tarball-404", "tarball-rate-limited", "codeload-404", "codeload-503"],
)
def test_archive_maps_failures(
    tarball: Callable[[httpx.Request], httpx.Response],
    download: Callable[[httpx.Request], httpx.Response] | None,
    error_type: type[Exception],
    requests: int,
) -> None:
    routes = {_TARBALL: tarball}
    if download is not None:
        routes[_CODELOAD_KEY] = download
    github = GitHub(routes)

    error, counted = _run(github, lambda source: source.archive(_HEAD, max_bytes=_MAX))

    assert isinstance(error, error_type)
    assert counted == requests


# --- request counter ----------------------------------------------------------------------


def _full_build(source: GitHubRepositorySnapshotSource) -> Awaitable[SourceArchive]:
    async def build() -> SourceArchive:
        head = await source.head()
        assert head is not None
        await source.rules_document(head.commit_sha)
        return await source.archive(head.commit_sha, max_bytes=_MAX)

    return build()


def _incremental_build(source: GitHubRepositorySnapshotSource) -> Awaitable[SourceArchive]:
    async def build() -> SourceArchive:
        head = await source.head()
        assert head is not None
        assert await source.compare(_BASE, head.commit_sha) is not None
        await source.rules_document(head.commit_sha)
        return await source.archive(head.commit_sha, max_bytes=_MAX)

    return build()


@pytest.mark.parametrize(("build", "base_requests"), [(_full_build, 4), (_incremental_build, 5)])
@pytest.mark.parametrize(
    ("policy", "extra_requests"), [("present", 3), ("missing-parent", 1), ("missing-rules", 2)]
)
def test_request_count_of_a_build(
    build: Callable[[GitHubRepositorySnapshotSource], Awaitable[SourceArchive]],
    base_requests: int,
    policy: str,
    extra_requests: int,
) -> None:
    tokens = Tokens()
    github = GitHub(
        {
            "api.github.com/repos/octo/repo": _repository(),
            "api.github.com/repos/octo/repo/commits/main": _sha(),
            _COMPARE: _comparison(files=[_entry("src/app.py")]),
            _TARBALL: _redirect(),
            _CODELOAD_KEY: _answer(200, content=b"\x1f\x8b"),
        }
    )

    github.routes.update(_policy_routes())
    if policy != "present":
        github.routes[_ROOT_TREE if policy == "missing-parent" else _REVIEW_TREE] = _answer(
            200, json={"tree": [], "truncated": False}
        )

    archive, counted = _run(github, build, tokens=tokens)

    assert isinstance(archive, SourceArchive), repr(archive)
    assert counted == base_requests + extra_requests
    assert len(github.seen) == base_requests + extra_requests


# --- port errors --------------------------------------------------------------------------


def test_limit_and_unsafe_errors_carry_the_failed_codes() -> None:
    assert IndexLimitExceeded("download_bytes").code == "limit_exceeded:download_bytes"
    assert IndexLimitExceeded("deadline").code == "limit_exceeded:deadline"
    assert ArchiveUnsafe("absolute path").code == "archive_unsafe"
    with pytest.raises(ValueError, match="unknown index limit"):
        IndexLimitExceeded("lines")


def test_unavailable_carries_its_code() -> None:
    assert VcsUnavailable("x").code == "vcs_unavailable"
    assert VcsUnavailable("x", rate_limited=True).code == "vcs_rate_limited"
    assert VcsAccessDenied("x").code == "vcs_access_denied"


# --- authoritative rules at the archive commit (#207) -------------------------------------

_ROOT_TREE = f"api.github.com/repos/octo/repo/git/trees/{_HEAD}"
_REVIEW_TREE = f"api.github.com/repos/octo/repo/git/trees/{'d' * 40}"
_RULES_BLOB = f"api.github.com/repos/octo/repo/git/blobs/{'e' * 40}"


def _policy_entry(
    name: str, kind: str = "blob", mode: str = "100644", *, size: int = 10
) -> dict[str, Any]:
    return {
        "path": name,
        "type": kind,
        "mode": mode,
        "sha": ("d" if kind == "tree" else "e") * 40,
        "size": size,
    }


def _policy_routes(raw: bytes = b"rules\n") -> dict[str, Callable[[httpx.Request], httpx.Response]]:
    return {
        _ROOT_TREE: _answer(
            200,
            json={
                "tree": [_policy_entry(".review", "tree", "040000"), _policy_entry("AGENTS.md")],
                "truncated": False,
            },
        ),
        _REVIEW_TREE: _answer(
            200, json={"tree": [_policy_entry("rules.md", size=len(raw))], "truncated": False}
        ),
        _RULES_BLOB: _answer(200, content=raw),
    }


def test_rules_document_reads_only_the_pinned_rules_blob_and_normalizes_it() -> None:
    github = GitHub(_policy_routes(b"\xef\xbb\xbfhello\r\nworld\r"))

    document, requests = _run(github, lambda source: source.rules_document(_HEAD))

    assert not isinstance(document, Exception), repr(document)
    assert document.presence is DocumentPresence.PRESENT
    assert document.path == ".review/rules.md"
    assert document.text == "hello\nworld\n"
    assert document.blob_oid == "e" * 40
    assert requests == 3
    assert [str(request.url).split("?")[0] for request in github.seen] == [
        f"https://api.github.com/repos/octo/repo/git/trees/{_HEAD}",
        f"https://api.github.com/repos/octo/repo/git/trees/{'d' * 40}",
        f"https://api.github.com/repos/octo/repo/git/blobs/{'e' * 40}",
    ]
    assert github.seen[-1].headers["Accept"] == "application/vnd.github.raw+json"
    assert all(not request.url.query for request in github.seen)


@pytest.mark.parametrize(("missing_parent", "requests"), [(True, 1), (False, 2)])
def test_rules_document_requires_complete_tree_evidence_for_absence(
    missing_parent: bool, requests: int
) -> None:
    routes = _policy_routes()
    routes[_ROOT_TREE if missing_parent else _REVIEW_TREE] = _answer(
        200, json={"tree": [], "truncated": False}
    )
    github = GitHub(routes)

    document, counted = _run(github, lambda source: source.rules_document(_HEAD))

    assert not isinstance(document, Exception), repr(document)
    assert document.presence is DocumentPresence.MISSING
    assert document.text is None
    assert counted == requests


@pytest.mark.parametrize(
    ("path", "answer", "code", "requests"),
    [
        (_ROOT_TREE, _answer(404), "policy_revision_unavailable", 1),
        (_REVIEW_TREE, _answer(404), "policy_revision_unavailable", 2),
        (_RULES_BLOB, _answer(404), "policy_revision_unavailable", 3),
        (_ROOT_TREE, _answer(503), "policy_fetch_transient", 1),
        (_RULES_BLOB, _answer(403), "policy_access_denied", 3),
        (_RULES_BLOB, _answer(429), "policy_rate_limited", 3),
        (_ROOT_TREE, _answer(200, text="invalid"), "policy_fetch_transient", 1),
        (
            _ROOT_TREE,
            _answer(200, json={"tree": [], "truncated": True}),
            "policy_revision_unavailable",
            1,
        ),
        (
            _REVIEW_TREE,
            _answer(200, json={"tree": [], "truncated": True}),
            "policy_revision_unavailable",
            2,
        ),
        (_ROOT_TREE, _answer(200, json={}), "policy_fetch_transient", 1),
    ],
)
def test_rules_document_failure_never_claims_absence(
    path: str, answer: Callable[[httpx.Request], httpx.Response], code: str, requests: int
) -> None:
    routes = _policy_routes()
    routes[path] = answer

    error, counted = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == code
    assert counted == requests


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"\xff", "policy_document_decode_error"),
        (b"a\x00b", "policy_document_control_character"),
        (b"version https://git-lfs.github.com/spec/v1\n", "policy_document_type_unsupported"),
        (b"x" * 65537, "policy_document_too_large"),
    ],
)
def test_rules_document_preserves_review_validation(raw: bytes, code: str) -> None:
    # Missing size metadata must still enforce the streamed byte limit.
    routes = _policy_routes(raw)
    entry = _policy_entry("rules.md")
    del entry["size"]
    routes[_REVIEW_TREE] = _answer(200, json={"tree": [entry]})

    error, requests = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == code
    assert requests == 3


@pytest.mark.parametrize(
    ("parent", "kind", "mode", "requests"),
    [
        (True, "blob", "100644", 1),
        (True, "blob", "120000", 1),
        (True, "commit", "160000", 1),
        (False, "tree", "040000", 2),
        (False, "blob", "120000", 2),
        (False, "commit", "160000", 2),
    ],
)
def test_rules_document_rejects_nonregular_documents_and_parents(
    parent: bool, kind: str, mode: str, requests: int
) -> None:
    routes = _policy_routes()
    routes[_ROOT_TREE if parent else _REVIEW_TREE] = _answer(
        200, json={"tree": [_policy_entry(".review" if parent else "rules.md", kind, mode)]}
    )

    error, counted = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_document_type_unsupported"
    assert counted == requests


def test_rules_document_rejects_declared_oversize_before_fetching_blob() -> None:
    routes = _policy_routes()
    routes[_REVIEW_TREE] = _answer(200, json={"tree": [_policy_entry("rules.md", size=65537)]})

    error, requests = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_document_too_large"
    assert requests == 2


def test_rules_document_accepts_exactly_65536_bytes() -> None:
    document, requests = _run(
        GitHub(_policy_routes(b"x" * 65536)), lambda source: source.rules_document(_HEAD)
    )

    assert not isinstance(document, Exception), repr(document)
    assert document.byte_count == 65536
    assert requests == 3


@pytest.mark.parametrize(
    "entries",
    [
        [_policy_entry(".review/x", "tree", "040000")],
        [_policy_entry("."), _policy_entry("..")],
        [_policy_entry(".review", "blob", "040000")],
        [_policy_entry(".review", "tree", "100644")],
        [{"path": ".review", "type": "blob", "sha": "d" * 40}],
        [_policy_entry(".review", "tree", "040000"), _policy_entry(".review")],
    ],
    ids=[
        "recursive-path",
        "dot-path",
        "inconsistent-blob-mode",
        "inconsistent-tree-mode",
        "missing-mode",
        "duplicate-name",
    ],
)
@pytest.mark.parametrize(
    ("path", "requests"), [(_ROOT_TREE, 1), (_REVIEW_TREE, 2)], ids=["root", "review"]
)
def test_rules_document_rejects_ambiguous_nonrecursive_listings(
    entries: list[dict[str, Any]], path: str, requests: int
) -> None:
    routes = _policy_routes()
    routes[path] = _answer(200, json={"tree": entries, "truncated": False})

    error, counted = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_revision_unavailable"
    assert counted == requests


def test_rules_document_counts_transport_failures() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("unreadable", request=request)

    routes = _policy_routes()
    routes[_RULES_BLOB] = timeout

    error, requests = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_fetch_transient"
    assert requests == 3


def test_rules_document_counts_no_requests_for_a_failed_token_exchange() -> None:
    request = httpx.Request("POST", "https://api.github.com/app/installations/7/access_tokens")
    tokens = Tokens(
        error=httpx.HTTPStatusError(
            "token", request=request, response=httpx.Response(403, request=request)
        )
    )

    error, requests = _run(GitHub({}), lambda source: source.rules_document(_HEAD), tokens=tokens)

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_access_denied"
    assert requests == 0


@pytest.mark.parametrize(
    ("path", "requests"), [(_ROOT_TREE, 1), (_RULES_BLOB, 3)], ids=["tree", "blob"]
)
@pytest.mark.parametrize(
    ("retry_after", "expected_delay"),
    [
        # The shared policy classifier: an unconvertible value waits the one-day ceiling.
        ("1" * 5000, timedelta(days=1)),
        ("9" * 30, timedelta(days=1)),
    ],
    ids=["over-integer-limit", "over-timedelta-limit"],
)
def test_rules_document_bounds_untrusted_retry_after(
    path: str, requests: int, retry_after: str, expected_delay: timedelta | None
) -> None:
    routes = _policy_routes()
    routes[path] = _answer(429, headers={"Retry-After": retry_after})

    error, counted = _run(GitHub(routes), lambda source: source.rules_document(_HEAD))

    assert isinstance(error, PolicyError), repr(error)
    assert error.code.value == "policy_rate_limited"
    assert error.retry_after == expected_delay
    assert counted == requests
