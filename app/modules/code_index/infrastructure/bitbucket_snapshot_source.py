"""Bitbucket Cloud index snapshots through immutable repository UUID and /src.

The non-REST archive URL has no verified scoped-token redirect contract. We instead
assemble a bounded tarball from the documented pinned /src directory/file API. An
incomplete directory page or changed binding fails the snapshot. A redirecting raw
file is skipped and counted as unavailable; no Authorization header follows it.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
from collections.abc import Awaitable, Callable, Set
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveEntry,
    BranchHead,
    ChangedPath,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestAuthority,
    BoundBitbucketTarget,
)
from app.modules.integrations.bitbucket.infrastructure.retry_after import parse_retry_after
from app.modules.reviews.application.policy_documents import (
    MAX_DOCUMENT_BYTES,
    missing_document,
    normalize_document,
)
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import RULES_DOCUMENT_PATH, PolicyDocument

_ORIGIN = "https://api.bitbucket.org"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PAGE_SIZE = 100
_MAX_PAGES_PER_DIRECTORY = 5
_MAX_DIRECTORIES = 160
_MAX_DEPTH = 16
_MAX_ENTRIES = 160
_MAX_REQUESTS = 160
_MAX_LIST_BODY = 2 * 1024 * 1024
_MAX_REPOSITORY_BODY = 256 * 1024
_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 100 * 1024 * 1024


@dataclass(frozen=True)
class _Entry:
    path: str
    directory: bool
    symlink: bool
    size: int | None


class _FileRedirect(Exception):
    """A raw blob is stored behind a redirect we must not follow with a token."""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


class _TotalSourceBudgetExceeded(IndexLimitExceeded):
    """The combined source budget says nothing about one document's size."""

    def __init__(self) -> None:
        super().__init__("download_bytes")


def _sha(value: object) -> str:
    if not isinstance(value, str) or _SHA.fullmatch(value) is None:
        raise ValueError("Invalid Bitbucket commit SHA")
    return value


def _path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value.encode("utf-8")) > 1024
        or value.startswith("/")
        or "\\" in value
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
    ):
        raise VcsUnavailable("Bitbucket returned an invalid source path")
    return value


def _status_error(response: httpx.Response) -> VcsUnavailable | VcsAccessDenied:
    retry = parse_retry_after(response.headers.get("Retry-After"))
    if response.status_code == 429:
        return VcsUnavailable(
            "Bitbucket rate limited the source",
            rate_limited=True,
            retry_after=retry or timedelta(hours=1),
            http_status=429,
        )
    if response.status_code in {401, 403, 404}:
        return VcsAccessDenied(
            f"Bitbucket source is not accessible (HTTP {response.status_code})",
            http_status=response.status_code,
            pin_head=response.status_code == 401,
        )
    return VcsUnavailable(
        f"Bitbucket source request failed (HTTP {response.status_code})",
        retry_after=retry,
        http_status=response.status_code,
    )


def _next_params(raw: object, endpoint: str) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, str) or len(raw) > 2048:
        raise VcsUnavailable("Bitbucket returned an invalid source page")
    try:
        parsed = urlsplit(raw)
        query = parse_qs(
            parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
        )
    except ValueError:
        raise VcsUnavailable("Bitbucket returned an invalid source page") from None
    if (
        parsed.scheme != "https"
        or parsed.netloc != "api.bitbucket.org"
        or unquote(parsed.path) != unquote(endpoint)
        or parsed.fragment
        or not set(query).issubset({"pagelen", "page", "cursor"})
        or ("page" in query) == ("cursor" in query)
        or any(len(values) != 1 for values in query.values())
    ):
        raise VcsUnavailable("Bitbucket returned an invalid source page")
    params = {key: values[0] for key, values in query.items()}
    if any(
        not value or len(value) > 256 or any(ord(char) < 33 or ord(char) > 126 for char in value)
        for value in params.values()
    ):
        raise VcsUnavailable("Bitbucket returned an invalid source page")
    pagelen = params.get("pagelen")
    if pagelen is not None and (not pagelen.isdecimal() or not 1 <= int(pagelen) <= _PAGE_SIZE):
        raise VcsUnavailable("Bitbucket returned an invalid source page")
    return params


