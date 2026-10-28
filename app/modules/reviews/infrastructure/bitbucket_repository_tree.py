"""Bounded Bitbucket Cloud source directory reads at pinned revisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from hashlib import sha256
from urllib.parse import parse_qs, quote, unquote, urlsplit
from uuid import UUID

from app.modules.integrations.bitbucket.infrastructure.run_http import (
    BitbucketRunHttp,
    BitbucketRunHttpError,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RunCancelled
from app.modules.reviews.infrastructure.bitbucket_vcs import HttpBitbucketVcsProvider, _path

_SHA = re.compile(r"[0-9a-f]{40}\Z")
_PAGE_SIZE = 100
_MAX_PAGES = 8
_MAX_DIRECTORIES = 128
_MAX_DEPTH = 16
_MAX_ENTRIES = 5000


@dataclass(frozen=True)
class BitbucketTreeEntry:
    path: str
    kind: str
    oid: str
    size: int | None

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


@dataclass(frozen=True)
class BitbucketTreeListing:
    entries: tuple[BitbucketTreeEntry, ...]
    complete: bool


class BitbucketRepositoryTree:
    def __init__(
        self,
        *,
        http: BitbucketRunHttp,
        expected_input: RunVcsInput,
        vcs: HttpBitbucketVcsProvider,
        checkpoint: RunProviderCheckpoint,
    ) -> None:
        if (
            expected_input.provider != "bitbucket"
            or expected_input.locator.repository_uuid != http.target.repository_uuid
            or expected_input.repository_id != http.target.repository_id
        ):
            raise ValueError("Bitbucket tree origin mismatch")
        self._http = http
        self.expected_input = expected_input
        self._vcs = vcs
        self._checkpoint = checkpoint
        self._directories: dict[str, str] = {}
        self._files: dict[str, tuple[UUID, str, str]] = {}

    async def checkpoint(self) -> None:
        await self._checkpoint()

    async def _repository(self, revision: str) -> UUID:
        await self.checkpoint()
        if _SHA.fullmatch(revision) is None:
            raise RunCancelled("superseded")
        pull_request = await self._vcs.get_pull_request(
            self.expected_input.locator, checkpoint=self._checkpoint
        )
        if (
            pull_request.head_sha != self.expected_input.head_sha
            or pull_request.base_sha != self.expected_input.base_sha
        ):
            raise RunCancelled("superseded")
        source_uuid = await self._vcs.source_repository_uuid(checkpoint=self._checkpoint)
        if revision == self.expected_input.head_sha:
            return source_uuid
        if revision == self.expected_input.base_sha:
            return self._http.target.repository_uuid
        merge_base = await self._vcs.get_merge_base(pull_request, checkpoint=self._checkpoint)
        if revision != merge_base:
            raise RunCancelled("superseded")
        return self._http.target.repository_uuid

    @staticmethod
    def _entry(raw: object, directory: str, revision: str, repository: UUID) -> BitbucketTreeEntry:
        if not isinstance(raw, dict):
            raise BitbucketRunHttpError("invalid_response")
        path = raw.get("path")
        commit = raw.get("commit")
        if not isinstance(path, str) or not isinstance(commit, dict):
            raise BitbucketRunHttpError("invalid_response")
        try:
            path = _path(path)
        except ValueError:
            raise BitbucketRunHttpError("invalid_response") from None
        if (
            commit.get("hash") != revision
            or (directory and not path.startswith(f"{directory}/"))
            or "/" in path.removeprefix(f"{directory}/" if directory else "")
        ):
            raise BitbucketRunHttpError("invalid_response")
        kind = raw.get("type")
        attributes = raw.get("attributes", [])
        if not isinstance(attributes, list) or not all(
            isinstance(item, str) for item in attributes
        ):
            raise BitbucketRunHttpError("invalid_response")
        if kind == "commit_directory":
            if raw.get("size") is not None:
                raise BitbucketRunHttpError("invalid_response")
            mapped = "directory"
            size = None
        elif kind == "commit_file":
            size = raw.get("size")
            if type(size) is not int or not 0 <= size <= 1_000_000_000:
                raise BitbucketRunHttpError("invalid_response")
            mapped = "symlink" if "link" in attributes else "file"
        else:
            raise BitbucketRunHttpError("invalid_response")
        oid = sha256(f"bitbucket:{repository}:{revision}:{path}".encode()).hexdigest()
        return BitbucketTreeEntry(path, mapped, oid, size)

    @staticmethod
    def _next(raw: object, endpoint: str) -> dict[str, str] | None:
        if raw is None:
            return None
        if not isinstance(raw, str) or len(raw) > 2048:
            raise BitbucketRunHttpError("invalid_response")
        try:
            parsed = urlsplit(raw)
            query = parse_qs(
                parsed.query, keep_blank_values=True, strict_parsing=True, max_num_fields=4
            )
        except ValueError:
            raise BitbucketRunHttpError("invalid_response") from None
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.bitbucket.org"
            or unquote(parsed.path) != unquote(endpoint)
            or parsed.fragment
            or not set(query).issubset({"pagelen", "page", "cursor"})
            or ("page" in query) == ("cursor" in query)
            or any(len(values) != 1 for values in query.values())
        ):
            raise BitbucketRunHttpError("invalid_response")
        params = {key: values[0] for key, values in query.items()}
        if any(
            not value
            or len(value) > 256
            or any(ord(char) < 33 or ord(char) > 126 for char in value)
            for value in params.values()
        ):
            raise BitbucketRunHttpError("invalid_response")
        pagelen = params.get("pagelen")
        if pagelen is not None and (not pagelen.isdecimal() or not 1 <= int(pagelen) <= _PAGE_SIZE):
            raise BitbucketRunHttpError("invalid_response")
        return params

    async def directory(self, revision: str, path: str = "") -> tuple[BitbucketTreeEntry, ...]:
        if path:
            try:
                path = _path(path)
            except ValueError:
                raise BitbucketRunHttpError("invalid_response") from None
        repository = await self._repository(revision)
        # Bitbucket's directory self links end in '/'; never depend on a 301 here.
        suffix = f"src/{revision}/" + (f"{quote(path, safe='/')}/" if path else "")
        endpoint = f"/2.0/repositories/%7B%7D/%7B{repository}%7D/{suffix}"
        entries: list[BitbucketTreeEntry] = []
        total: int | None = None
        params = {"pagelen": str(_PAGE_SIZE), "page": "1"}
        seen_params: set[tuple[tuple[str, str], ...]] = set()
        for _ in range(_MAX_PAGES):
            page_key = tuple(sorted(params.items()))
            if page_key in seen_params:
                raise BitbucketRunHttpError("invalid_response")
            seen_params.add(page_key)
            await self.checkpoint()
            raw = await self._http.get_json(repository, suffix, params=params)
            await self.checkpoint()
            if not isinstance(raw, dict):
                raise BitbucketRunHttpError("invalid_response")
            values = raw.get("values")
            size = raw.get("size")
            page = raw.get("page")
            pagelen = raw.get("pagelen")
            if (
                not isinstance(values, list)
                or len(values) > _PAGE_SIZE
                or (page is not None and (type(page) is not int or page < 1))
                or (
                    pagelen is not None
                    and (type(pagelen) is not int or not 1 <= pagelen <= _PAGE_SIZE)
                )
                or (pagelen is not None and len(values) > pagelen)
                or (size is not None and (type(size) is not int or not 0 <= size <= _MAX_ENTRIES))
            ):
                raise BitbucketRunHttpError("invalid_response")
            if size is not None and total is None:
                total = size
            if size is not None and size != total:
                raise BitbucketRunHttpError("invalid_response")
            entries.extend(self._entry(item, path, revision, repository) for item in values)
            if len(entries) > _MAX_ENTRIES or (total is not None and len(entries) > total):
                raise BitbucketRunHttpError("invalid_response")
            next_params = self._next(raw.get("next"), endpoint)
            if next_params is None:
                if total is not None and len(entries) != total:
                    raise BitbucketRunHttpError("invalid_response")
                break
            params = next_params
        else:
            raise BitbucketRunHttpError("budget_exhausted")
        if len({entry.path for entry in entries}) != len(entries):
            raise BitbucketRunHttpError("invalid_response")
        await self.checkpoint()
        for entry in entries:
            if entry.kind == "directory":
                self._directories[entry.oid] = entry.path
            else:
                self._files[entry.oid] = (repository, revision, entry.path)
        return tuple(entries)

    def directory_path(self, oid: str) -> str:
        try:
            return self._directories[oid]
        except KeyError:
            raise BitbucketRunHttpError("invalid_response") from None

    async def walk(self, revision: str) -> BitbucketTreeListing:
        paths = [""]
        entries: list[BitbucketTreeEntry] = []
        try:
            for path in paths:
                if (
                    len(paths) > _MAX_DIRECTORIES
                    or len(entries) > _MAX_ENTRIES
                    or path.count("/") + bool(path) > _MAX_DEPTH
                ):
                    raise BitbucketRunHttpError("budget_exhausted")
                children = await self.directory(revision, path)
                entries.extend(children)
                paths.extend(item.path for item in children if item.kind == "directory")
            if len(entries) > _MAX_ENTRIES:
                raise BitbucketRunHttpError("budget_exhausted")
        except BitbucketRunHttpError as error:
            if error.kind in {"rate_limited", "request_budget_exceeded"}:
                raise
            return BitbucketTreeListing(tuple(entries), False)
        await self._vcs.verify_current(checkpoint=self._checkpoint)
        return BitbucketTreeListing(tuple(entries), True)

    async def read_file(self, oid: str, *, max_bytes: int) -> bytes:
        known = self._files.get(oid)
        if known is None:
            raise BitbucketRunHttpError("invalid_response")
        repository, revision, path = known
        await self.checkpoint()
        raw = await self._http.get_bytes(
            repository, f"src/{revision}/{quote(path, safe='/')}", max_bytes=max_bytes
        )
        await self.checkpoint()
        await self.checkpoint()
        return raw

    async def read_blob(self, oid: str, *, max_bytes: int) -> bytes:
        if oid in self._files:
            return await self.read_file(oid, max_bytes=max_bytes)
        # Diff snapshots use a separate opaque id. Resolve only ids registered
        # by the pinned VCS adapter; arbitrary ids fail closed there.
        await self.checkpoint()
        return await self._vcs.get_blob(
            self.expected_input.locator, oid, checkpoint=self._checkpoint
        )
