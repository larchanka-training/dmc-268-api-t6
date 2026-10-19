"""One attempt's shared GitLab tree discovery and immutable byte reads."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from functools import partial
from types import MappingProxyType
from typing import Literal

from sqlalchemy.exc import DisconnectionError, InterfaceError, OperationalError
from sqlalchemy.exc import TimeoutError as SqlAlchemyTimeoutError

from app.modules.integrations.gitlab.application.request_credentials import (
    GitLabCredentialUnavailable,
    GitLabRequestUnavailable,
)
from app.modules.integrations.gitlab.infrastructure.run_http import (
    GitLabCanonicalMergeRequest,
    GitLabFileMetadata,
    GitLabRunHttp,
    GitLabRunHttpError,
    GitLabTreeEntry,
    GitLabTreeResource,
)
from app.modules.reviews.application.check_runs import RunProviderCheckpoint
from app.modules.reviews.application.process_run import RunVcsInput
from app.modules.reviews.application.run_failures import RequiredSourceUnavailable


@dataclass(frozen=True)
class GitLabRepositoryListing:
    revision: str
    side: Literal["target", "head"]
    entries: tuple[GitLabTreeEntry, ...]
    sizes: Mapping[str, int]
    listing_complete: bool
    sizes_complete: bool
    incomplete_reasons: tuple[str, ...] = ()


class GitLabRepositoryTree:
    """Explicitly shared by sources; owns no transport, session or renewed budget."""

    def __init__(self, *, http: GitLabRunHttp, checkpoint: RunProviderCheckpoint) -> None:
        self._http = http
        self._checkpoint = checkpoint
        self._canonical: GitLabCanonicalMergeRequest | None = None
        self._listings: dict[tuple[str, str, str, bool], GitLabRepositoryListing] = {}
        self._raw: dict[str, bytes] = {}
        self._raw_bytes = 0

    @property
    def expected_input(self) -> RunVcsInput:
        return self._http.expected_input

    async def checkpoint(self) -> None:
        try:
            await self._checkpoint()
            await self._http.checkpoint()
        except (
            OperationalError,
            InterfaceError,
            DisconnectionError,
            SqlAlchemyTimeoutError,
            OSError,
            GitLabCredentialUnavailable,
            GitLabRequestUnavailable,
        ):
            raise RequiredSourceUnavailable("diff_fetch_failed") from None

    async def _read[T](self, operation: Callable[[], Awaitable[T]]) -> T:
        await self.checkpoint()
        try:
            value = await operation()
        except GitLabRunHttpError:
            await self.checkpoint()
            raise
        await self.checkpoint()
        return value

    async def canonical(self) -> GitLabCanonicalMergeRequest:
        await self.checkpoint()
        if self._canonical is None:
            self._canonical = await self._read(self._http.get_canonical_merge_request)
        return self._canonical

    async def tree(
        self,
        revision: str,
        *,
        side: Literal["target", "head"] = "target",
        path: str = "",
        recursive: bool = True,
        sizes: bool = False,
    ) -> GitLabRepositoryListing:
        await self.checkpoint()
        await self.canonical()
        key = (side, revision, path, recursive)
        listing = self._listings.get(key)
        if listing is None:
            entries: list[GitLabTreeEntry] = []
            names: set[str] = set()
            cursors: set[str] = set()
            token: str | None = None
            complete = False
            for _ in range(512):
                page = await self._read(
                    partial(
                        self._http.get_tree_page,
                        GitLabTreeResource(
                            revision, path=path, recursive=recursive, page_token=token, side=side
                        ),
                    )
                )
                for entry in page.entries:
                    if entry.path in names:
                        raise GitLabRunHttpError("invalid_response")
                    names.add(entry.path)
                    entries.append(entry)
                if page.terminal:
                    complete = True
                    break
                if page.next_page_token is None:
                    break
                if page.next_page_token in cursors:
                    raise GitLabRunHttpError("invalid_response")
                token = page.next_page_token
                cursors.add(token)
            regular = tuple(entry for entry in entries if entry.kind == "file")
            listing = GitLabRepositoryListing(
                revision,
                side,
                tuple(entries),
                MappingProxyType({}),
                complete,
                not regular,
                () if complete else ("listing_incomplete",),
            )
            if listing.listing_complete:
                self._listings[key] = listing
        if not sizes or listing.sizes_complete or not listing.listing_complete:
            return listing
        known_sizes = dict(listing.sizes)
        reasons: tuple[str, ...] = ()
        for entry in sorted(listing.entries, key=lambda entry: entry.path):
            if entry.kind != "file" or entry.path in known_sizes:
                continue
            try:
                metadata = await self.file_metadata(
                    entry.path, revision, side=side, expected_oid=entry.oid
                )
            except GitLabRunHttpError as error:
                reasons = ("sizes_incomplete",)
                if error.kind == "budget_exhausted":
                    reasons += ("budget_exhausted",)
                    break
                continue
            known_sizes[entry.path] = metadata.size
        complete_sizes = all(
            entry.path in known_sizes for entry in listing.entries if entry.kind == "file"
        )
        listing = replace(
            listing,
            sizes=MappingProxyType(known_sizes),
            sizes_complete=complete_sizes,
            incomplete_reasons=reasons,
        )
        self._listings[key] = listing
        return listing

    async def directory_path(
        self, oid: str, *, revision: str, side: Literal["target", "head"] = "target"
    ) -> str:
        return await self._read(partial(self._http.tree_path, oid, revision, side=side))

    async def file_metadata(
        self, path: str, revision: str, *, side: Literal["target", "head"], expected_oid: str
    ) -> GitLabFileMetadata:
        value = await self._read(partial(self._http.file_metadata, path, revision, side=side))
        if value.blob_oid != expected_oid:
            raise GitLabRunHttpError("invalid_response")
        return value

    async def read_blob(self, oid: str, *, max_bytes: int = 1048576) -> bytes:
        await self.checkpoint()
        if type(max_bytes) is not int or not 0 < max_bytes <= 1048576:
            raise ValueError("Invalid GitLab source byte budget")
        cached = self._raw.get(oid)
        if cached is not None:
            if len(cached) > max_bytes:
                raise GitLabRunHttpError("body_too_large")
            return cached
        value = await self._read(partial(self._http.get_blob, oid, max_bytes=max_bytes))
        if self._raw_bytes + len(value) > 67108864:
            raise GitLabRunHttpError("budget_exhausted")
        self._raw[oid] = value
        self._raw_bytes += len(value)
        return value
