"""Pinned Bitbucket Cloud PR metadata, diffstat, patches and source files."""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from hashlib import sha256
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from app.modules.integrations.bitbucket.infrastructure.commit_hash import (
    pr_commit_hash,
    resolved_commit_hash,
)
from app.modules.integrations.bitbucket.infrastructure.run_http import (
    BitbucketRunHttp,
    BitbucketRunHttpError,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.commit_prefix_limits import (
    COMMIT_PAGE_COUNT,
    COMMIT_PAGE_SIZE,
    MAX_COMMIT_MESSAGES,
    MAX_COMMIT_RESPONSE_BYTES,
    CommitMessagePrefix,
    commit_prefix_incomplete,
)
from app.modules.reviews.application.prompt_builder import (
    FileStatus,
    PullRequestMeta,
    shorten_commit_message,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.unified_diff import UnifiedDiffError, parse_hunks, split_lf
from app.modules.reviews.application.vcs_diff import (
    MAX_BLOB_BYTES,
    BlobTooLargeError,
    MergeBaseUnavailable,
    OmissionReason,
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
)

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_MAX_FILES = 200
_PAGE_SIZE = 100
_MAX_PAGES = 2
_MAX_PATCH_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _Current:
    head: str
    base: str = field(compare=False)
    source_uuid: UUID
    title: str = field(compare=False)
    description: str | None = field(compare=False)
    author: str = field(compare=False)
    source_branch: str = field(compare=False)
    target_branch: str
    draft: bool = field(compare=False)


@dataclass(frozen=True)
class _Stat:
    path: str
    old_path: str | None
    status: FileStatus
    additions: int
    deletions: int
    size: int | None


class _DiffstatLimitExceeded(ValueError):
    """The pinned PR is larger than this review adapter's fixed file/page cap."""


def _object(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError("invalid Bitbucket response")
    return value


def _string(value: object, maximum: int = 4096, *, empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or not (0 if empty else 1) <= len(value) <= maximum
        or "\x00" in value
        or any(0xD800 <= ord(char) <= 0xDFFF for char in value)
    ):
        raise ValueError("invalid Bitbucket response")
    return value


def _sha(value: object) -> str:
    result = _string(value, 40)
    if _SHA.fullmatch(result) is None:
        raise ValueError("invalid Bitbucket commit")
    return result


def _uuid(value: object) -> UUID:
    text = _string(value, 40)
    try:
        return UUID(text.strip("{}"))
    except ValueError:
        raise ValueError("invalid Bitbucket repository UUID") from None


def _path(value: object) -> str:
    path = _string(value, 1024)
    if (
        len(path.encode("utf-8")) > 1024
        or "\\" in path
        or path.startswith("/")
        or any(part in {"", ".", ".."} for part in path.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in path)
    ):
        raise ValueError("invalid Bitbucket path")
    return path


def _count(value: object) -> int:
    if type(value) is not int or not 0 <= value <= 10000000:
        raise ValueError("invalid Bitbucket count")
    return value


def _git_path_token(raw: str) -> str:
    """Decode Git's quoted UTF-8 byte paths without relaxing path identity."""
    if not raw.startswith('"'):
        return raw
    if len(raw) < 2 or not raw.endswith('"'):
        raise ValueError("invalid Git path quote")
    encoded = bytearray()
    body = raw[1:-1]
    index = 0
    while index < len(body):
        char = body[index]
        if char == '"':
            raise ValueError("invalid Git path quote")
        if char != "\\":
            encoded.extend(char.encode("utf-8"))
            index += 1
            continue
        if index + 1 >= len(body):
            raise ValueError("invalid Git path escape")
        if body[index + 1] in {'"', "\\"}:
            encoded.extend(body[index + 1].encode("utf-8"))
            index += 2
        elif index + 4 <= len(body) and all(
            digit in "01234567" for digit in body[index + 1 : index + 4]
        ):
            encoded.append(int(body[index + 1 : index + 4], 8))
            index += 4
        else:
            raise ValueError("invalid Git path escape")
    return encoded.decode("utf-8")


def _git_header_matches(header: str, old: str, new: str) -> bool:
    expected_old, expected_new = f"a/{old}", f"b/{new}"
    if not header.startswith("diff --git "):
        return False
    rest = header.removeprefix("diff --git ")
    if len(rest) > 8192:
        return False
    if rest.startswith('"'):
        index = 1
        while index < len(rest):
            if rest[index] == "\\":
                index += 2
            elif rest[index] == '"':
                break
            else:
                index += 1
        if index >= len(rest):
            return False
        old_token, tail = rest[: index + 1], rest[index + 1 :]
    else:
        if not rest.startswith(expected_old):
            return False
        old_token, tail = expected_old, rest[len(expected_old) :]
    if not tail.startswith(" "):
        return False
    try:
        return (
            _git_path_token(old_token) == expected_old and _git_path_token(tail[1:]) == expected_new
        )
    except (ValueError, UnicodeDecodeError):
        return False


def _git_marker_matches(line: str, prefix: str, expected: str) -> bool:
    if not line.startswith(prefix):
        return False
    try:
        return _git_path_token(line[len(prefix) :].split("\t", 1)[0]) == expected
    except (ValueError, UnicodeDecodeError):
        return False


class HttpBitbucketVcsProvider:
    """The locator and UUID-bound credential are fixed for one Run attempt."""

    def __init__(
        self,
        *,
        http: BitbucketRunHttp,
        locator: PullRequestLocator,
        pinned_base_sha: str | None = None,
        pinned_target_branch: str | None = None,
    ) -> None:
        if (
            locator.provider != "bitbucket"
            or locator.origin_provider_installation_id != http.target.installation_id
            or locator.repository_uuid != http.target.repository_uuid
        ):
            raise ValueError("Bitbucket VCS origin mismatch")
        self._http = http
        self._locator = locator
        self._pinned_base_sha = _sha(pinned_base_sha) if pinned_base_sha is not None else None
        self._pinned_target_branch = pinned_target_branch
        self._current: _Current | None = None
        self._stats: tuple[_Stat, ...] | None = None
        self._pull_request: VcsPullRequest | None = None
        self._blobs: dict[str, tuple[UUID, str, str]] = {}
        self._resolved_commits: dict[tuple[UUID, str], str] = {}

    async def _read_json(
        self,
        suffix: str,
        checkpoint: RunProviderCheckpoint,
        *,
        params: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
        repository_uuid: UUID | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ) -> object:
        await checkpoint()
        value = await self._http.get_json(
            repository_uuid or self._http.target.repository_uuid,
            suffix,
            params=params,
            max_bytes=max_bytes,
        )
        await checkpoint()
        return value

    async def _read_bytes(
        self,
        repository_uuid: UUID,
        suffix: str,
        checkpoint: RunProviderCheckpoint,
        *,
        max_bytes: int,
        params: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
    ) -> bytes:
        await checkpoint()
        value = await self._http.get_bytes(
            repository_uuid, suffix, max_bytes=max_bytes, params=params
        )
        await checkpoint()
        return value

    def _parse_current(self, raw: object) -> _Current:
        data = _object(raw)
        if type(data.get("id")) is not int or data["id"] != self._locator.number:
            raise ValueError("invalid Bitbucket PR identity")
        if data.get("state") != "OPEN":
            raise RunCancelled("pr_closed")
        source, destination = _object(data.get("source")), _object(data.get("destination"))
        destination_uuid = _uuid(_object(destination.get("repository")).get("uuid"))
        if destination_uuid != self._http.target.repository_uuid:
            raise RunCancelled("installation_unavailable")
        source_uuid = _uuid(_object(source.get("repository")).get("uuid"))
        description = data.get("description")
        if description is not None:
            description = _string(description, 65536, empty=True)
        author = _object(data.get("author"))
        draft = data.get("draft", False)
        if type(draft) is not bool:
            raise ValueError("invalid Bitbucket draft status")
        return _Current(
            head=pr_commit_hash(_object(source.get("commit")).get("hash")),
            base=pr_commit_hash(_object(destination.get("commit")).get("hash")),
            source_uuid=source_uuid,
            title=_string(data.get("title"), 500),
            description=description,
            author=_string(author.get("nickname") or author.get("display_name"), 255),
            source_branch=_string(_object(source.get("branch")).get("name"), 255),
            target_branch=_string(_object(destination.get("branch")).get("name"), 255),
            draft=draft,
        )

    async def _canonical(self, checkpoint: RunProviderCheckpoint) -> _Current:
        raw = await self._read_json(f"pullrequests/{self._locator.number}", checkpoint)
        try:
            current = self._parse_current(raw)
        except RunCancelled:
            raise
        except (ValueError, TypeError):
            raise RunFailure(
                "bitbucket_invalid_response", "Bitbucket PR response is malformed"
            ) from None
        if current.source_uuid != self._http.target.repository_uuid:
            self._http.allow_source_repository(current.source_uuid)
        try:
            head = await self._resolve_commit(current.source_uuid, current.head, checkpoint)
        except BitbucketRunHttpError as error:
            if current.source_uuid != self._http.target.repository_uuid and error.status_code in {
                401,
                403,
                404,
            }:
                raise RunFailure(
                    "bitbucket_source_unavailable", "Bitbucket fork source is unreadable"
                ) from None
            raise
        base = self._pinned_base_sha
        if base is None:
            base = await self._resolve_commit(
                self._http.target.repository_uuid, current.base, checkpoint
            )
        return replace(current, head=head, base=base)

    async def _resolve_commit(
        self, repository_uuid: UUID, prefix: str, checkpoint: RunProviderCheckpoint
    ) -> str:
        if len(prefix) == 40:
            return prefix
        identity = (repository_uuid, prefix)
        if identity in self._resolved_commits:
            return self._resolved_commits[identity]
        raw = await self._read_json(f"commit/{prefix}", checkpoint, repository_uuid=repository_uuid)
        try:
            full = resolved_commit_hash(_object(raw).get("hash"), prefix)
        except (ValueError, TypeError):
            raise RunFailure(
                "bitbucket_invalid_response", "Bitbucket commit is malformed"
            ) from None
        self._resolved_commits[identity] = full
        return full

    async def _verify_source(self, current: _Current, checkpoint: RunProviderCheckpoint) -> None:
        if current.source_uuid == self._http.target.repository_uuid:
            return
        if self._resolved_commits.get((current.source_uuid, current.head[:12])) == current.head:
            return
        try:
            raw = await self._read_json(
                f"commit/{current.head}", checkpoint, repository_uuid=current.source_uuid
            )
        except BitbucketRunHttpError as error:
            if error.status_code in {401, 403, 404}:
                raise RunFailure(
                    "bitbucket_source_unavailable", "Bitbucket fork source is unreadable"
                ) from None
            raise
        try:
            if _sha(_object(raw).get("hash")) != current.head:
                raise ValueError("mismatched Bitbucket source commit")
        except (ValueError, TypeError):
            raise RunFailure(
                "bitbucket_invalid_response", "Bitbucket fork source is unreadable"
            ) from None

    @staticmethod
    def _stat(raw: object) -> _Stat:
        data = _object(raw)
        status = data.get("status")
        if status not in {"added", "modified", "removed", "renamed", "copied"}:
            raise ValueError("invalid Bitbucket diff status")
        old_raw, new_raw = data.get("old"), data.get("new")
        old = _path(_object(old_raw).get("path")) if old_raw is not None else None
        new = _path(_object(new_raw).get("path")) if new_raw is not None else None
        if status == "added":
            if new is None:
                raise ValueError("missing added path")
        elif status == "removed":
            if old is None:
                raise ValueError("missing removed path")
        elif old is None or new is None:
            raise ValueError("missing diff path")
        if status == "modified" and old != new:
            raise ValueError("ambiguous modified path")
        size = _object(new_raw).get("size") if new_raw is not None else None
        if size is not None:
            size = _count(size)
        return _Stat(
            path=new or old or "",
            old_path=old if status in {"renamed", "copied"} else None,
            status=status,
            additions=_count(data.get("lines_added")),
            deletions=_count(data.get("lines_removed")),
            size=size,
        )

    @staticmethod
    def _validate_next(next_url: object, endpoint: str, page: int) -> bool:
        if next_url is None:
            return False
        if not isinstance(next_url, str) or len(next_url) > 2048:
            raise ValueError("invalid Bitbucket pagination")
        parsed = urlsplit(next_url)
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.bitbucket.org"
            or unquote(parsed.path) != unquote(endpoint)
            or parsed.fragment
        ):
            raise ValueError("invalid Bitbucket pagination")
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        if query != {"topic": ["true"], "pagelen": [str(_PAGE_SIZE)], "page": [str(page + 1)]}:
            raise ValueError("invalid Bitbucket pagination")
        return True

    async def _diffstat(
        self, current: _Current, checkpoint: RunProviderCheckpoint
    ) -> tuple[_Stat, ...]:
        suffix = f"diffstat/{current.head}..{current.base}"
        endpoint = f"/2.0/repositories/%7B%7D/%7B{self._http.target.repository_uuid}%7D/{suffix}"
        values: list[_Stat] = []
        total: int | None = None
        try:
            for page in range(1, _MAX_PAGES + 1):
                raw = await self._read_json(
                    suffix,
                    checkpoint,
                    params={"topic": "true", "pagelen": str(_PAGE_SIZE), "page": str(page)},
                )
                data = _object(raw)
                batch = data.get("values")
                if not isinstance(batch, list) or len(batch) > _PAGE_SIZE:
                    raise ValueError("invalid Bitbucket diffstat page")
                if ("page" in data and data["page"] != page) or (
                    "pagelen" in data and data["pagelen"] != _PAGE_SIZE
                ):
                    raise ValueError("invalid Bitbucket diffstat page")
                if "size" in data:
                    size = _count(data["size"])
                    if size > _MAX_FILES:
                        raise _DiffstatLimitExceeded
                    if total is None:
                        total = size
                    elif size != total:
                        raise ValueError("changing Bitbucket diffstat size")
                values.extend(self._stat(item) for item in batch)
                if len(values) > _MAX_FILES:
                    raise _DiffstatLimitExceeded
                if total is not None and len(values) > total:
                    raise ValueError("Bitbucket diffstat size mismatch")
                more = self._validate_next(data.get("next"), endpoint, page)
                if not more:
                    if total is not None and len(values) != total:
                        raise ValueError("truncated Bitbucket diffstat")
                    break
            else:
                raise _DiffstatLimitExceeded
            if len({stat.path for stat in values}) != len(values):
                raise ValueError("duplicate Bitbucket diffstat path")
            return tuple(values)
        except _DiffstatLimitExceeded:
            raise RunFailure(
                "bitbucket_review_limit_exceeded", "Bitbucket PR exceeds the diffstat file limit"
            ) from None
        except (ValueError, TypeError):
            raise RunFailure(
                "bitbucket_invalid_response", "Bitbucket diffstat is incomplete"
            ) from None

    async def get_pull_request(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> VcsPullRequest:
        if locator != self._locator:
            raise RunCancelled("installation_unavailable")
        if self._pull_request is not None:
            await checkpoint()
            await self._http.checkpoint()
            await checkpoint()
            return self._pull_request
        current = await self._canonical(checkpoint)
        if (
            self._pinned_target_branch is not None
            and current.target_branch != self._pinned_target_branch
        ):
            raise RunCancelled("superseded")
        if current.source_uuid != self._http.target.repository_uuid:
            self._http.allow_source_repository(current.source_uuid)
        await self._verify_source(current, checkpoint)
        stats = await self._diffstat(current, checkpoint)
        if await self._canonical(checkpoint) != current:
            raise RunCancelled("superseded")
        self._current = current
        self._stats = stats
        for stat in stats:
            if stat.status != "removed":
                blob_id = sha256(f"{current.head}:{stat.path}".encode()).hexdigest()
                self._blobs[blob_id] = (current.source_uuid, current.head, stat.path)
        metadata = PullRequestMeta(
            title=current.title,
            description=current.description,
            author=current.author,
            source_branch=current.source_branch,
            target_branch=current.target_branch,
            labels=(),
            files_changed=len(stats),
            lines_added=sum(item.additions for item in stats),
            lines_removed=sum(item.deletions for item in stats),
            is_draft=current.draft,
            is_fork=current.source_uuid != self._http.target.repository_uuid,
            head_sha=current.head,
        )
        self._pull_request = VcsPullRequest(locator, current.head, current.base, metadata)
        return self._pull_request

    async def source_repository_uuid(self, *, checkpoint: RunProviderCheckpoint) -> UUID:
        await self.get_pull_request(self._locator, checkpoint=checkpoint)
        assert self._current is not None
        return self._current.source_uuid

    async def verify_current(self, *, checkpoint: RunProviderCheckpoint) -> None:
        """One canonical recheck at a stage boundary, after its pinned reads."""
        await self.get_pull_request(self._locator, checkpoint=checkpoint)
        assert self._current is not None
        if await self._canonical(checkpoint) != self._current:
            raise RunCancelled("superseded")

    @staticmethod
    def _commit_next(raw: object, endpoint: str) -> dict[str, str] | None:
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > 2048:
            raise ValueError("invalid Bitbucket commit page")
        try:
            parsed = urlsplit(raw)
            query = parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
            )
        except ValueError:
            raise ValueError("invalid Bitbucket commit page") from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.bitbucket.org"
            or unquote(parsed.path) != unquote(endpoint)
            or parsed.fragment
            or not set(query).issubset({"pagelen", "page", "cursor"})
            or ("page" in query) == ("cursor" in query)
            or any(len(values) != 1 for values in query.values())
        ):
            raise ValueError("invalid Bitbucket commit page")
        params = {key: values[0] for key, values in query.items()}
        if any(
            not value
            or len(value) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            for value in params.values()
        ):
            raise ValueError("invalid Bitbucket commit page")
        pagelen = params.get("pagelen")
        if pagelen is not None and (
            not pagelen.isdecimal() or not 1 <= int(pagelen) <= COMMIT_PAGE_SIZE
        ):
            raise ValueError("invalid Bitbucket commit page")
        return params

    async def get_commit_messages(
        self, locator: PullRequestLocator, *, checkpoint: RunProviderCheckpoint
    ) -> CommitMessagePrefix:
        """Read a bounded API-order prefix and recheck the pinned PR afterwards."""
        pull_request = await self.get_pull_request(locator, checkpoint=checkpoint)
        assert self._current is not None
        current = self._current
        suffix = f"pullrequests/{locator.number}/commits"
        endpoint = f"/2.0/repositories/%7B%7D/%7B{self._http.target.repository_uuid}%7D/{suffix}"
        params = {"pagelen": str(COMMIT_PAGE_SIZE), "page": "1"}
        seen_pages: set[tuple[tuple[str, str], ...]] = set()
        seen_commits: set[str] = set()
        messages: list[str] = []
        incomplete = False
        try:
            for page in range(1, COMMIT_PAGE_COUNT + 1):
                page_key = tuple(sorted(params.items()))
                if page_key in seen_pages:
                    raise ValueError("repeated Bitbucket commit page")
                seen_pages.add(page_key)
                raw = await self._read_json(
                    suffix,
                    checkpoint,
                    params=params,
                    max_bytes=MAX_COMMIT_RESPONSE_BYTES,
                )
                batch = _object(raw).get("values")
                if not isinstance(batch, list) or len(batch) > COMMIT_PAGE_SIZE:
                    raise ValueError("invalid Bitbucket commit page")
                if page == 1 and not batch:
                    # Cloud returns an empty list if the source branch no longer exists.
                    raise ValueError("Bitbucket PR source branch is unavailable")
                remaining = MAX_COMMIT_MESSAGES - len(messages)
                for item in batch[:remaining]:
                    commit = _object(item)
                    commit_sha = _sha(commit.get("hash"))
                    if commit_sha in seen_commits:
                        raise ValueError("repeated Bitbucket commit")
                    seen_commits.add(commit_sha)
                    messages.append(
                        shorten_commit_message(
                            _string(commit.get("message"), 1_048_576, empty=True)
                        )
                    )
                next_params = self._commit_next(_object(raw).get("next"), endpoint)
                incomplete = commit_prefix_incomplete(
                    has_next=next_params is not None,
                    page=page,
                    batch_length=len(batch),
                    remaining=remaining,
                )
                if next_params is None:
                    break
                params = next_params
            if (
                await self._canonical(checkpoint) != current
                or pull_request.head_sha != current.head
            ):
                raise RunCancelled("superseded")
            return CommitMessagePrefix(tuple(messages), incomplete)
        except (BitbucketRunHttpError, ValueError, TypeError) as error:
            if isinstance(error, BitbucketRunHttpError) and error.kind in {
                "total_budget_exceeded",
                "request_budget_exceeded",
                "rate_limited",
            }:
                raise
            raise RunFailure(
                "bitbucket_commit_messages_fetch_failed",
                "Bitbucket PR commit messages are unavailable",
                retry_after=getattr(error, "retry_after", None),
            ) from None

    @staticmethod
    def _patch(raw: bytes, stat: _Stat) -> tuple[str | None, OmissionReason | None]:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None, OmissionReason.MISSING_PATCH
        lines = split_lf(text)
        old_path = stat.old_path or stat.path
        if (
            not lines
            or not _git_header_matches(lines[0], old_path, stat.path)
            or any(line.startswith("diff --git ") for line in lines[1:])
        ):
            return None, OmissionReason.MISSING_PATCH
        hunk_index = next(
            (index for index, line in enumerate(lines) if line.startswith("@@ ")), None
        )
        if hunk_index is None:
            return None, (
                OmissionReason.BINARY
                if any("Binary files" in line or "GIT binary patch" in line for line in lines)
                else OmissionReason.MISSING_PATCH
            )
        old_marker = "/dev/null" if stat.status == "added" else f"a/{old_path}"
        new_marker = "/dev/null" if stat.status == "removed" else f"b/{stat.path}"
        prelude = lines[1:hunk_index]
        old_lines = [line for line in prelude if line.startswith("--- ")]
        new_lines = [line for line in prelude if line.startswith("+++ ")]
        if (
            len(old_lines) != 1
            or len(new_lines) != 1
            or not _git_marker_matches(old_lines[0], "--- ", old_marker)
            or not _git_marker_matches(new_lines[0], "+++ ", new_marker)
        ):
            return None, OmissionReason.MISSING_PATCH
        patch = "\n".join(lines[hunk_index:]) + "\n"
        try:
            hunks = parse_hunks(split_lf(patch))
        except UnifiedDiffError:
            return None, OmissionReason.MISSING_PATCH
        added = sum(row.kind == "added" for hunk in hunks for row in hunk.rows)
        removed = sum(row.kind == "removed" for hunk in hunks for row in hunk.rows)
        if added != stat.additions or removed != stat.deletions:
            return None, OmissionReason.MISSING_PATCH
        return patch, None

    async def get_diff(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> tuple[VcsFile, ...]:
        if pull_request != await self.get_pull_request(self._locator, checkpoint=checkpoint):
            raise RunCancelled("superseded")
        assert self._current is not None and self._stats is not None
        current = self._current
        files: list[VcsFile] = []
        for stat in self._stats:
            try:
                paths = [stat.path]
                if stat.old_path is not None and stat.old_path != stat.path:
                    paths.insert(0, stat.old_path)
                raw = await self._read_bytes(
                    self._http.target.repository_uuid,
                    f"diff/{current.head}..{current.base}",
                    checkpoint,
                    max_bytes=_MAX_PATCH_BYTES,
                    params=(("topic", "true"), *(("path", path) for path in paths)),
                )
                patch, omission = self._patch(raw, stat)
            except BitbucketRunHttpError as error:
                if error.kind != "body_too_large":
                    raise
                patch, omission = None, OmissionReason.TOO_LARGE
            blob_id: str | None = None
            if stat.status != "removed":
                blob_id = sha256(f"{current.head}:{stat.path}".encode()).hexdigest()
            files.append(
                VcsFile(
                    filename=stat.path,
                    status=stat.status,
                    blob_sha=blob_id,
                    previous_filename=stat.old_path,
                    additions=stat.additions,
                    deletions=stat.deletions,
                    changes=stat.additions + stat.deletions,
                    patch=patch,
                    size=stat.size,
                    omission_reason=omission,
                )
            )
        if await self._canonical(checkpoint) != current:
            raise RunCancelled("superseded")
        await self._verify_source(current, checkpoint)
        return tuple(files)

    async def get_blob(
        self, locator: PullRequestLocator, sha: str, *, checkpoint: RunProviderCheckpoint
    ) -> bytes:
        if locator != self._locator:
            raise RunCancelled("installation_unavailable")
        if sha not in self._blobs:
            # A retry after StoreDiffSnapshot starts with a fresh VCS adapter.
            # Rebuild only from a verified current PR/diffstat, never from the
            # opaque, persisted blob id or a mutable repository slug.
            await self.get_pull_request(locator, checkpoint=checkpoint)
        if sha not in self._blobs or self._current is None:
            raise RunCancelled("superseded")
        repository_uuid, revision, path = self._blobs[sha]
        try:
            result = await self._read_bytes(
                repository_uuid,
                f"src/{revision}/{quote(path, safe='/')}",
                checkpoint,
                max_bytes=MAX_BLOB_BYTES,
            )
            # Source bytes are pinned by the full commit SHA. The caller verifies
            # the mutable PR once at the end of the blob stage, not per file.
            await self._http.checkpoint()
            return result
        except BitbucketRunHttpError as error:
            if error.kind == "body_too_large":
                raise BlobTooLargeError(
                    "Bitbucket source file exceeds review size budget"
                ) from None
            raise

    async def get_merge_base(
        self, pull_request: VcsPullRequest, *, checkpoint: RunProviderCheckpoint
    ) -> str:
        if pull_request != await self.get_pull_request(self._locator, checkpoint=checkpoint):
            raise RunCancelled("superseded")
        try:
            raw = await self._read_json(
                f"merge-base/{pull_request.head_sha}..{pull_request.base_sha}", checkpoint
            )
            result = _sha(_object(raw).get("hash"))
            if await self._canonical(checkpoint) != self._current:
                raise RunCancelled("superseded")
            return result
        except RunFailure as error:
            if error.error_code in {
                "bitbucket_response_budget_exceeded",
                "bitbucket_review_limit_exceeded",
                "bitbucket_request_budget_exceeded",
            } or (isinstance(error, BitbucketRunHttpError) and error.kind == "rate_limited"):
                raise
            raise MergeBaseUnavailable("Bitbucket merge base is unavailable") from None
        except ValueError:
            raise MergeBaseUnavailable("Bitbucket merge base is unavailable") from None
