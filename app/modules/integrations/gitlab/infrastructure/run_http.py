"""Finite target-only HTTP reads; per-attempt credentials and transport ownership."""

from __future__ import annotations

import asyncio
import json
import math
import re
import ssl
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from email.utils import parsedate_to_datetime
from types import MappingProxyType, TracebackType
from typing import Any, Literal, Self
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from app.modules.integrations.gitlab.application.manage_connection import (
    GitLabManagementError,
    GitLabOriginPolicy,
)
from app.modules.integrations.gitlab.infrastructure.response_body import (
    GitLabResponseBodyError,
    read_response_body,
    validate_response_headers,
)
from app.modules.integrations.gitlab.infrastructure.run_credentials import (
    FetchCode,
    GitLabRunCredentialPort,
)
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RequiredSourceUnavailable, RunCancelled

type JsonValue = None | bool | int | Decimal | str | list[JsonValue] | dict[str, JsonValue]
type HttpFaultKind = Literal[
    "access_denied",
    "not_found",
    "rate_limited",
    "unavailable",
    "invalid_response",
    "body_too_large",
    "budget_exhausted",
]


class GitLabRunHttpError(Exception):
    """Ordinary provider fault, distinct from required admission/control failures."""

    def __init__(
        self,
        kind: HttpFaultKind,
        *,
        status_code: int | None = None,
        retry_after: timedelta | None = None,
    ) -> None:
        self.kind = kind
        self.status_code = status_code
        self.retry_after = retry_after
        super().__init__(f"GitLab read failed ({kind})")


@dataclass(frozen=True)
class GitLabJsonResponse:
    data: JsonValue = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class GitLabPublicationResponse:
    status_code: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(repr=False)

    def decode_json(self) -> JsonValue:
        return _json(self.body)

    @property
    def retry_after(self) -> timedelta | None:
        return _retry_after(self.headers.get("retry-after"))


@dataclass(frozen=True)
class GitLabHeadResponse:
    headers: Mapping[str, str] = field(repr=False)


@dataclass(frozen=True)
class GitLabTreeEntry:
    path: str
    name: str
    kind: Literal["file", "directory", "symlink", "submodule"]
    mode: str
    oid: str


@dataclass(frozen=True)
class GitLabTreePage:
    entries: tuple[GitLabTreeEntry, ...]
    next_page_token: str | None
    terminal: bool


@dataclass(frozen=True)
class GitLabFileMetadata:
    path: str
    revision: str
    side: Literal["target", "head"]
    blob_oid: str
    size: int


@dataclass(frozen=True)
class GitLabCanonicalMergeRequest:
    id: int
    iid: int
    target_project_id: int
    source_project_id: int
    head_sha: str
    start_sha: str
    base_sha: str
    data: JsonValue = field(repr=False, compare=False)


@dataclass(frozen=True)
class GitLabMergeRequestResource:
    pass


@dataclass(frozen=True)
class GitLabDiffVersionsResource:
    page: int
    per_page: int = 100


@dataclass(frozen=True)
class GitLabDiffVersionResource:
    version_id: int


@dataclass(frozen=True)
class GitLabCommitsResource:
    page: int
    per_page: int = 10


@dataclass(frozen=True)
class GitLabTreeResource:
    revision: str
    path: str = ""
    recursive: bool = True
    page_token: str | None = None
    per_page: int = 100
    side: Literal["target", "head"] = "target"


type GitLabJsonResource = (
    GitLabMergeRequestResource
    | GitLabDiffVersionsResource
    | GitLabDiffVersionResource
    | GitLabCommitsResource
    | GitLabTreeResource
)
_PAGINATION = frozenset({"link", "x-next-page", "x-page", "x-per-page", "x-total", "x-total-pages"})
_HEAD = frozenset(
    {
        "x-gitlab-blob-id",
        "x-gitlab-size",
        "x-gitlab-ref",
        "x-gitlab-commit-id",
        "x-gitlab-last-commit-id",
        "x-gitlab-file-path",
        "content-length",
    }
)


def _positive(value: int, maximum: int = 9223372036854775807) -> None:
    if type(value) is not int or not 0 < value <= maximum:
        raise ValueError("Invalid GitLab resource number")


