"""GitHub implementation of the indexer's VCS port (#142).

One instance serves one repository and one job. It counts every request it sends to
the API and to codeload, failed ones included; the token exchange is not counted.
Failures map to the port's provider-neutral errors as in PIPELINE_SPEC §5.2 (model:
`reviews/infrastructure/vcs_failures.py`, a 401 included: it stays retryable).

The codeload URL of a private repository carries a download token, so no error carries a
URL or chains an httpx error. `httpx` logs every request with its URL at INFO; the adapter
adds one `logging.Filter` to the `httpx` logger, once per process, that redacts the query
of a codeload URL in that request line. Other URLs are logged as they are. That covers
the INFO request lines only: at DEBUG, `httpcore` logs the response headers, the
tarball's 302 `Location` with its token included. The worker entrypoints log at INFO, so
that line is not written today; a worker turned to DEBUG writes it.

GitHub REST docs, checked 2026-10-16:

- compare lists the changed files on the first page only, at most 300 for the whole
  comparison; `per_page` pages the commits;
- the tarball answers 302 with a `Location`; the link of a private repository expires
  after five minutes;
- `GET /commits/{ref}` with `application/vnd.github.sha` answers the bare SHA.

Observed, not documented: the `Location` names `codeload.github.com`, an unknown ref
answers 422, and an unknown compare base 404 (a probe of a public repository); an empty
repository answers 409 "Git Repository is empty." (`_is_empty_repository_answer`).
"""

from __future__ import annotations

import io
import logging
import re
from datetime import timedelta
from typing import Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, model_validator

