"""Bitbucket Cloud index snapshots through immutable repository UUID and /src.

The non-REST archive URL has no verified scoped-token redirect contract. We instead
assemble a bounded tarball from the documented pinned /src directory/file API. An
incomplete directory page, changed binding or file redirect fails the whole snapshot.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
from hashlib import sha256
from urllib.parse import parse_qs, quote, unquote, urlsplit

import httpx

from app.modules.code_index.application.vcs import (
    Ancestry,
    BranchHead,
    Comparison,
    IndexLimitExceeded,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.integrations.bitbucket.application.request_credentials import (
    BitbucketRequestAuthority,
    BoundBitbucketTarget,
)
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
_MAX_PAGES_PER_DIRECTORY = 1000
_MAX_DIRECTORIES = 1000
_MAX_ENTRIES = 100_000
_MAX_LIST_BODY = 2 * 1024 * 1024
_MAX_REPOSITORY_BODY = 256 * 1024
_MAX_FILE_BYTES = 100 * 1024 * 1024
_MAX_TOTAL_SOURCE_BYTES = 100 * 1024 * 1024
_MAX_RETRY_AFTER = 3600


@dataclass(frozen=True)
class _Entry:
    path: str
    directory: bool
    symlink: bool
    size: int | None


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


def _retry_after(raw: str | None) -> timedelta | None:
    if raw and len(raw) <= 4 and raw.isascii() and raw.isdecimal():
        seconds = int(raw)
        if 1 <= seconds <= _MAX_RETRY_AFTER:
            return timedelta(seconds=seconds)
    return None


def _status_error(response: httpx.Response) -> VcsUnavailable | VcsAccessDenied:
    retry = _retry_after(response.headers.get("Retry-After"))
    if response.status_code == 429:
        return VcsUnavailable(
            "Bitbucket rate limited the source", rate_limited=True, retry_after=retry
        )
    if response.status_code in {403, 404}:
        return VcsAccessDenied("Bitbucket source is not accessible")
    return VcsUnavailable("Bitbucket source request failed", retry_after=retry)


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
    ) -> None:
        self._client = client
        self._target_for_repository = target_for_repository
        self._credentials = credentials
        self._target: BoundBitbucketTarget | None = None
        self._requests = 0
        self._read_bytes = 0
        self._tree_cache: dict[str, dict[str, _Entry]] = {}
        self._directory_cache: dict[tuple[str, str], tuple[_Entry, ...]] = {}

    @property
    def request_count(self) -> int:
        return self._requests

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
        self, suffix: str, *, max_bytes: int, params: dict[str, str] | None = None
    ) -> bytes:
        target = await self._bound()
        try:
            credential = await self._credentials.credential_for(target)
        except ValueError:
            raise VcsAccessDenied("Bitbucket repository credential changed") from None
        endpoint = f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/{suffix}"
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
                    raise _status_error(response)
                declared = response.headers.get("Content-Length")
                if declared and declared.isdecimal() and int(declared) > max_bytes:
                    raise IndexLimitExceeded("download_bytes")
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if (
                        len(body) + len(chunk) > max_bytes
                        or self._read_bytes + len(body) + len(chunk) > _MAX_TOTAL_SOURCE_BYTES
                    ):
                        raise IndexLimitExceeded("download_bytes")
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
        # Diffstat does not prove ancestry. A full snapshot is the safe fallback.
        return Comparison(None, (), False)

    async def _directory(self, commit_sha: str, path: str) -> tuple[_Entry, ...]:
        key = (commit_sha, path)
        if key in self._directory_cache:
            await self._bound()
            return self._directory_cache[key]
        target = await self._bound()
        suffix = f"src/{commit_sha}/" + (f"{quote(path, safe='/')}/" if path else "")
        endpoint = f"/2.0/repositories/%7B%7D/%7B{target.repository_uuid}%7D/{suffix}"
        entries: list[_Entry] = []
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
                if type(size) is not int or not 0 <= size <= _MAX_ENTRIES:
                    raise VcsUnavailable("Bitbucket returned an invalid source listing")
                if declared_size is None:
                    declared_size = size
                elif declared_size != size:
                    raise VcsUnavailable("Bitbucket changed a source listing")
            for raw_entry in values:
                if not isinstance(raw_entry, dict):
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                entry_path = _path(raw_entry.get("path"))
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
                size_value = raw_entry.get("size")
                if kind == "commit_directory":
                    if size_value is not None:
                        raise VcsUnavailable("Bitbucket returned an invalid source entry")
                    entry = _Entry(entry_path, True, False, None)
                elif kind == "commit_file":
                    if type(size_value) is not int or not 0 <= size_value <= 1_000_000_000:
                        raise VcsUnavailable("Bitbucket returned an invalid source entry")
                    entry = _Entry(entry_path, False, "link" in attributes, size_value)
                else:
                    raise VcsUnavailable("Bitbucket returned an invalid source entry")
                entries.append(entry)
                if len(entries) > _MAX_ENTRIES:
                    raise IndexLimitExceeded("archive_entries")
            next_params = _next_params(raw.get("next"), endpoint)
            if next_params is None:
                if declared_size is not None and len(entries) != declared_size:
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
            directories += 1
            if directories > _MAX_DIRECTORIES:
                raise IndexLimitExceeded("archive_entries")
            for entry in await self._directory(commit_sha, directory):
                if entry.path in found:
                    raise VcsUnavailable("Bitbucket repeated a source path")
                found[entry.path] = entry
                if len(found) > _MAX_ENTRIES:
                    raise IndexLimitExceeded("archive_entries")
                if entry.directory:
                    pending.append(entry.path)
        self._tree_cache[commit_sha] = found
        return found

    async def _file(self, commit_sha: str, path: str, *, max_bytes: int) -> bytes:
        return await self._read(
            f"src/{commit_sha}/{quote(_path(path), safe='/')}", max_bytes=max_bytes
        )

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        sha = _sha(commit_sha)
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
            raw = await self._file(sha, RULES_DOCUMENT_PATH, max_bytes=MAX_DOCUMENT_BYTES)
            oid = sha256(f"{sha}:{RULES_DOCUMENT_PATH}".encode()).hexdigest()
            return normalize_document(RULES_DOCUMENT_PATH, raw, blob_oid=oid, policy_base_sha=sha)
        except (VcsUnavailable, VcsAccessDenied, IndexLimitExceeded) as error:
            code = (
                PolicyErrorCode.DOCUMENT_TOO_LARGE
                if isinstance(error, IndexLimitExceeded)
                else PolicyErrorCode.FETCH_TRANSIENT
            )
            raise PolicyError(
                code,
                "Bitbucket rules could not be read",
                policy_base_sha=sha,
                source_path=RULES_DOCUMENT_PATH,
                limit=MAX_DOCUMENT_BYTES if code is PolicyErrorCode.DOCUMENT_TOO_LARGE else None,
            ) from None

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        sha = _sha(commit_sha)
        if max_bytes <= 0:
            raise ValueError("Invalid Bitbucket archive limit")
        max_bytes = min(max_bytes, _MAX_TOTAL_SOURCE_BYTES)
        entries = await self._tree(sha)
        contents: list[tuple[_Entry, bytes]] = []
        total = 0
        for entry in sorted(entries.values(), key=lambda item: item.path):
            if entry.directory:
                continue
            if entry.size is None or entry.size > _MAX_FILE_BYTES:
                raise IndexLimitExceeded("download_bytes")
            remaining = max_bytes - total
            if entry.size > remaining:
                raise IndexLimitExceeded("download_bytes")
            body = await self._file(sha, entry.path, max_bytes=max(1, remaining))
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
        return SourceArchive(sha, content)


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