def _oid(value: str) -> str:
    if (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[a-fA-F0-9]{40}|[a-fA-F0-9]{64})", value) is None
    ):
        raise ValueError("Invalid GitLab object identity")
    return value.lower()


def _path(value: str, *, root: bool = False) -> str:
    if not isinstance(value, str) or (
        not value
        and not root
        or any(
            ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF for char in value
        )
        or "\\" in value
        or value
        and any(part in {"", ".", ".."} for part in value.split("/"))
        or len(value.encode("utf-8")) > 4096
    ):
        raise ValueError("Invalid GitLab repository path")
    return value


def _retry_after(value: str | None) -> timedelta | None:
    if value is None or len(value) > 128:
        return None
    if re.fullmatch(r"[0-9]{1,10}", value):
        return timedelta(seconds=min(int(value), 86400))
    try:
        date = parsedate_to_datetime(value)
        if date.tzinfo is None:
            return None
        return timedelta(seconds=max(0, min(86400, (date - datetime.now(UTC)).total_seconds())))
    except (ValueError, TypeError, OverflowError):
        return None


def _json(body: bytes) -> JsonValue:
    def pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def integer(value: str) -> int:
        if len(value.lstrip("-")) > 64:
            raise ValueError
        return int(value)

    def decimal(value: str) -> Decimal:
        if len(value) > 128:
            raise ValueError
        exponent = re.search(r"[eE]([+-]?[0-9]+)$", value)
        if exponent and abs(int(exponent[1])) > 308:
            raise ValueError
        result = Decimal(value)
        if not result.is_finite():
            raise ValueError
        return result

    def constant(value: str) -> None:
        raise ValueError

    try:
        source = body.decode("utf-8")
        # Bound nesting before the stdlib decoder recursively materializes objects.
        depth, string, escape = 0, False, False
        for char in source:
            if string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    string = False
            elif char == '"':
                string = True
            elif char in "[{":
                depth += 1
                if depth > 64:
                    raise ValueError
            elif char in "]}":
                depth -= 1
        value: JsonValue = json.loads(
            source,
            object_pairs_hook=pairs,
            parse_int=integer,
            parse_float=decimal,
            parse_constant=constant,
        )
        pending = [value]
        nodes = 0
        while pending:
            current = pending.pop()
            nodes += 1
            if nodes > 200000:
                raise ValueError
            if isinstance(current, dict):
                pending.extend(current.keys())
                pending.extend(current.values())
            elif isinstance(current, list):
                pending.extend(current)
            elif isinstance(current, str) and any(
                ord(char) == 0 or 0xD800 <= ord(char) <= 0xDFFF for char in current
            ):
                raise ValueError
        return value
    except (ValueError, UnicodeError, RecursionError, InvalidOperation):
        raise GitLabRunHttpError("invalid_response") from None


