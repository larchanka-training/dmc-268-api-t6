"""One attempt's GitLab saved-version VCS and bounded metadata reads.

Line totals describe validated supplied hunks only. Missing provider patches remain
explicit file omissions; this adapter cannot establish the absence of whole lost hunks
on legacy GitLab 17.4. Canonical target/start and merge base stay distinct.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from functools import partial
from urllib.parse import parse_qs, urlsplit

from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabCredentialUnavailable,
    GitLabRequestUnavailable,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import FetchCode
from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabCanonicalMergeRequest,
    GitLabCommitsResource,
    GitLabDiffVersionResource,
    GitLabDiffVersionsResource,
    GitLabRunHttp,
    GitLabRunHttpError,
    JsonValue,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.commit_prefix_limits import (
    COMMIT_PAGE_COUNT,
    COMMIT_PAGE_SIZE,
    MAX_COMMIT_MESSAGES,
    commit_prefix_incomplete,
)
from app.modules.reviews.application.prompt_builder import (
    FileStatus,
    PullRequestMeta,
    shorten_commit_message,
)
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.unified_diff import UnifiedDiffError, parse_hunks, split_lf
from app.modules.reviews.application.vcs_diff import (
    MAX_BLOB_BYTES,
    BlobTooLargeError,
    CommitMessagePrefix,
    MergeBaseUnavailable,
    OmissionReason,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)

_VCS_BYTES = 33554432
_MAX_FILES = 200


class _InvalidResponse(Exception):
    pass


def _object(value: JsonValue) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise _InvalidResponse
    return value


def _integer(value: JsonValue, *, maximum: int = 9223372036854775807, zero: bool = False) -> int:
    if type(value) is not int or not (0 if zero else 1) <= value <= maximum:
        raise _InvalidResponse
    return value


def _count(value: JsonValue) -> int:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{1,3}", value) is None:
        raise _InvalidResponse
    return _integer(int(value), maximum=_MAX_FILES, zero=True)


def _text(value: JsonValue, maximum: int = 4096, *, empty: bool = False) -> str:
    if not isinstance(value, str) or not (0 if empty else 1) <= len(value) <= maximum:
        raise _InvalidResponse
    return value


def _oid(value: JsonValue) -> str:
    result = _text(value, 64)
    if re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", result) is None:
        raise _InvalidResponse
    return result.lower()


def _path(value: JsonValue) -> str:
    result = _text(value)
    if (
        len(result.encode("utf-8")) > 4096
        or "\\" in result
        or any(ord(char) < 32 or ord(char) == 127 for char in result)
        or any(part in {"", ".", ".."} for part in result.split("/"))
    ):
        raise _InvalidResponse
    return result


def _flag(data: dict[str, JsonValue], name: str, *, required: bool = False) -> bool:
    if name not in data and not required:
        return False
    value = data.get(name)
    if type(value) is not bool:
        raise _InvalidResponse
    return value


class HttpGitLabVcsProvider:
    """Public VcsMetadataProvider and MergeBaseProvider backed by one owned run HTTP.

    Successful raw bytes are retained without eviction within the same 32 MiB read
    ceiling, so classification/cache fill does not redownload immutable OIDs. No token,
    global route, authorization-validity bit, retry engine or GitHub fallback is cached.
    """

    def __init__(self, *, http: GitLabRunHttp) -> None:
        self._http = http
        self._expected = http.expected_input
        self._snapshot: GitLabCanonicalMergeRequest | None = None
        self._pull_request: VcsPullRequest | None = None
        self._files: tuple[VcsFile, ...] | None = None
        self._resolved: tuple[VcsFile, ...] | None = None
        self._raw: dict[str, bytes] = {}
        self._known_oids: set[str] = set()
        self._raw_bytes = 0

    async def _checkpoint(self, checkpoint: RunProviderCheckpoint, code: FetchCode) -> None:
        try:
            await checkpoint()
            await self._http.checkpoint(error_code=code)
        except (
            OperationalError,
            InterfaceError,
            DisconnectionError,
            SqlAlchemyTimeoutError,
            OSError,
            GitLabCredentialUnavailable,
            GitLabRequestUnavailable,
        ):
            raise RequiredSourceUnavailable(code) from None

    async def _read[T](
        self,
        checkpoint: RunProviderCheckpoint,
        code: FetchCode,
        operation: Callable[[], Awaitable[T]],
    ) -> T:
        await self._checkpoint(checkpoint, code)
        try:
            value = await operation()
        except GitLabRunHttpError:
            await self._checkpoint(checkpoint, code)
            raise
        await self._checkpoint(checkpoint, code)
        return value

    def _locator(self, locator: PullRequestLocator) -> None:
        if locator != self._expected.locator:
            raise RunCancelled("installation_unavailable")

    @staticmethod
    def _failure(error: GitLabRunHttpError | _InvalidResponse, code: FetchCode) -> RunFailure:
        return RunFailure(
            code,
            "GitLab review source is unavailable",
            retry_after=error.retry_after if isinstance(error, GitLabRunHttpError) else None,
        )

    async def _canonical(
        self,
        checkpoint: RunProviderCheckpoint,
        code: FetchCode,
    ) -> GitLabCanonicalMergeRequest:
        current = await self._read(
            checkpoint,
            code,
            lambda: self._http.get_canonical_merge_request(
                error_code=code,
                max_total_bytes=_VCS_BYTES,
            ),
        )
        if self._snapshot is not None and _count(
            _object(current.data).get("changes_count")
        ) != _count(_object(self._snapshot.data).get("changes_count")):
            raise _InvalidResponse
        self._snapshot = current
        return current

    def _next_page(
        self,
        headers: Mapping[str, str],
        *,
        page: int,
        length: int,
        page_size: int,
        endpoint: str,
        require_continuation: bool = True,
    ) -> bool:
        # Tokens construct a new numeric query; URLs are checked, never followed.
        next_value = headers.get("x-next-page")
        link = headers.get("link")
        linked_next = False
        if link is not None:
            for item in link.split(","):
                match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([a-z]+)"\s*', item)
                if match is None:
                    raise _InvalidResponse
                try:
                    parsed = urlsplit(match[1])
                    query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
                except ValueError:
                    raise _InvalidResponse from None
                # The origin was authorized by HTTP; use the received canonical
                # request's origin supplied below via the snapshot's target route.
                # Origin is compared through the public canonical response field.
                if (
                    parsed.scheme != "https"
                    or parsed.username
                    or parsed.password
                    or parsed.fragment
                ):
                    raise _InvalidResponse
                expected_origin = self._origin
                if (
                    f"{parsed.scheme}://{parsed.netloc}" != expected_origin
                    or parsed.path != endpoint
                ):
                    raise _InvalidResponse
                if set(query) != {"page", "per_page"} or query["per_page"] != [str(page_size)]:
                    raise _InvalidResponse
                if len(query["page"]) != 1 or not query["page"][0].isdigit():
                    raise _InvalidResponse
                if match[2] == "next":
                    if query["page"] != [str(page + 1)]:
                        raise _InvalidResponse
                    linked_next = True
        if next_value is not None:
            if link is not None and bool(next_value) != linked_next:
                raise _InvalidResponse
            if next_value == "":
                if linked_next:
                    raise _InvalidResponse
                return False
            if next_value != str(page + 1):
                raise _InvalidResponse
            return True
        if linked_next:
            return True
        if length < page_size or not require_continuation:
            return False
        # A full page without authoritative terminal/next metadata is uncertain.
        raise _InvalidResponse

    @property
    def _origin(self) -> str:
        # Canonical provenance owns this trusted origin; never read a repository URL.
        return self._http.instance_origin

    async def _prepare(self, checkpoint: RunProviderCheckpoint) -> VcsPullRequest:
        if self._pull_request is not None:
            return self._pull_request
        snapshot = await self._canonical(checkpoint, "diff_fetch_failed")
        data = _object(snapshot.data)
        count = _count(data.get("changes_count"))
        if self._pull_request is not None:
            return self._pull_request
        selected: int | None = None
        seen: set[int] = set()
        for page in range(1, 6):
            response = await self._read(
                checkpoint,
                "diff_fetch_failed",
                partial(
                    self._http.get_json,
                    GitLabDiffVersionsResource(page),
                    max_bytes=1048576,
                    max_total_bytes=_VCS_BYTES,
                ),
            )
            if not isinstance(response.data, list) or len(response.data) > 100:
                raise _InvalidResponse
            matches: list[int] = []
            for raw in response.data:
                row = _object(raw)
                identity = _integer(row.get("id"))
                if identity in seen:
                    raise _InvalidResponse
                seen.add(identity)
                triple = tuple(
                    _oid(row.get(key))
                    for key in ("head_commit_sha", "start_commit_sha", "base_commit_sha")
                )
                if triple == (snapshot.head_sha, snapshot.start_sha, snapshot.base_sha):
                    matches.append(identity)
            # Validate every supplied pagination field even when this page matches.
            # A positive match needs no claim about unobserved historical pages.
            has_next = self._next_page(
                response.headers,
                page=page,
                length=len(response.data),
                page_size=100,
                endpoint=f"/api/v4/projects/{snapshot.target_project_id}/merge_requests/{snapshot.iid}/versions",
                require_continuation=not matches,
            )
            if matches:
                selected = matches[0]
                break
            if not has_next:
                break
        if selected is None:
            raise _InvalidResponse
        response = await self._read(
            checkpoint,
            "diff_fetch_failed",
            lambda: self._http.get_json(
                GitLabDiffVersionResource(selected),
                max_bytes=8388608,
                max_total_bytes=_VCS_BYTES,
            ),
        )
        detail = _object(response.data)
        if _integer(detail.get("id")) != selected or detail.get("state") != "collected":
            raise _InvalidResponse
        if tuple(
            _oid(detail.get(key))
            for key in ("head_commit_sha", "start_commit_sha", "base_commit_sha")
        ) != (snapshot.head_sha, snapshot.start_sha, snapshot.base_sha):
            raise _InvalidResponse
        records = detail.get("diffs")
        if (
            not isinstance(records, list)
            or len(records) != count
            or _count(detail.get("real_size")) != count
        ):
            raise _InvalidResponse
        files = tuple(self._file(_object(record)) for record in records)
        if len({item.filename for item in files}) != count:
            raise _InvalidResponse
        author = _object(data.get("author"))
        description = data.get("description")
        if description is not None:
            description = _text(description, 131072, empty=True)
        labels = data.get("labels")
        if not isinstance(labels, list) or len(labels) > 100:
            raise _InvalidResponse
        metadata = PullRequestMeta(
            title=_text(data.get("title"), 500),
            description=description,
            author=_text(author.get("username"), 255),
            source_branch=_text(data.get("source_branch"), 255),
            target_branch=_text(data.get("target_branch"), 255),
            labels=tuple(_text(label, 255) for label in labels),
            files_changed=count,
            lines_added=sum(item.additions for item in files),
            lines_removed=sum(item.deletions for item in files),
            is_draft=_flag(data, "draft", required=True),
            is_fork=snapshot.source_project_id != snapshot.target_project_id,
            head_sha=self._expected.head_sha,
        )
        # Current canonical identity/refs/count rechecked after version retrieval.
        await self._canonical(checkpoint, "diff_fetch_failed")
        self._files = files
        self._pull_request = VcsPullRequest(
            self._expected.locator,
            self._expected.head_sha,
            self._expected.base_sha,
            metadata,
        )
        return self._pull_request

    @staticmethod
    def _file(data: dict[str, JsonValue]) -> VcsFile:
        old, new = _path(data.get("old_path")), _path(data.get("new_path"))
        added, removed, renamed = (
            _flag(data, name, required=True)
            for name in ("new_file", "deleted_file", "renamed_file")
        )
        if sum((added, removed, renamed)) > 1 or (not renamed and old != new):
            raise _InvalidResponse
        status: FileStatus = (
            "added" if added else "removed" if removed else "renamed" if renamed else "modified"
        )
        patch = data.get("diff")
        if patch is not None:
            patch = _text(patch, 8388608, empty=True) or None
        reason = None
        if _flag(data, "too_large"):
            reason = OmissionReason.TOO_LARGE
        if _flag(data, "collapsed") and reason is None:
            reason = OmissionReason.MISSING_PATCH
        if _flag(data, "generated_file"):
            reason = OmissionReason.GENERATED
        additions = deletions = 0
        if patch is not None and reason is None:
            if len(patch.encode("utf-8")) > 1048576 or len(split_lf(patch)) > 20000:
                reason = OmissionReason.TOO_LARGE
            else:
                try:
                    hunks = parse_hunks(split_lf(patch))
                except UnifiedDiffError:
                    raise _InvalidResponse from None
                additions = sum(row.kind == "added" for hunk in hunks for row in hunk.rows)
                deletions = sum(row.kind == "removed" for hunk in hunks for row in hunk.rows)
        return VcsFile(
            new,
            status,
            None,
            old if renamed else None,
            additions,
            deletions,
            additions + deletions,
            patch,
            omission_reason=reason,
        )

    async def get_pull_request(
        self,
        locator: PullRequestLocator,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> VcsPullRequest:
        await self._checkpoint(checkpoint, "diff_fetch_failed")
        self._locator(locator)
        try:
            return await self._prepare(checkpoint)
        except (GitLabRunHttpError, _InvalidResponse) as error:
            raise self._failure(error, "diff_fetch_failed") from None

    async def get_diff(
        self,
        pull_request: VcsPullRequest,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> tuple[VcsFile, ...]:
        await self._checkpoint(checkpoint, "diff_fetch_failed")
        self._locator(pull_request.locator)
        try:
            current = await self._prepare(checkpoint)
            if current != pull_request:
                raise RunCancelled("superseded")
            if self._resolved is not None:
                return self._resolved
            assert self._files is not None and self._snapshot is not None
            result: list[VcsFile] = []
            for item in self._files:
                if item.status == "removed":
                    result.append(item)
                    continue
                response = await self._read(
                    checkpoint,
                    "diff_fetch_failed",
                    partial(
                        self._http.head_file,
                        item.filename,
                        current.head_sha,
                        side="head",
                        max_total_bytes=_VCS_BYTES,
                    ),
                )
                oid = _oid(response.headers.get("x-gitlab-blob-id"))
                size_text = response.headers.get("x-gitlab-size")
                if size_text is None or re.fullmatch(r"[0-9]{1,19}", size_text) is None:
                    raise _InvalidResponse
                self._known_oids.add(oid)
                result.append(replace(item, blob_sha=oid, size=int(size_text)))
            await self._canonical(checkpoint, "diff_fetch_failed")
            self._resolved = tuple(result)
            return self._resolved
        except (GitLabRunHttpError, _InvalidResponse) as error:
            raise self._failure(error, "diff_fetch_failed") from None

    async def get_blob(
        self,
        locator: PullRequestLocator,
        sha: str,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> bytes:
        await self._checkpoint(checkpoint, "diff_fetch_failed")
        self._locator(locator)
        try:
            oid = _oid(sha)
            if oid not in self._known_oids:
                raise _InvalidResponse
            cached = self._raw.get(oid)
            if cached is not None:
                return cached
            data = await self._read(
                checkpoint,
                "diff_fetch_failed",
                lambda: self._http.get_blob(
                    oid,
                    max_bytes=MAX_BLOB_BYTES,
                    max_total_bytes=_VCS_BYTES,
                ),
            )
            if self._raw_bytes + len(data) > _VCS_BYTES:
                raise GitLabRunHttpError("budget_exhausted")
            self._raw[oid] = data
            self._raw_bytes += len(data)
            return data
        except GitLabRunHttpError as error:
            if error.kind == "body_too_large":
                raise BlobTooLargeError("GitLab blob exceeds the retrieval budget") from None
            raise self._failure(error, "diff_fetch_failed") from None
        except _InvalidResponse as error:
            raise self._failure(error, "diff_fetch_failed") from None

    async def get_merge_base(
        self,
        pull_request: VcsPullRequest,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> str:
        await self._checkpoint(checkpoint, "diff_fetch_failed")
        self._locator(pull_request.locator)
        try:
            if (
                pull_request.head_sha != self._expected.head_sha
                or pull_request.base_sha != self._expected.base_sha
            ):
                raise RunCancelled("superseded")
            current = await self._prepare(checkpoint)
            if current != pull_request:
                raise RunCancelled("superseded")
            assert self._snapshot is not None
            return self._snapshot.base_sha
        except (GitLabRunHttpError, _InvalidResponse):
            raise MergeBaseUnavailable("GitLab merge base is unavailable") from None

    async def get_commit_messages(
        self,
        locator: PullRequestLocator,
        *,
        checkpoint: RunProviderCheckpoint,
    ) -> CommitMessagePrefix:
        code: FetchCode = "commit_messages_fetch_failed"
        await self._checkpoint(checkpoint, code)
        self._locator(locator)
        try:
            snapshot = await self._canonical(checkpoint, code)
            messages: list[str] = []
            seen: set[str] = set()
            incomplete = False
            for page in range(1, COMMIT_PAGE_COUNT + 1):
                response = await self._read(
                    checkpoint,
                    code,
                    partial(
                        self._http.get_json,
                        GitLabCommitsResource(page),
                        max_bytes=1048576,
                        error_code=code,
                        max_total_bytes=_VCS_BYTES,
                    ),
                )
                batch = response.data
                if not isinstance(batch, list) or len(batch) > COMMIT_PAGE_SIZE:
                    raise _InvalidResponse
                remaining = MAX_COMMIT_MESSAGES - len(messages)
                for raw in batch[:remaining]:
                    item = _object(raw)
                    oid = _oid(item.get("id"))
                    if oid in seen:
                        raise _InvalidResponse
                    seen.add(oid)
                    messages.append(
                        shorten_commit_message(_text(item.get("message"), 1048576, empty=True))
                    )
                has_next = self._next_page(
                    response.headers,
                    page=page,
                    length=len(batch),
                    page_size=COMMIT_PAGE_SIZE,
                    endpoint=f"/api/v4/projects/{snapshot.target_project_id}/merge_requests/{snapshot.iid}/commits",
                )
                incomplete = commit_prefix_incomplete(
                    has_next=has_next, page=page, batch_length=len(batch), remaining=remaining
                )
                if not has_next:
                    break
            await self._canonical(checkpoint, code)
            return CommitMessagePrefix(tuple(messages), incomplete)
        except (GitLabRunHttpError, _InvalidResponse) as error:
            raise self._failure(error, code) from None