from app.common.infrastructure.github_repository_path import (
    commit_sha_segment,
    ref_segment,
    repository_path_from_full_name,
)
from app.modules.code_index.application.vcs import (
    Ancestry,
    BranchHead,
    ChangedPath,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.integrations.webhooks.infrastructure.github_installation_tree_provider import (
    _is_empty_repository_answer,
)

_API_VERSION = "2022-11-28"
_REQUEST_TIMEOUT = 10.0
_ARCHIVE_HOST = "codeload.github.com"
# GitHub lists at most 300 files of a comparison, so 300 may be a cut list.
_COMPARE_FILE_CAP = 300
_MAX_COMPARE_RESPONSE_BYTES = 8 * 1024 * 1024
# The longest delay a `Retry-After` can ask for.
_MAX_RETRY_AFTER_SECONDS = 86_400
_SHA = re.compile(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}")

_ANCESTRY = {
    "ahead": Ancestry.AHEAD,
    "identical": Ancestry.IDENTICAL,
    "behind": Ancestry.BEHIND,
    "diverged": Ancestry.DIVERGED,
}
_CHANGE_STATUS = {
    "added": ChangeStatus.ADDED,
    "modified": ChangeStatus.MODIFIED,
    "removed": ChangeStatus.REMOVED,
    "renamed": ChangeStatus.RENAMED,
    "copied": ChangeStatus.COPIED,
    "changed": ChangeStatus.CHANGED,
    "unchanged": ChangeStatus.UNCHANGED,
}


class InstallationTokenProvider(Protocol):
    async def get_installation_access_token(self, installation_external_id: int) -> str: ...


class _GitHubDto(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)


class _Repository(_GitHubDto):
    default_branch: str | None = None


class _DiffEntry(_GitHubDto):
    filename: str = Field(min_length=1)
    status: Literal["added", "modified", "removed", "renamed", "copied", "changed", "unchanged"]
    previous_filename: str | None = None

    @model_validator(mode="after")
    def validate_entry(self) -> _DiffEntry:
        if self.status == "renamed" and not self.previous_filename:
            raise ValueError("renamed GitHub file needs previous_filename")
        return self


class _Comparison(_GitHubDto):
    status: Literal["ahead", "identical", "behind", "diverged"]
    # Not required by the schema: without it the change set is unknown.
    files: list[_DiffEntry] | None = None


_REPOSITORY = TypeAdapter(_Repository)
_COMPARISON = TypeAdapter(_Comparison)


def _headers(token: str, *, accept: str = "application/vnd.github+json") -> dict[str, str]:
    return {
        "Accept": accept,
        "Authorization": f"Bearer {token}",
        "X-GitHub-Api-Version": _API_VERSION,
    }


class _ArchiveUrlRedaction(logging.Filter):
    """Redacts the query of a codeload URL in the request line `httpx` logs."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(_redacted(arg) for arg in record.args)
        return True


def _redacted(value: object) -> object:
    if isinstance(value, httpx.URL) and value.host == _ARCHIVE_HOST and value.query:
        return value.copy_with(query=b"redacted")
    return value


# One instance: `addFilter` adds it once however many sources are built.
_ARCHIVE_URL_REDACTION = _ArchiveUrlRedaction()


def _retry_after(value: str | None) -> timedelta | None:
    """The delay a `Retry-After` of whole seconds asks for, at most one day; a date, a
    sign, a non-ASCII digit or more digits than `int()` reads is no delay."""
    if not value or not (value.isascii() and value.isdecimal()):
        return None
    try:
        seconds = int(value)
    except ValueError:
        # Over 4300 digits (`sys.get_int_max_str_digits`).
        return None
    return timedelta(seconds=min(seconds, _MAX_RETRY_AFTER_SECONDS))


def _classify(response: httpx.Response) -> VcsUnavailable | VcsAccessDenied:
    status = response.status_code
    delay = _retry_after(response.headers.get("Retry-After"))
    limited = response.headers.get("X-RateLimit-Remaining") == "0" or delay is not None
    message = f"GitHub answered {status}"
    if status == 429 or (status == 403 and limited):
        return VcsUnavailable(message, rate_limited=True, retry_after=delay)
    # A 401 is not here: as in the model, it retries, and a fresh installation token can
    # heal it.
    if status in {403, 404}:
        return VcsAccessDenied(message)
    return VcsUnavailable(message, retry_after=delay)


def _transport_failure(exc: httpx.HTTPError) -> VcsUnavailable | VcsAccessDenied:
    if isinstance(exc, httpx.HTTPStatusError):
        return _classify(exc.response)
    return VcsUnavailable(f"GitHub request failed: {type(exc).__name__}")


def _archive_url(location: str | None) -> httpx.URL:
    """The codeload URL of a tarball redirect; any other target is refused unsent."""
    try:
        url = httpx.URL(location) if location else None
    except httpx.InvalidURL:
        url = None
    if (
        url is None
        or url.scheme != "https"
        or url.host != _ARCHIVE_HOST
        or url.userinfo
        or url.port not in {None, 443}
    ):
        raise VcsUnavailable("GitHub redirected the archive outside codeload")
    return url


async def _capped_body(response: httpx.Response, max_bytes: int) -> bytes | None:
    """The decoded body, or `None` when it is declared or read over `max_bytes`."""
    declared = response.headers.get("Content-Length")
    if declared is not None and declared.isdecimal() and int(declared) > max_bytes:
        return None
    body = bytearray()
    async for chunk in response.aiter_bytes():
        if len(body) + len(chunk) > max_bytes:
            return None
        body.extend(chunk)
    return bytes(body)


class GitHubRepositorySnapshotSource:
    """``RepositorySnapshotSource`` of one repository through its GitHub App installation."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        token_provider: InstallationTokenProvider,
        installation_external_id: int,
        repository_full_name: str,
    ) -> None:
        if installation_external_id <= 0:
            raise ValueError("GitHub installation id must be positive")
        logging.getLogger("httpx").addFilter(_ARCHIVE_URL_REDACTION)
        self._client = client
        self._tokens = token_provider
        self._installation = installation_external_id
        self._prefix = repository_path_from_full_name(repository_full_name)
        self._requests = 0

    @property
    def request_count(self) -> int:
        return self._requests

    async def head(self) -> BranchHead | None:
        try:
            token = await self._token()
            response = await self._get(self._prefix, headers=_headers(token))
            if not response.is_success:
                raise _classify(response)
            try:
                branch = _REPOSITORY.validate_json(response.content).default_branch
            except ValidationError:
                raise VcsUnavailable("GitHub returned an unreadable repository") from None
            if not branch:
                return None
            response = await self._get(
                f"{self._prefix}/commits/{ref_segment(branch)}",
                headers=_headers(token, accept="application/vnd.github.sha"),
            )
        except httpx.HTTPError as exc:
            raise _transport_failure(exc) from None
        if _is_empty_repository_answer(response) or response.status_code in {404, 422}:
            return None
        if not response.is_success:
            raise _classify(response)
        sha = response.text.strip()
        if _SHA.fullmatch(sha) is None:
            raise VcsUnavailable("GitHub returned an unreadable commit SHA")
        return BranchHead(branch=branch, commit_sha=sha)

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        base, head = commit_sha_segment(base_sha), commit_sha_segment(head_sha)
        path = f"{self._prefix}/compare/{base}...{head}"
        try:
            token = await self._token()
            self._requests += 1
            async with self._client.stream(
                "GET",
                path,
                # The changed files come with the first page; one commit per page keeps it
                # small.
                params={"per_page": 1},
                headers=_headers(token) | {"Accept-Encoding": "identity"},
                timeout=_REQUEST_TIMEOUT,
                follow_redirects=False,
            ) as response:
                if response.status_code == 404:
                    return None
                if not response.is_success:
                    raise _classify(response)
                body = await _capped_body(response, _MAX_COMPARE_RESPONSE_BYTES)
        except httpx.HTTPError as exc:
            raise _transport_failure(exc) from None
        if body is None:
            return Comparison(ancestry=None, files=(), complete=False)
        try:
            comparison = _COMPARISON.validate_json(body)
        except ValidationError:
            raise VcsUnavailable("GitHub returned an unreadable comparison") from None
        ancestry = _ANCESTRY[comparison.status]
        if comparison.files is None:
            return Comparison(ancestry=ancestry, files=(), complete=ancestry is Ancestry.IDENTICAL)
        return Comparison(
            ancestry=ancestry,
            files=tuple(
                ChangedPath(item.filename, _CHANGE_STATUS[item.status], item.previous_filename)
                for item in comparison.files
            ),
            complete=len(comparison.files) < _COMPARE_FILE_CAP,
        )

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        sha = commit_sha_segment(commit_sha)
        try:
            token = await self._token()
            response = await self._get(f"{self._prefix}/tarball/{sha}", headers=_headers(token))
            if response.status_code != 302:
                raise _classify(response)
            url = _archive_url(response.headers.get("Location"))
            content = await self._download(url, max_bytes)
        except httpx.HTTPError as exc:
            raise _transport_failure(exc) from None
        return SourceArchive(commit_sha=sha, content=content)

    async def _token(self) -> str:
        return await self._tokens.get_installation_access_token(self._installation)

    async def _get(self, path: str, *, headers: dict[str, str]) -> httpx.Response:
        self._requests += 1
        return await self._client.get(
            path, headers=headers, timeout=_REQUEST_TIMEOUT, follow_redirects=False
        )

    async def _download(self, url: httpx.URL, max_bytes: int) -> bytes:
        # Built by hand so that no default header or auth of the client adds credentials:
        # the codeload URL authorizes the download itself.
        request = self._client.build_request(
            "GET", url, headers={"Accept-Encoding": "identity"}, timeout=_REQUEST_TIMEOUT
        )
        request.headers.pop("Authorization", None)
        self._requests += 1
        response = await self._client.send(request, stream=True, auth=None, follow_redirects=False)
        try:
            if not response.is_success:
                raise _classify(response)
            # The gzip bytes of the archive itself are counted and kept: with a transfer
            # encoding, httpx would decode it and the limit would count other bytes.
            encoding = response.headers.get("Content-Encoding", "").strip().casefold()
            if encoding not in {"", "identity"}:
                raise VcsUnavailable("GitHub archive uses an unsupported content encoding")
            declared = response.headers.get("Content-Length")
            if declared is not None and declared.isdecimal() and int(declared) > max_bytes:
                raise IndexLimitExceeded("download_bytes")
            # `getvalue()` hands over the buffer without a copy, so the archive is held once.
            body = io.BytesIO()
            async for chunk in response.aiter_bytes():
                if body.tell() + len(chunk) > max_bytes:
                    raise IndexLimitExceeded("download_bytes")
                body.write(chunk)
            return body.getvalue()
        finally:
            await response.aclose()