class GitLabRunHttp:
    """An attempt owns this context, including any explicitly injected transport.

    Each call is one send; callers own bounded pagination and existing run retries.
    Only target repository reads at persisted head/start are supported here. Canonical
    fork/merge-base provenance is a separately counted consuming adapter extension.
    """

    def __init__(
        self,
        *,
        credentials: GitLabRunCredentialPort,
        origin_policy: GitLabOriginPolicy,
        ssl_context: ssl.SSLContext | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if ssl_context is not None and (
            ssl_context.verify_mode != ssl.CERT_REQUIRED or not ssl_context.check_hostname
        ):
            raise ValueError("GitLab HTTP requires verified TLS")
        self._credentials = credentials
        self._policy = origin_policy
        self._client = httpx.AsyncClient(
            verify=ssl_context if ssl_context is not None else True,
            transport=transport,
            trust_env=False,
            follow_redirects=False,
            timeout=httpx.Timeout(10),
        )
        self._entered = False
        self._closed = False
        self._sends = 0
        self._bytes = 0
        self._instance_origin: str | None = None
        self._canonical: GitLabCanonicalMergeRequest | None = None
        self._blob_provenance: dict[str, tuple[int, str, int | None]] = {}
        self._file_metadata: dict[tuple[str, str, str], GitLabFileMetadata] = {}
        self._tree_entries: dict[tuple[str, str, str], GitLabTreeEntry] = {}
        self._tree_paths: dict[tuple[str, str, str], set[str]] = {}

    async def __aenter__(self) -> Self:
        if self._entered or self._closed:
            raise RuntimeError("GitLab HTTP context is not reusable")
        # Client/transport are already owned before any awaited startup step.
        self._entered = True
        try:
            await self._client.__aenter__()
        except BaseException:
            await self._close(primary=True)
            raise
        return self

    async def _close(self, *, primary: bool) -> None:
        self._closed = True
        try:
            async with asyncio.timeout(1):
                await self._client.aclose()
        except BaseException:
            if not primary:
                raise

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self._close(primary=exc is not None)

    @property
    def expected_input(self) -> RunVcsInput:
        return self._credentials.expected_input

    @property
    def instance_origin(self) -> str:
        if self._instance_origin is None:
            raise RuntimeError("GitLab HTTP origin is not bound")
        return self._instance_origin

    async def checkpoint(self, *, error_code: FetchCode = "diff_fetch_failed") -> None:
        """Metadata-only authority fence for immutable cached attempt results."""
        if not self._entered or self._closed:
            raise RuntimeError("GitLab HTTP requires an open context")
        if error_code not in {"diff_fetch_failed", "commit_messages_fetch_failed"}:
            raise ValueError("Invalid GitLab source failure code")
        await self._credentials.checkpoint(error_code=error_code)

    async def get_canonical_merge_request(
        self,
        *,
        error_code: FetchCode = "diff_fetch_failed",
        max_total_bytes: int = 67108864,
    ) -> GitLabCanonicalMergeRequest:
        response = await self.get_json(
            GitLabMergeRequestResource(),
            max_bytes=1048576,
            error_code=error_code,
            max_total_bytes=max_total_bytes,
        )
        data = response.data
        if not isinstance(data, dict):
            raise GitLabRunHttpError("invalid_response")
        state = data.get("state")
        if not isinstance(state, str):
            raise GitLabRunHttpError("invalid_response")
        if state in {"closed", "merged"}:
            raise RunCancelled("pr_closed")
        if state == "locked":
            raise GitLabRunHttpError("unavailable")
        if state != "opened":
            raise GitLabRunHttpError("invalid_response")
        values = [
            data.get(key)
            for key in ("id", "iid", "project_id", "target_project_id", "source_project_id")
        ]
        if any(type(value) is not int or not 0 < value <= 9223372036854775807 for value in values):
            raise GitLabRunHttpError("invalid_response")
        if data["iid"] != self.expected_input.locator.number or (
            data["project_id"] != self.expected_input.repository_external_id
            or data["target_project_id"] != self.expected_input.repository_external_id
        ):
            raise GitLabRunHttpError("invalid_response")
        refs = data.get("diff_refs")
        if refs is None:
            raise GitLabRunHttpError("unavailable")
        if not isinstance(refs, dict):
            raise GitLabRunHttpError("invalid_response")
        parsed: list[str] = []
        for key in ("head_sha", "start_sha", "base_sha"):
            value = refs.get(key)
            if value is None or value == "":
                raise GitLabRunHttpError("unavailable")
            if not isinstance(value, str):
                raise GitLabRunHttpError("invalid_response")
            try:
                parsed.append(_oid(value))
            except ValueError:
                raise GitLabRunHttpError("invalid_response") from None
        if parsed[:2] != [
            self.expected_input.head_sha.lower(),
            self.expected_input.base_sha.lower(),
        ]:
            raise RunCancelled("superseded")
        # Identity fields were exact ints above; keep decoding local, not a generic grant.
        identity = data["id"]
        iid = data["iid"]
        target = data["target_project_id"]
        source = data["source_project_id"]
        assert isinstance(identity, int) and isinstance(iid, int)
        assert isinstance(target, int) and isinstance(source, int)
        snapshot = GitLabCanonicalMergeRequest(
            identity, iid, target, source, parsed[0], parsed[1], parsed[2], data
        )
        if self._canonical is not None and snapshot != self._canonical:
            raise RunCancelled("superseded")
        self._canonical = snapshot
        return snapshot

    def _ref(self, value: str, side: Literal["target", "head"] = "target") -> str:
        ref = _oid(value)
        if side not in {"target", "head"}:
            raise ValueError("Invalid GitLab revision side")
        if self._canonical is None:
            if side != "target":
                raise GitLabRunHttpError("invalid_response")
            allowed = {self.expected_input.head_sha.lower(), self.expected_input.base_sha.lower()}
        else:
            allowed = (
                {self._canonical.head_sha}
                if side == "head"
                else {self._canonical.start_sha, self._canonical.base_sha}
            )
        if ref not in allowed:
            raise ValueError("GitLab revision is outside the persisted run")
        return ref

    def _revision_project(self, revision: str, side: Literal["target", "head"]) -> int:
        self._ref(revision, side)
        if side == "head":
            assert self._canonical is not None
            return self._canonical.source_project_id
        project = self.expected_input.repository_external_id
        assert project is not None
        return project

    def _route(self, resource: GitLabJsonResource) -> tuple[str, dict[str, str | int], int]:
        iid = self._credentials.expected_input.locator.number
        _positive(iid)
        mr = f"merge_requests/{iid}"
        if isinstance(resource, GitLabMergeRequestResource):
            return mr, {}, 1048576
        if isinstance(resource, GitLabDiffVersionsResource):
            _positive(resource.page, 5)
            _positive(resource.per_page, 100)
            return f"{mr}/versions", {"page": resource.page, "per_page": resource.per_page}, 1048576
        if isinstance(resource, GitLabDiffVersionResource):
            _positive(resource.version_id)
            return f"{mr}/versions/{resource.version_id}", {}, 8388608
        if isinstance(resource, GitLabCommitsResource):
            _positive(resource.page, 2)
            if type(resource.per_page) is not int or resource.per_page != 10:
                raise ValueError("Invalid GitLab commit page")
            return f"{mr}/commits", {"page": resource.page, "per_page": 10}, 1048576
        if isinstance(resource, GitLabTreeResource):
            _positive(resource.per_page, 100)
            if type(resource.recursive) is not bool:
                raise ValueError("Invalid GitLab tree traversal")
            params: dict[str, str | int] = {
                "ref": self._ref(resource.revision, resource.side),
                "path": _path(resource.path, root=True),
                "recursive": str(resource.recursive).lower(),
                "per_page": resource.per_page,
                "pagination": "keyset",
            }
            if resource.page_token is not None:
                token = resource.page_token
                if (
                    not isinstance(token, str)
                    or not 1 <= len(token) <= 2048
                    or any(not 33 <= ord(char) <= 126 for char in token)
                ):
                    raise ValueError("Invalid GitLab page token")
                params["page_token"] = token
            return "repository/tree", params, 1048576
        raise ValueError("Unsupported GitLab resource")

    @staticmethod
    def _limits(max_bytes: int, maximum: int, timeout_seconds: float) -> None:
        if type(max_bytes) is not int or not 0 < max_bytes <= maximum:
            raise ValueError("Invalid GitLab response budget")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 0 < timeout_seconds <= 10
        ):
            raise ValueError("Invalid GitLab request timeout")

    async def get_json(
        self,
        resource: GitLabJsonResource,
        *,
        max_bytes: int,
        timeout_seconds: float = 10,
        error_code: FetchCode = "diff_fetch_failed",
        max_total_bytes: int = 67108864,
    ) -> GitLabJsonResponse:
        route, params, maximum = self._route(resource)
        self._limits(max_bytes, maximum, timeout_seconds)
        project = (
            self._revision_project(resource.revision, resource.side)
            if isinstance(resource, GitLabTreeResource)
            else None
        )
        result = await self._request(
            "GET",
            route,
            params,
            "json",
            max_bytes,
            timeout_seconds,
            error_code,
            max_total_bytes=max_total_bytes,
            repository_project_id=project,
        )
        assert isinstance(result, GitLabJsonResponse)
        return result

    async def get_publication_json(
        self,
        kind: Literal["versions", "discussions"],
        *,
        page: int | None = None,
        per_page: int | None = None,
    ) -> GitLabPublicationResponse:
        if kind not in {"versions", "discussions"}:
            raise ValueError("Invalid GitLab publication resource")
        params: dict[str, str | int] = {}
        if page is not None:
            _positive(page)
            params["page"] = page
        if per_page is not None:
            _positive(per_page, 100)
            params["per_page"] = per_page
        result = await self._request(
            "GET",
            f"merge_requests/{self.expected_input.locator.number}/{kind}",
            params,
            "publication",
            8388608,
            10,
            "diff_fetch_failed",
        )
        assert isinstance(result, GitLabPublicationResponse)
        return result

    async def post_discussion(self, payload: Mapping[str, JsonValue]) -> GitLabPublicationResponse:
        try:
            body = json.dumps(
                dict(payload), ensure_ascii=False, allow_nan=False, separators=(",", ":")
            ).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError, OverflowError):
            raise GitLabRunHttpError("invalid_response") from None
        if len(body) > 1048576:
            raise GitLabRunHttpError("body_too_large")
        _json(body)
        result = await self._request(
            "POST",
            f"merge_requests/{self.expected_input.locator.number}/discussions",
            {},
            "publication",
            8388608,
            10,
            "diff_fetch_failed",
            request_body=body,
        )
        assert isinstance(result, GitLabPublicationResponse)
        return result

    async def get_tree_page(
        self,
        resource: GitLabTreeResource,
        *,
        max_bytes: int = 1048576,
        timeout_seconds: float = 10,
        error_code: FetchCode = "diff_fetch_failed",
        max_total_bytes: int = 67108864,
    ) -> GitLabTreePage:
        if self._canonical is None:
            raise GitLabRunHttpError("invalid_response")
        if resource.per_page != 100:
            raise ValueError("GitLab tree pages require the bounded page size")
        response = await self.get_json(
            resource,
            max_bytes=max_bytes,
            timeout_seconds=timeout_seconds,
            error_code=error_code,
            max_total_bytes=max_total_bytes,
        )
        if not isinstance(response.data, list) or len(response.data) > resource.per_page:
            raise GitLabRunHttpError("invalid_response")
        revision = self._ref(resource.revision, resource.side)
        project = self._revision_project(revision, resource.side)
        entries: list[GitLabTreeEntry] = []
        paths: set[str] = set()
        kinds: dict[tuple[str, str], Literal["file", "directory", "symlink", "submodule"]] = {
            ("tree", "040000"): "directory",
            ("blob", "100644"): "file",
            ("blob", "100755"): "file",
            ("blob", "120000"): "symlink",
            ("commit", "160000"): "submodule",
        }
        try:
            for raw in response.data:
                if not isinstance(raw, dict):
                    raise ValueError
                path, name, kind, mode, oid = (
                    raw.get(key) for key in ("path", "name", "type", "mode", "id")
                )
                if not all(isinstance(value, str) for value in (path, name, kind, mode, oid)):
                    raise ValueError
                assert isinstance(path, str) and isinstance(name, str)
                assert isinstance(kind, str) and isinstance(mode, str) and isinstance(oid, str)
                path = _path(path)
                if name != path.rsplit("/", 1)[-1] or path in paths:
                    raise ValueError
                prefix = resource.path + "/" if resource.path else ""
                if not path.startswith(prefix) or (
                    not resource.recursive and "/" in path[len(prefix) :]
                ):
                    raise ValueError
                entry_kind = kinds.get((kind, mode))
                if entry_kind is None:
                    raise ValueError
                entry = GitLabTreeEntry(path, name, entry_kind, mode, _oid(oid))
                prior = self._tree_entries.get((resource.side, revision, path))
                if prior is not None and prior != entry:
                    raise ValueError
                entries.append(entry)
                paths.add(path)
        except ValueError:
            raise GitLabRunHttpError("invalid_response") from None
        known_entries = {
            path: entry
            for (side, ref, path), entry in self._tree_entries.items()
            if side == resource.side and ref == revision
        }
        known_entries.update((entry.path, entry) for entry in entries)
        for entry in known_entries.values():
            parts = entry.path.split("/")
            for index in range(1, len(parts)):
                parent = known_entries.get("/".join(parts[:index]))
                if parent is not None and parent.kind != "directory":
                    raise GitLabRunHttpError("invalid_response")
        next_token = self._tree_next(response.headers, resource, project)
        if next_token is not None and (not entries or next_token == resource.page_token):
            raise GitLabRunHttpError("invalid_response")
        # No caller DTO can populate these maps: only fully validated actual response bytes.
        for entry in entries:
            self._tree_entries[(resource.side, revision, entry.path)] = entry
            if entry.kind == "file":
                self._blob_provenance.setdefault(entry.oid, (project, revision, None))
            elif entry.kind == "directory":
                self._tree_paths.setdefault((resource.side, revision, entry.oid), set()).add(
                    entry.path
                )
        return GitLabTreePage(tuple(entries), next_token, next_token is None and len(entries) < 100)

    def _tree_next(
        self, headers: Mapping[str, str], resource: GitLabTreeResource, project: int
    ) -> str | None:
        link = headers.get("link")
        if link is None:
            return None
        next_token: str | None = None
        expected = {
            "ref": [resource.revision.lower()],
            "path": [resource.path],
            "recursive": [str(resource.recursive).lower()],
            "per_page": ["100"],
            "pagination": ["keyset"],
        }
        for item in link.split(","):
            match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="(next|prev|first|last)"\s*', item)
            if match is None:
                raise GitLabRunHttpError("invalid_response")
            try:
                url = urlsplit(match[1])
                query = parse_qs(url.query, keep_blank_values=True, strict_parsing=True)
            except ValueError:
                raise GitLabRunHttpError("invalid_response") from None
            if (
                url.scheme != "https"
                or url.username
                or url.password
                or url.fragment
                or f"{url.scheme}://{url.netloc}" != self.instance_origin
                or url.path != f"/api/v4/projects/{project}/repository/tree"
                or set(query) - {"page_token"} != set(expected)
                or any(query.get(key) != value for key, value in expected.items())
            ):
                raise GitLabRunHttpError("invalid_response")
            values = query.get("page_token")
            if values is not None and (
                len(values) != 1
                or not 1 <= len(values[0]) <= 2048
                or any(not 33 <= ord(char) <= 126 for char in values[0])
            ):
                raise GitLabRunHttpError("invalid_response")
            if match[2] == "next":
                if values is None or next_token is not None:
                    raise GitLabRunHttpError("invalid_response")
                next_token = values[0]
        return next_token

    async def tree_path(
        self,
        oid: str,
        revision: str,
        *,
        side: Literal["target", "head"] = "target",
    ) -> str:
        await self.checkpoint()
        paths = self._tree_paths.get((side, self._ref(revision, side), _oid(oid)))
        if not paths:
            raise GitLabRunHttpError("invalid_response")
        return min(paths)

    async def file_metadata(
        self,
        path: str,
        revision: str,
        *,
        side: Literal["target", "head"] = "target",
        timeout_seconds: float = 10,
        error_code: FetchCode = "diff_fetch_failed",
        max_total_bytes: int = 67108864,
    ) -> GitLabFileMetadata:
        self._limits(1, 1, timeout_seconds)
        if type(max_total_bytes) is not int or not 0 < max_total_bytes <= 67108864:
            raise ValueError("Invalid GitLab aggregate budget")
        await self.checkpoint(error_code=error_code)
        key = (side, self._ref(revision, side), _path(path))
        cached = self._file_metadata.get(key)
        if cached is not None:
            return cached
        await self.head_file(
            path,
            revision,
            side=side,
            timeout_seconds=timeout_seconds,
            error_code=error_code,
            max_total_bytes=max_total_bytes,
        )
        result = self._file_metadata.get(key)
        if result is None:
            raise GitLabRunHttpError("invalid_response")
        return result

    async def head_file(
        self,
        path: str,
        revision: str,
        *,
        timeout_seconds: float = 10,
        error_code: FetchCode = "diff_fetch_failed",
        side: Literal["target", "head"] = "target",
        max_total_bytes: int = 67108864,
    ) -> GitLabHeadResponse:
        self._limits(1, 1, timeout_seconds)
        route = f"repository/files/{quote(_path(path), safe='')}"
        project = self._revision_project(revision, side)
        result = await self._request(
            "HEAD",
            route,
            {"ref": self._ref(revision, side)},
            "head",
            0,
            timeout_seconds,
            error_code,
            max_total_bytes=max_total_bytes,
            repository_project_id=project,
        )
        assert isinstance(result, GitLabHeadResponse)
        if self._canonical is not None:
            try:
                oid = _oid(result.headers.get("x-gitlab-blob-id", ""))
                ref = _oid(result.headers.get("x-gitlab-ref", ""))
                size_text = result.headers.get("x-gitlab-size", "")
                if re.fullmatch(r"[0-9]{1,19}", size_text) is None:
                    raise ValueError
                size = int(size_text)
                if size > 9223372036854775807 or ref != revision.lower():
                    raise ValueError
            except ValueError:
                raise GitLabRunHttpError("invalid_response") from None
            prior = self._blob_provenance.get(oid)
            if prior is not None and prior[2] is not None and prior[2] != size:
                raise GitLabRunHttpError("invalid_response")
            known = self._tree_entries.get((side, ref, path))
            if known is not None and (known.kind != "file" or known.oid != oid):
                raise GitLabRunHttpError("invalid_response")
            self._blob_provenance[oid] = (project, ref, size)
            self._file_metadata[(side, ref, path)] = GitLabFileMetadata(path, ref, side, oid, size)
        return result

    async def get_blob(
        self,
        blob_oid: str,
        *,
        max_bytes: int,
        timeout_seconds: float = 10,
        error_code: FetchCode = "diff_fetch_failed",
        max_total_bytes: int = 67108864,
    ) -> bytes:
        self._limits(max_bytes, 1048576, timeout_seconds)
        oid = _oid(blob_oid)
        provenance = self._blob_provenance.get(oid)
        if self._canonical is not None and provenance is None:
            raise GitLabRunHttpError("invalid_response")
        result = await self._request(
            "GET",
            f"repository/blobs/{oid}/raw",
            {},
            "raw",
            max_bytes,
            timeout_seconds,
            error_code,
            max_total_bytes=max_total_bytes,
            repository_project_id=None if provenance is None else provenance[0],
        )
        assert isinstance(result, bytes)
        if provenance is not None and provenance[2] is not None and len(result) != provenance[2]:
            raise GitLabRunHttpError("invalid_response")
        return result

    async def _body(
        self, response: httpx.Response, maximum: int, mode: str, max_total_bytes: int
    ) -> bytes:
        try:
            validate_response_headers(response)
        except GitLabResponseBodyError as error:
            raise GitLabRunHttpError(error.kind) from None
        if mode != "publication" and response.status_code != 200:
            status = response.status_code
            kind: HttpFaultKind = (
                "access_denied"
                if status in (401, 403)
                else "not_found"
                if status == 404
                else "rate_limited"
                if status == 429
                else "invalid_response"
                if 300 <= status < 400
                else "unavailable"
            )
            raise GitLabRunHttpError(
                kind,
                status_code=status,
                retry_after=_retry_after(response.headers.get("Retry-After")),
            )

        def charge(size: int) -> None:
            self._bytes += size
            if self._bytes > max_total_bytes:
                raise GitLabRunHttpError("budget_exhausted")

        try:
            return await read_response_body(
                response,
                maximum,
                json_content=mode == "json"
                or mode == "publication"
                and response.status_code in (200, 201),
                head=mode == "head",
                charge=charge,
            )
        except GitLabResponseBodyError as error:
            raise GitLabRunHttpError(error.kind) from None

    @staticmethod
    async def _close_response(response: httpx.Response, *, primary: bool) -> bool:
        try:
            async with asyncio.timeout(1):
                await response.aclose()
        except BaseException:
            if not primary:
                raise
            return False
        return True

    async def _request(
        self,
        method: str,
        route: str,
        params: dict[str, str | int],
        mode: str,
        max_bytes: int,
        timeout_seconds: float,
        error_code: FetchCode,
        *,
        max_total_bytes: int = 67108864,
        repository_project_id: int | None = None,
        request_body: bytes | None = None,
    ) -> GitLabJsonResponse | GitLabHeadResponse | GitLabPublicationResponse | bytes:
        if not self._entered or self._closed:
            raise RuntimeError("GitLab HTTP requires an open context")
        if error_code not in {"diff_fetch_failed", "commit_messages_fetch_failed"}:
            raise ValueError("Invalid GitLab source failure code")
        if type(max_total_bytes) is not int or not 0 < max_total_bytes <= 67108864:
            raise ValueError("Invalid GitLab total response budget")
        target = await self._credentials.bind(error_code=error_code)
        expected = self._credentials.expected_input
        if (
            target.project_id != expected.repository_external_id
            or target.origin_provider_installation_id != expected.origin_provider_installation_id
            or target.repository_id != expected.repository_id
        ):
            raise RunCancelled("installation_unavailable")
        try:
            origin = self._policy.validate(target.instance_origin)
        except GitLabManagementError:
            raise RequiredSourceUnavailable(error_code) from None
        if self._instance_origin is not None and origin != self._instance_origin:
            raise RunCancelled("installation_unavailable")
        self._instance_origin = origin
        if self._sends >= 512 or self._bytes > max_total_bytes:
            raise GitLabRunHttpError("budget_exhausted")
        credential = await self._credentials.credential_for_request(error_code=error_code)
        if credential.target != target:
            raise RunCancelled("installation_unavailable")
        project = target.project_id if repository_project_id is None else repository_project_id
        if project != target.project_id and (
            self._canonical is None or project != self._canonical.source_project_id
        ):
            raise RunCancelled("installation_unavailable")
        request = httpx.Request(
            method,
            f"{origin}/api/v4/projects/{project}/{route}",
            params=params,
            content=request_body,
            headers={
                "PRIVATE-TOKEN": credential.token,
                "Accept-Encoding": "identity",
                "Accept": "application/json"
                if mode in {"json", "publication"}
                else "application/octet-stream",
                **({"Content-Type": "application/json"} if request_body is not None else {}),
            },
            extensions={"timeout": httpx.Timeout(timeout_seconds).as_dict()},
        )
        # Credential admission awaits SQL/codec work: reserve again without an
        # intervening await so concurrent callers cannot overshoot the send cap.
        if self._sends >= 512 or self._bytes > max_total_bytes:
            raise GitLabRunHttpError("budget_exhausted")
        self._sends += 1
        pending: GitLabRunHttpError | None = None
        closed = False
        try:
            async with asyncio.timeout(timeout_seconds):
                response = await self._client.send(
                    request, stream=True, auth=None, follow_redirects=False
                )
                try:
                    body = await self._body(response, max_bytes, mode, max_total_bytes)
                    if mode == "json":
                        data = _json(body)
                        result: (
                            GitLabJsonResponse
                            | GitLabHeadResponse
                            | GitLabPublicationResponse
                            | bytes
                        ) = GitLabJsonResponse(
                            data,
                            MappingProxyType(
                                {k: v for k, v in response.headers.items() if k in _PAGINATION}
                            ),
                        )
                    elif mode == "publication":
                        if response.status_code in (200, 201):
                            _json(body)
                        result = GitLabPublicationResponse(
                            response.status_code,
                            body,
                            MappingProxyType(
                                {
                                    k: v
                                    for k, v in response.headers.items()
                                    if k in _PAGINATION or k == "retry-after"
                                }
                            ),
                        )
                    elif mode == "head":
                        result = GitLabHeadResponse(
                            MappingProxyType(
                                {k: v for k, v in response.headers.items() if k in _HEAD}
                            )
                        )
                    else:
                        result = body
                except BaseException:
                    closed = await self._close_response(response, primary=True)
                    raise
                else:
                    closed = await self._close_response(response, primary=False)
        except GitLabRunHttpError as error:
            pending = error
        except (httpx.HTTPError, TimeoutError):
            raise GitLabRunHttpError("unavailable") from None
        # Completed, closed ordinary provider errors still need a final authority fence.
        # Primary control/programmer/startup/close/I/O failures never reach this point.
        if closed:
            await self._credentials.checkpoint(error_code=error_code)
        if pending is not None:
            raise pending from None
        return result