class BitbucketRepositorySnapshotSource:
    """One index job, pinned to a portal binding and a repository UUID."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        target_for_repository: Callable[[], Awaitable[BoundBitbucketTarget]],
        credentials: BitbucketRequestAuthority,
        limits: IndexLimits | None = None,
    ) -> None:
        self._client = client
        self._target_for_repository = target_for_repository
        self._credentials = credentials
        self._limits = limits or IndexLimits()
        self._target: BoundBitbucketTarget | None = None
        self._requests = 0
        self._read_bytes = 0
        self._listed_entries = 0
        self._tree_cache: dict[str, dict[str, _Entry]] = {}
        self._directory_cache: dict[tuple[str, str], tuple[_Entry, ...]] = {}
        self._file_cache: dict[tuple[str, str], bytes | _FileRedirect] = {}
        self._redirect_skips: set[tuple[str, str]] = set()
        self._skipped_entries: dict[str, int] = {}

    @property
    def request_count(self) -> int:
        return self._requests

    async def checkpoint(self) -> None:
        """Fence a completed rules read against disconnect or token rotation."""
        await self._bound()

    @property
    def skipped_entries(self) -> dict[str, int]:
        """Counts of provider entries omitted from this snapshot attempt."""
        return self._skipped_entries.copy()

    def _skip(self, reason: str) -> None:
        self._skipped_entries[reason] = self._skipped_entries.get(reason, 0) + 1

    async def _bound(self) -> BoundBitbucketTarget:
        if self._target is None:
            try:
                self._target = await self._target_for_repository()
            except ValueError:
                raise VcsAccessDenied("Bitbucket repository binding is inactive") from None
        try:
            await self._credentials.checkpoint(self._target)
        except ValueError:
            raise VcsAccessDenied("Bitbucket repository binding changed") from None
        return self._target

    async def _read(
        self,
        suffix: str,
        *,
        max_bytes: int,
        params: dict[str, str] | None = None,
        skip_file_redirect: bool = False,
    ) -> bytes:
        target = await self._bound()
        try:
            credential = await self._credentials.credential_for(target)
        except ValueError:
            raise VcsAccessDenied("Bitbucket repository credential changed") from None
        endpoint = f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/{suffix}"
        if self._requests >= _MAX_REQUESTS:
            raise IndexLimitExceeded("provider_requests")
        try:
            self._requests += 1
            async with self._client.stream(
                "GET",
                f"{_ORIGIN}{endpoint}",
                params=params,
                headers={
                    "Authorization": f"Bearer {credential.token}",
                    "Accept-Encoding": "identity",
                },
                timeout=10.0,
                follow_redirects=False,
            ) as response:
                if response.status_code != 200:
                    if skip_file_redirect and response.status_code in {301, 302, 303, 307, 308}:
                        raise _FileRedirect(response.status_code)
                    raise _status_error(response)
                declared = response.headers.get("Content-Length")
                if declared and declared.isdecimal() and int(declared) > max_bytes:
                    raise IndexLimitExceeded("download_bytes")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > max_bytes:
                        raise IndexLimitExceeded("download_bytes")
                    if self._read_bytes + len(body) + len(chunk) > min(
                        _MAX_TOTAL_SOURCE_BYTES, self._limits.download_bytes
                    ):
                        raise _TotalSourceBudgetExceeded()
                    body.extend(chunk)
        except httpx.RequestError:
            raise VcsUnavailable("Bitbucket source request failed") from None
        await self._bound()
        self._read_bytes += len(body)
        return bytes(body)

    async def _json(
        self, suffix: str, *, max_bytes: int, params: dict[str, str] | None = None
    ) -> dict[str, object]:
        body = await self._read(suffix, max_bytes=max_bytes, params=params)
        try:
            value = json.loads(body)
        except (ValueError, RecursionError):
            raise VcsUnavailable("Bitbucket returned invalid source JSON") from None
        if not isinstance(value, dict):
            raise VcsUnavailable("Bitbucket returned invalid source JSON")
        return value

    async def head(self) -> BranchHead | None:
        repository = await self._json("", max_bytes=_MAX_REPOSITORY_BODY)
        target = await self._bound()
        if repository.get("uuid") != "{" + str(target.repository_uuid) + "}":
            raise VcsAccessDenied("Bitbucket repository UUID changed")
        main = repository.get("mainbranch")
        if main is None:
            return None
        branch = main.get("name") if isinstance(main, dict) else None
        if (
            not isinstance(branch, str)
            or not 1 <= len(branch) <= 255
            or any(ord(c) < 32 for c in branch)
        ):
            raise VcsUnavailable("Bitbucket returned an invalid default branch")
        raw = await self._json(
            f"refs/branches/{quote(branch, safe='')}", max_bytes=_MAX_REPOSITORY_BODY
        )
        target_commit = raw.get("target")
        sha = target_commit.get("hash") if isinstance(target_commit, dict) else None
        try:
            return BranchHead(branch, _sha(sha))
        except ValueError:
            raise VcsUnavailable("Bitbucket returned an invalid default branch commit") from None

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        _sha(base_sha)
        _sha(head_sha)
        await self._bound()
        if base_sha == head_sha:
            return Comparison(Ancestry.IDENTICAL, (), True)
        merge = await self._json(
            f"merge-base/{base_sha}..{head_sha}", max_bytes=_MAX_REPOSITORY_BODY
        )
        if merge.get("hash") != base_sha:
            return Comparison(None, (), False)
        suffix = f"diffstat/{base_sha}..{head_sha}"
        target = await self._bound()
        endpoint = f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/{suffix}"
        params = {"pagelen": str(_PAGE_SIZE), "page": "1"}
        seen: set[tuple[tuple[str, str], ...]] = set()
        files: list[ChangedPath] = []
        declared_size: int | None = None
        for _ in range(_MAX_PAGES_PER_DIRECTORY):
            page_key = tuple(sorted(params.items()))
            if page_key in seen:
                raise VcsUnavailable("Bitbucket repeated a diffstat page")
            seen.add(page_key)
            raw = await self._json(suffix, max_bytes=_MAX_LIST_BODY, params=params)
            values = raw.get("values")
            size = raw.get("size")
            if not isinstance(values, list) or len(values) > _PAGE_SIZE:
                raise VcsUnavailable("Bitbucket returned an invalid diffstat")
            if size is not None:
                if type(size) is not int or size < 0:
                    raise VcsUnavailable("Bitbucket returned an invalid diffstat")
                if size > self._limits.incremental_files:
                    return Comparison(Ancestry.AHEAD, (), False)
                if declared_size is None:
                    declared_size = size
                elif declared_size != size:
                    raise VcsUnavailable("Bitbucket changed a diffstat")
            for item in values:
                if not isinstance(item, dict):
                    raise VcsUnavailable("Bitbucket returned an invalid diffstat")
                status = item.get("status")
                if not isinstance(status, str) or status not in ChangeStatus._value2member_map_:
                    raise VcsUnavailable("Bitbucket returned an invalid diffstat")
                old = item.get("old")
                new = item.get("new")
                try:
                    if status == "removed":
                        path = _path(old.get("path") if isinstance(old, dict) else None)
                    else:
                        path = _path(new.get("path") if isinstance(new, dict) else None)
                    previous = (
                        _path(old.get("path") if isinstance(old, dict) else None)
                        if status in {"renamed", "copied"}
                        else None
                    )
                except VcsUnavailable:
                    # A provider path that cannot be represented safely is not an
                    # incremental change set. A complete full walk can skip it.
                    return Comparison(Ancestry.AHEAD, (), False)
                files.append(ChangedPath(path, ChangeStatus(status), previous))
                if len(files) > self._limits.incremental_files:
                    return Comparison(Ancestry.AHEAD, (), False)
            next_params = _next_params(raw.get("next"), endpoint)
            if next_params is None:
                if declared_size is not None and len(files) != declared_size:
                    raise VcsUnavailable("Bitbucket diffstat is incomplete")
                if len({item.path for item in files}) != len(files):
                    raise VcsUnavailable("Bitbucket repeated a diffstat path")
                return Comparison(Ancestry.AHEAD, tuple(files), True)
            params = next_params
        return Comparison(Ancestry.AHEAD, (), False)

    async def _directory(self, commit_sha: str, path: str) -> tuple[_Entry, ...]:
        key = (commit_sha, path)
        if key in self._directory_cache:
            await self._bound()
            return self._directory_cache[key]
        target = await self._bound()
        suffix = f"src/{commit_sha}/" + (f"{quote(path, safe='/')}/" if path else "")
        endpoint = f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/{suffix}"
        entries: list[_Entry] = []
        listed_count = 0
        params = {"pagelen": str(_PAGE_SIZE), "page": "1"}
        seen_pages: set[tuple[tuple[str, str], ...]] = set()
        declared_size: int | None = None
        for _ in range(_MAX_PAGES_PER_DIRECTORY):
            page_key = tuple(sorted(params.items()))
            if page_key in seen_pages:
                raise VcsUnavailable("Bitbucket repeated a source page")
            seen_pages.add(page_key)
            raw = await self._json(suffix, max_bytes=_MAX_LIST_BODY, params=params)
            values = raw.get("values")
            size = raw.get("size")
            if not isinstance(values, list) or len(values) > _PAGE_SIZE:
                raise VcsUnavailable("Bitbucket returned an invalid source listing")
            if size is not None:
                if type(size) is not int or size < 0:
                    raise VcsUnavailable("Bitbucket returned an invalid source listing")
                if size > min(_MAX_ENTRIES, self._limits.archive_entries):
                    raise IndexLimitExceeded("archive_entries")
                if declared_size is None:
                    declared_size = size
                elif declared_size != size:
                    raise VcsUnavailable("Bitbucket changed a source listing")
            for raw_entry in values:
                if not isinstance(raw_entry, dict):
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                listed_count += 1
                self._listed_entries += 1
                if self._listed_entries > min(_MAX_ENTRIES, self._limits.archive_entries):
                    raise IndexLimitExceeded("archive_entries")
                try:
                    entry_path = _path(raw_entry.get("path"))
                except VcsUnavailable:
                    self._skip("unrepresentable_path")
                    continue
                immediate = entry_path.removeprefix(f"{path}/" if path else "")
                if (
                    not immediate
                    or "/" in immediate
                    or (path and not entry_path.startswith(f"{path}/"))
                ):
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                commit = raw_entry.get("commit")
                if not isinstance(commit, dict) or commit.get("hash") != commit_sha:
                    raise VcsUnavailable("Bitbucket source entry changed commit")
                kind = raw_entry.get("type")
                attributes = raw_entry.get("attributes", [])
                if not isinstance(attributes, list) or not all(
                    isinstance(item, str) for item in attributes
                ):
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                if kind not in {"commit_file", "commit_directory", "commit_subrepository"}:
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                if kind == "commit_subrepository" or "subrepository" in attributes:
                    self._skip("subrepository")
                    continue
                if "binary" in attributes or "lfs" in attributes:
                    self._skip("lfs" if "lfs" in attributes else "binary")
                    continue
                size_value = raw_entry.get("size")
                if kind == "commit_directory":
                    if size_value is not None:
                        raise VcsUnavailable("Bitbucket returned an invalid source entry")
                    entry = _Entry(entry_path, True, False, None)
                elif kind == "commit_file":
                    if type(size_value) is not int or not 0 <= size_value <= 1_000_000_000:
                        raise VcsUnavailable("Bitbucket returned an invalid source entry")
                    entry = _Entry(
                        entry_path,
                        False,
                        "link" in attributes,
                        size_value,
                    )
                else:
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                entries.append(entry)
                if len(entries) > min(_MAX_ENTRIES, self._limits.archive_entries):
                    raise IndexLimitExceeded("archive_entries")
            next_params = _next_params(raw.get("next"), endpoint)
            if next_params is None:
                if declared_size is not None and listed_count != declared_size:
                    raise VcsUnavailable("Bitbucket source listing is incomplete")
                break
            params = next_params
        else:
            raise IndexLimitExceeded("archive_entries")
        if len({entry.path for entry in entries}) != len(entries):
            raise VcsUnavailable("Bitbucket repeated a source path")
        result = tuple(entries)
        self._directory_cache[key] = result
        return result

    async def _tree(self, commit_sha: str) -> dict[str, _Entry]:
        if commit_sha in self._tree_cache:
            await self._bound()
            return self._tree_cache[commit_sha]
        pending = [""]
        found: dict[str, _Entry] = {}
        directories = 0
        while pending:
            directory = pending.pop()
            if directory.count("/") + bool(directory) > _MAX_DEPTH:
                raise IndexLimitExceeded("archive_entries")
            directories += 1
            if directories > min(_MAX_DIRECTORIES, self._limits.archive_entries):
                raise IndexLimitExceeded("archive_entries")
            for entry in await self._directory(commit_sha, directory):
                if entry.path in found:
                    raise VcsUnavailable("Bitbucket repeated a source path")
                found[entry.path] = entry
                if len(found) > min(_MAX_ENTRIES, self._limits.archive_entries):
                    raise IndexLimitExceeded("archive_entries")
                if entry.directory:
                    pending.append(entry.path)
        self._tree_cache[commit_sha] = found
        return found

    async def _file(
        self, commit_sha: str, path: str, *, max_bytes: int, skip_redirect: bool = False
    ) -> bytes | _FileRedirect:
        key = (commit_sha, path)
        if key in self._file_cache:
            await self._bound()
            cached = self._file_cache[key]
            if isinstance(cached, bytes) and len(cached) > max_bytes:
                raise IndexLimitExceeded("download_bytes")
            return cached
        body: bytes | _FileRedirect
        try:
            body = await self._read(
                f"src/{commit_sha}/{quote(_path(path), safe='/')}",
                max_bytes=max_bytes,
                skip_file_redirect=skip_redirect,
            )
        except _FileRedirect as error:
            body = error
        self._file_cache[key] = body
        return body

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        sha = _sha(commit_sha)
        reading_file = False
        try:
            root = await self._directory(sha, "")
            review = next((item for item in root if item.path == ".review"), None)
            if review is None:
                return missing_document(RULES_DOCUMENT_PATH)
            if not review.directory:
                raise PolicyError(
                    PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
                    "Bitbucket rules parent is not a directory",
                    policy_base_sha=sha,
                    source_path=RULES_DOCUMENT_PATH,
                )
            children = await self._directory(sha, ".review")
            rules = next((item for item in children if item.path == RULES_DOCUMENT_PATH), None)
            if rules is None:
                return missing_document(RULES_DOCUMENT_PATH)
            if rules.directory or rules.symlink:
                raise PolicyError(
                    PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
                    "Bitbucket rules are not a regular file",
                    policy_base_sha=sha,
                    source_path=RULES_DOCUMENT_PATH,
                )
            if rules.size is not None and rules.size > MAX_DOCUMENT_BYTES:
                raise PolicyError(
                    PolicyErrorCode.DOCUMENT_TOO_LARGE,
                    "Bitbucket rules are too large",
                    policy_base_sha=sha,
                    source_path=RULES_DOCUMENT_PATH,
                    limit=MAX_DOCUMENT_BYTES,
                )
            reading_file = True
            raw = await self._file(
                sha, RULES_DOCUMENT_PATH, max_bytes=MAX_DOCUMENT_BYTES, skip_redirect=True
            )
            if isinstance(raw, _FileRedirect):
                raise PolicyError(
                    PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED,
                    f"Bitbucket rules redirect to an unsupported blob (HTTP {raw.status})",
                    policy_base_sha=sha,
                    source_path=RULES_DOCUMENT_PATH,
                    http_status=raw.status,
                )
            if len(raw) != rules.size:
                raise VcsUnavailable("Bitbucket rules file size differs from the pinned listing")
            oid = sha256(f"{sha}:{RULES_DOCUMENT_PATH}".encode()).hexdigest()
            return normalize_document(RULES_DOCUMENT_PATH, raw, blob_oid=oid, policy_base_sha=sha)
        except IndexLimitExceeded as error:
            if error.limit != "download_bytes":
                raise
            raise PolicyError(
                (
                    PolicyErrorCode.DOCUMENT_TOO_LARGE
                    if reading_file and not isinstance(error, _TotalSourceBudgetExceeded)
                    else PolicyErrorCode.FETCH_TRANSIENT
                ),
                "Bitbucket rules could not be read",
                policy_base_sha=sha,
                source_path=RULES_DOCUMENT_PATH,
                limit=(
                    MAX_DOCUMENT_BYTES
                    if reading_file and not isinstance(error, _TotalSourceBudgetExceeded)
                    else None
                ),
            ) from None
        except (VcsUnavailable, VcsAccessDenied) as error:
            if isinstance(error, VcsAccessDenied):
                code = PolicyErrorCode.ACCESS_DENIED
            elif error.rate_limited:
                code = PolicyErrorCode.RATE_LIMITED
            else:
                code = PolicyErrorCode.FETCH_TRANSIENT
            raise PolicyError(
                code,
                f"Bitbucket rules could not be read: {error}",
                policy_base_sha=sha,
                source_path=RULES_DOCUMENT_PATH,
                retry_after=error.retry_after if isinstance(error, VcsUnavailable) else None,
                http_status=error.http_status,
                pin_head=isinstance(error, VcsAccessDenied) and error.pin_head,
            ) from None

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        sha = _sha(commit_sha)
        if max_bytes <= 0:
            raise ValueError("Invalid Bitbucket archive limit")
        max_bytes = min(max_bytes, _MAX_TOTAL_SOURCE_BYTES, self._limits.download_bytes)
        entries = await self._tree(sha)
        selected = {
            entry.path
            for entry in entries.values()
            if not entry.directory
            and entry.size is not None
            and entry.size <= self._limits.read_bytes
        }
        return await self.archive_paths(sha, selected, max_bytes=max_bytes)

    async def manifest(self, commit_sha: str) -> dict[str, ArchiveEntry]:
        """Complete pinned path set for a sparse incremental index build."""
        sha = _sha(commit_sha)
        entries = await self._tree(sha)
        return {
            entry.path: ArchiveEntry(entry.path, entry.size or 0, symlink=entry.symlink)
            for entry in entries.values()
            if not entry.directory
        }

    async def archive_paths(
        self, commit_sha: str, paths: Set[str], *, max_bytes: int
    ) -> SourceArchive:
        """Fetch only selected files after a complete pinned tree listing."""
        sha = _sha(commit_sha)
        if max_bytes <= 0:
            raise ValueError("Invalid Bitbucket archive limit")
        max_bytes = min(max_bytes, _MAX_TOTAL_SOURCE_BYTES, self._limits.download_bytes)
        entries = await self._tree(sha)
        if any(path not in entries or entries[path].directory for path in paths):
            raise VcsUnavailable("Bitbucket sparse archive path is absent from the pinned tree")
        eligible = [
            entry
            for entry in sorted((entries[path] for path in paths), key=lambda item: item.path)
            if entry.size is not None and entry.size <= self._limits.read_bytes
        ]
        if sum(entry.size or 0 for entry in eligible) > max_bytes:
            raise IndexLimitExceeded("download_bytes")
        uncached = sum((sha, entry.path) not in self._file_cache for entry in eligible)
        if self._requests + uncached > _MAX_REQUESTS:
            raise IndexLimitExceeded("provider_requests")
        contents: list[tuple[_Entry, bytes]] = []
        total = 0
        for entry in eligible:
            if entry.size is None or entry.size > _MAX_FILE_BYTES:
                raise IndexLimitExceeded("download_bytes")
            remaining = max_bytes - total
            if entry.size > remaining:
                raise IndexLimitExceeded("download_bytes")
            body = await self._file(
                sha, entry.path, max_bytes=max(1, remaining), skip_redirect=True
            )
            if isinstance(body, _FileRedirect):
                key = (sha, entry.path)
                if key not in self._redirect_skips:
                    self._redirect_skips.add(key)
                    self._skip("redirect")
                continue
            if len(body) != entry.size:
                raise VcsUnavailable("Bitbucket source file size differs from the pinned listing")
            total += len(body)
            if total > max_bytes:
                raise IndexLimitExceeded("download_bytes")
            if entry.symlink and (len(body) > 1024 or b"\x00" in body):
                raise VcsUnavailable("Bitbucket returned an invalid symbolic link")
            contents.append((entry, body))
        await self._bound()
        content = await asyncio.to_thread(_tarball, sha, contents)
        if len(content) > max_bytes:
            raise IndexLimitExceeded("download_bytes")
        return SourceArchive(sha, content, self.skipped_entries)


def _tarball(sha: str, contents: list[tuple[_Entry, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz", format=tarfile.PAX_FORMAT) as tar:
        root = tarfile.TarInfo(f"bitbucket-{sha}/")
        root.type = tarfile.DIRTYPE
        tar.addfile(root)
        for entry, body in contents:
            info = tarfile.TarInfo(f"bitbucket-{sha}/{entry.path}")
            if entry.symlink:
                try:
                    info.linkname = body.decode("utf-8")
                except UnicodeDecodeError:
                    raise VcsUnavailable("Bitbucket returned an invalid symbolic link") from None
                info.type = tarfile.SYMTYPE
            else:
                info.size = len(body)
                info.type = tarfile.REGTYPE
            tar.addfile(info, io.BytesIO(body) if not entry.symlink else None)
    return buffer.getvalue()
