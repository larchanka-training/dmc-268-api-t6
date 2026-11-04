"""Use case `BuildRepositoryIndex` against fakes of its ports (#142).

AC5: every mode decision. AC7: no transaction is open during a VCS call. AC8: every code,
and an empty repository without an error. AC12: a CAS miss, a lost lease, the soft cutoff
and a target change. AC3: the skip counters equal literals.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import AsyncIterator, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

import pytest

from app.modules.code_index.application import build_index
from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildOutcome,
    BuildRepositoryIndex,
    BuildStatus,
    FullBuildReason,
    IndexBuildSettings,
    IndexVersions,
    limits_profile_version,
    parser_profile_version,
)
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.application.index_resolution import INDEX_RESOLVER_VERSION
from app.modules.code_index.application.index_store import (
    ClaimRefusal,
    ClaimRefused,
    CodeIndexStore,
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
    IndexCounts,
    IndexedFile,
    IndexedSymbol,
    IndexFence,
    IndexLeaseLost,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.symbol_rows import file_rows, symbol_rows
from app.modules.code_index.application.vcs import (
    Ancestry,
    ArchiveCounters,
    ArchiveEntry,
    ArchiveScan,
    ArchiveUnsafe,
    BranchHead,
    ChangedPath,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    SourceArchive,
    VcsAccessDenied,
    VcsUnavailable,
)
from app.modules.code_index.domain.enums import IndexStatus
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    ParseRequest,
)
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicyDocument
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    IgnorePolicy,
    parse_review_ignore,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_REPO = UUID("00000000-0000-0000-0000-000000000142")
_GENERATION = 3
_OLD = "a" * 40
_HEAD = "c" * 40
_MIB = 1_048_576
_LIMITS = IndexLimits()
_VERSIONS = IndexVersions(
    extractor_version="facts/v2",
    resolver_version=INDEX_RESOLVER_VERSION,
    parser_profile_version="p" * 64,
    limits_profile_version=limits_profile_version(_LIMITS),
)
_DIGEST = parse_review_ignore(None).digest
_PROFILE = _VERSIONS.profile("main", _DIGEST)
_ADMISSION = IndexAdmission(2, 1, 512 * _MIB, 2048 * _MIB)
_FENCE = IndexFence(UUID(int=7), _GENERATION, 5)
_SETTINGS = IndexBuildSettings(admission=_ADMISSION)
_RULES = ".review/rules.md"

_MODELS = b"class User:\n    pass\n"
_VIEWS = b"from app.models import User\n\n\ndef show():\n    return User()\n"
_UTIL = b"def helper():\n    return 1\n"
_CLI = b"from app.util import helper\n\n\ndef main():\n    return helper()\n"


def _blob(content: bytes) -> str:
    """`git hash-object` of `content`."""
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


@dataclass(frozen=True)
class Link:
    """A symlink entry of a fake archive."""

    target: str


Tree = Mapping[str, bytes | Link]
_TREE: Tree = {"app/models.py": _MODELS, "app/views.py": _VIEWS}


@dataclass
class Tracker:
    """The interleaving of transactions, store calls and network calls."""

    open: int = 0
    transactions: int = 0
    log: list[str] = field(default_factory=list)

    def store(self, name: str) -> None:
        assert self.open == 1, f"store call {name} outside one transaction"
        self.log.append(name)

    def network(self, name: str) -> None:
        # AC7: no transaction is open during a VCS call or a wake-up.
        assert self.open == 0, f"{name} inside a transaction"
        self.log.append(name)


def _state(**changes: Any) -> IndexState:
    state = IndexState(
        generation=_GENERATION,
        status=IndexStatus.READY,
        purge_reason=None,
        error_code=None,
        last_error_code=None,
        indexed_commit_sha=_OLD,
        target_commit_sha=None,
        reserved_publication=5,
        published_publication=4,
        floor_publication=1,
        published_at=None,
        last_used_at=None,
        lease_expires_at=None,
        file_count=0,
        symbol_count=0,
        edge_count=0,
        stored_bytes=0,
        pending_bytes=0,
        profile=_PROFILE,
        stats=IndexBuildStats(0, 0, 0),
        updated_at=datetime(2026, 10, 16, tzinfo=UTC),
    )
    return replace(state, **changes)


_NO_INDEX = _state(
    status=IndexStatus.BUILDING, indexed_commit_sha=None, published_publication=0, profile=None
)


@dataclass
class Store:
    tracker: Tracker
    state: IndexState | None
    claim: LeaseClaim | ClaimRefused = LeaseClaim(_FENCE, None)
    # Rows each discard portion deletes; the last value repeats.
    discards: list[int] = field(default_factory=lambda: [0])
    files: dict[str, str] = field(default_factory=dict)
    imports: list[ImportRecord] = field(default_factory=list)
    counts: dict[str, tuple[int, int]] = field(default_factory=dict)
    bytes_per_file: int = 0
    # The stored and live pending bytes of the Workspace's other repositories.
    other_bytes: int = 0
    published: bool = True
    after_publish: IndexState | None = None
    # The state once the job released its lease or failed the index.
    after_release: IndexState | None = None
    lose_lease_at: str | None = None
    written: list[tuple[int, IndexedFile]] = field(default_factory=list)
    publications: list[dict[str, Any]] = field(default_factory=list)
    failures: list[tuple[str, IndexBuildStats | None]] = field(default_factory=list)
    releases: list[str | None] = field(default_factory=list)
    release_stats: list[IndexBuildStats | None] = field(default_factory=list)
    pending: int = 0
    admission: IndexAdmission | None = None
    # A store call that waits for `proceed` once `held` is set; `finished` follows it.
    hold: str | None = None
    held: asyncio.Event = field(default_factory=asyncio.Event)
    proceed: asyncio.Event = field(default_factory=asyncio.Event)
    finished: asyncio.Event = field(default_factory=asyncio.Event)
    # Seconds each `replace_file` takes.
    write_delay: float = 0.0

    def _call(self, name: str) -> None:
        self.tracker.store(name)
        if self.lose_lease_at == name:
            raise IndexLeaseLost("lost")

    async def _wait_if_held(self, name: str) -> None:
        if self.hold == name:
            self.held.set()
            await self.proceed.wait()

    async def get_state(self, repository_id: UUID) -> IndexState | None:
        assert repository_id == _REPO
        self._call("get_state")
        if self.publications and self.after_publish is not None:
            return self.after_publish
        if (self.releases or self.failures) and self.after_release is not None:
            return self.after_release
        return self.state

    async def claim_lease(
        self, repository_id: UUID, generation: int, lease_seconds: int, admission: IndexAdmission
    ) -> LeaseClaim | ClaimRefused:
        assert (repository_id, generation, lease_seconds) == (_REPO, _GENERATION, 1200)
        self.admission = admission
        self._call("claim_lease")
        return self.claim

    async def discard_abandoned(self, repository_id: UUID, fence: IndexFence, limit: int) -> int:
        self._call("discard_abandoned")
        return self.discards.pop(0) if len(self.discards) > 1 else self.discards[0]

    async def replace_file(
        self, repository_id: UUID, fence: IndexFence, file: IndexedFile
    ) -> IndexStorage:
        assert fence == _FENCE
        self._call("replace_file")
        if self.write_delay:
            await asyncio.sleep(self.write_delay)
        self.written.append((self.tracker.transactions, file))
        self.pending += self.bytes_per_file
        stored = self.state.stored_bytes if self.state is not None else 0
        return IndexStorage(stored, self.pending, stored + self.pending + self.other_bytes)

    async def publish(self, repository_id: UUID, fence: IndexFence, **values: Any) -> bool:
        self._call("publish")
        self.publications.append(values)
        return self.published

    async def mark_failed(
        self,
        repository_id: UUID,
        fence: IndexFence,
        error_code: str,
        *,
        stats: IndexBuildStats | None = None,
    ) -> None:
        self._call("mark_failed")
        await self._wait_if_held("mark_failed")
        self.failures.append((error_code, stats))
        self.finished.set()

    async def release(
        self,
        repository_id: UUID,
        fence: IndexFence,
        *,
        last_error_code: str | None = None,
        stats: IndexBuildStats | None = None,
        terminal_head_sha: str | None = None,
        expected_target_sha: str | None = None,
    ) -> IndexStatus:
        self._call("release")
        await self._wait_if_held("release")
        self.releases.append(last_error_code)
        self.release_stats.append(stats)
        self.finished.set()
        return IndexStatus.READY

    async def iter_files(
        self, repository_id: UUID, after: str | None, limit: int
    ) -> list[tuple[str, str]]:
        self._call("iter_files")
        rows = sorted(item for item in self.files.items() if after is None or item[0] > after)
        return rows[:limit]

    async def iter_imports(
        self, repository_id: UUID, after: ImportRecord | None, limit: int
    ) -> list[ImportRecord]:
        self._call("iter_imports")

        def key(record: ImportRecord) -> tuple[str, str, str]:
            return record.path, record.specifier, record.resolution.value

        rows = sorted(self.imports, key=key)
        return [row for row in rows if after is None or key(row) > key(after)][:limit]

    async def visible_counts(self, repository_id: UUID, paths: Sequence[str]) -> IndexCounts:
        self._call("visible_counts")
        found = [path for path in paths if path in self.files]
        return IndexCounts(
            len(found),
            sum(self.counts.get(path, (0, 0))[0] for path in found),
            sum(self.counts.get(path, (0, 0))[1] for path in found),
        )


@dataclass
class Uow:
    tracker: Tracker
    store: Store
    entered_at: int = 0

    async def __aenter__(self) -> Self:
        self.tracker.open += 1
        self.tracker.transactions += 1
        self.entered_at = len(self.tracker.log)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            # `hold="claim_exit"` parks the end of the claim transaction, after its commit.
            if (
                self.store.hold == "claim_exit"
                and "claim_lease" in self.tracker.log[self.entered_at :]
            ):
                self.store.held.set()
                await asyncio.sleep(3600)
        finally:
            self.tracker.open -= 1

    async def commit(self) -> None:
        self.tracker.store("commit")

    async def rollback(self) -> None:
        self.tracker.store("rollback")

    @property
    def index(self) -> CodeIndexStore:
        # The fake implements only the methods a build calls.
        return cast(CodeIndexStore, self.store)


@dataclass
class Source:
    tracker: Tracker
    head_value: BranchHead | None = BranchHead("main", _HEAD)
    comparison: Comparison | None = None
    errors: dict[str, BaseException] = field(default_factory=dict)
    hang: str | None = None
    content: bytes = b"archive"
    requests: int = 0
    policy_tree: Tree = field(default_factory=dict)
    policy_commits: list[str] = field(default_factory=list)

    @property
    def request_count(self) -> int:
        return self.requests

    async def _call(self, name: str) -> None:
        self.tracker.network(name)
        self.requests += 1
        if name in self.errors:
            raise self.errors[name]
        if self.hang == name:
            await asyncio.sleep(3600)

    async def head(self) -> BranchHead | None:
        await self._call("head")
        return self.head_value

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        assert (base_sha, head_sha) == (_OLD, _HEAD)
        await self._call("compare")
        return self.comparison

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        # This fake counts port calls, not GitHub HTTP requests.
        self.policy_commits.append(commit_sha)
        await self._call("rules_document")
        raw = self.policy_tree.get(_RULES)
        if ".review" in self.policy_tree or isinstance(raw, Link):
            raise PolicyError(PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED, "nonregular policy")
        if raw is None:
            return missing_document(_RULES)
        return normalize_document(_RULES, raw, blob_oid=_blob(raw), policy_base_sha=commit_sha)

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        assert (commit_sha, max_bytes) == (_HEAD, _LIMITS.download_bytes)
        await self._call("archive")
        return SourceArchive(commit_sha, self.content)


@dataclass
class Archives:
    """A fake archive reader over an in-memory tree, in insertion order."""

    tree: Tree
    error: BaseException | None = None
    scans: int = 0
    reads: list[frozenset[str]] = field(default_factory=list)

    async def scan(
        self, archive: SourceArchive, *, documents: Set[str], deadline: float
    ) -> ArchiveScan:
        assert archive.commit_sha == _HEAD
        assert documents == {"go.mod"}
        self.scans += 1
        if self.error is not None:
            raise self.error
        entries = {}
        captured = {}
        for path, value in self.tree.items():
            if isinstance(value, Link):
                entries[path] = ArchiveEntry(path, 0, symlink=True)
                continue
            readable = len(value) <= _LIMITS.read_bytes
            entries[path] = ArchiveEntry(
                path, len(value), blob_sha=_blob(value) if readable else None
            )
            if readable and path in documents:
                captured[path] = value
        return ArchiveScan(archive.commit_sha, entries, captured, ArchiveCounters(len(entries), 0))

    async def contents(
        self, archive: SourceArchive, paths: Set[str], *, deadline: float, batch_files: int
    ) -> AsyncIterator[tuple[tuple[str, bytes], ...]]:
        self.reads.append(frozenset(paths))
        batch: list[tuple[str, bytes]] = []
        for path, value in self.tree.items():
            if isinstance(value, Link) or path not in paths or len(value) > _LIMITS.read_bytes:
                continue
            batch.append((path, value))
            if len(batch) == batch_files:
                yield tuple(batch)
                batch = []
        if batch:
            yield tuple(batch)


@dataclass
class Parser:
    """The in-process extractor; `failures` fail chosen paths, `error` fails the job."""

    failures: dict[str, AstFailureReason] = field(default_factory=dict)
    error: BaseException | None = None
    parsed: list[str] = field(default_factory=list)
    # Seconds the first file takes.
    first_delay: float = 0.0
    # `hang` parks every parse once `entered` is set.
    hang: bool = False
    entered: asyncio.Event = field(default_factory=asyncio.Event)

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        if self.error is not None:
            raise self.error
        if self.first_delay and not self.parsed:
            await asyncio.sleep(self.first_delay)
        if self.hang:
            self.entered.set()
            await asyncio.sleep(3600)
        results: dict[str, AstFacts | AstFailureReason] = {}
        for request in requests:
            self.parsed.append(request.key)
            if request.key in self.failures:
                results[request.key] = self.failures[request.key]
                continue
            try:
                request.source.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                results[request.key] = AstFailureReason.SOURCE_DECODE_FAILED
                continue
            results[request.key] = extract_facts(request.language, request.source, cpu_seconds=5.0)
        return results

    async def aclose(self) -> None:
        return None


@dataclass
class Wakeups:
    tracker: Tracker
    woken: list[tuple[UUID, int]] = field(default_factory=list)

    async def wake(self, repository_id: UUID, generation: int) -> None:
        self.tracker.network("wake")
        self.woken.append((repository_id, generation))


@dataclass
class Repositories:
    known: bool = True

    async def get(self, repository_id: UUID) -> IndexedRepository | None:
        if not self.known:
            return None
        return IndexedRepository(repository_id, "github", 17, "octo/repo")


@dataclass
class World:
    tracker: Tracker
    store: Store
    source: Source
    archives: Archives
    parser: Parser
    wakeups: Wakeups
    repositories: Repositories = field(default_factory=Repositories)
    sources: list[IndexedRepository] = field(default_factory=list)
    # What the source factory raises after the claim, if anything.
    source_error: BaseException | None = None

    def use_case(
        self, limits: IndexLimits = _LIMITS, settings: IndexBuildSettings = _SETTINGS
    ) -> BuildRepositoryIndex:
        def source(repository: IndexedRepository) -> Source:
            self.sources.append(repository)
            if self.source_error is not None:
                raise self.source_error
            return self.source

        return BuildRepositoryIndex(
            uow=lambda: Uow(self.tracker, self.store),
            repositories=self.repositories,
            sources=source,
            archives=self.archives,
            parser=self.parser,
            wakeups=self.wakeups,
            versions=_VERSIONS,
            limits=limits,
            settings=settings,
        )

    def run(
        self, limits: IndexLimits = _LIMITS, settings: IndexBuildSettings = _SETTINGS
    ) -> BuildOutcome:
        return asyncio.run(self.use_case(limits, settings).execute(_REPO, _GENERATION))

    def written(self) -> list[str]:
        return [file.path for _, file in self.store.written]

    def network(self) -> list[str]:
        return [name for name in self.tracker.log if name in {"head", "compare", "archive"}]


def _world(
    tree: Tree = _TREE,
    *,
    state: IndexState | None = _NO_INDEX,
    comparison: Comparison | None = None,
    policy_tree: Tree | None = None,
    **store: Any,
) -> World:
    tracker = Tracker()
    return World(
        tracker,
        Store(tracker, state, **store),
        Source(
            tracker, comparison=comparison, policy_tree=tree if policy_tree is None else policy_tree
        ),
        Archives(tree),
        Parser(),
        Wakeups(tracker),
    )


def _comparison(*files: ChangedPath, ancestry: Ancestry = Ancestry.AHEAD) -> Comparison:
    return Comparison(ancestry, files, complete=True)


def _changed(path: str, status: ChangeStatus = ChangeStatus.MODIFIED) -> ChangedPath:
    return ChangedPath(path, status)


def _indexed(tree: Tree) -> dict[str, str]:
    """The stored `(path, blob_sha)` of every file of `tree` as an earlier build wrote it."""
    return {path: _blob(value) for path, value in tree.items() if isinstance(value, bytes)}


# The tree the index was built at: `app/util.py` existed and `app/models.py` differed.
_OLD_TREE: Tree = {
    "app/models.py": b"class Account:\n    pass\n",
    "app/views.py": _VIEWS,
    "app/util.py": _UTIL,
    "app/cli.py": _CLI,
}
_HEAD_TREE: Tree = {"app/models.py": _MODELS, "app/views.py": _VIEWS, "app/cli.py": _CLI}
_OLD_IMPORTS = [
    ImportRecord("app/views.py", "app.models", ImportResolution.RESOLVED, "app/models.py"),
    ImportRecord("app/cli.py", "app.util", ImportResolution.RESOLVED, "app/util.py"),
]
_INCREMENTAL = _comparison(_changed("app/models.py"), _changed("app/util.py", ChangeStatus.REMOVED))


def _incremental_world(**store: Any) -> World:
    return _world(
        _HEAD_TREE,
        state=_state(file_count=4, symbol_count=6, edge_count=5),
        comparison=_INCREMENTAL,
        files=_indexed(_OLD_TREE),
        imports=list(_OLD_IMPORTS),
        **store,
    )


def _go_mod(
    path: str = "go.mod",
    status: ChangeStatus = ChangeStatus.MODIFIED,
    previous_path: str | None = None,
) -> ChangedPath:
    return ChangedPath(path, status, previous_path)


# The target of the claim snapshot, the target on the state row at the end, and whether
# it moved: only a row that differs from the snapshot wakes the repository.
_TARGETS = [
    pytest.param(None, "d" * 40, True, id="none-then-a-target"),
    pytest.param(None, None, False, id="none-then-none"),
    pytest.param("d" * 40, "d" * 40, False, id="same-target"),
    pytest.param("d" * 40, "e" * 40, True, id="another-target"),
]


def _unsafe_archive(world: World) -> None:
    world.archives.error = ArchiveUnsafe("unsafe")


def _unavailable_head(world: World) -> None:
    world.source.errors["head"] = VcsUnavailable("down")


def _crashing_parser(world: World) -> None:
    world.parser.error = RuntimeError("parser crashed")


def _unchanged(world: World) -> None:
    world.store.state = _state(indexed_commit_sha=_HEAD)


def _empty(world: World) -> None:
    world.source.head_value = None


def _access_denied(world: World) -> None:
    world.source.errors["head"] = VcsAccessDenied("gone")


def _lost_lease(world: World) -> None:
    world.store.lose_lease_at = "replace_file"


def _fail_at_archive(world: World, error: BaseException) -> None:
    world.source.errors["archive"] = error


def _fail_at_scan(world: World, error: BaseException) -> None:
    world.archives.error = error


# Two exported top-level functions per file: each summary holds 2 symbols.
_GO_A = b"package m\n\nfunc A() {}\n\nfunc B() {}\n"
_GO_B = b"package m\n\nfunc C() {}\n\nfunc D() {}\n"
_GO_OLD: Tree = {"a.go": _GO_A, "b.go": _GO_B, "c.go": b"package m\n\nfunc E() {}\n"}
_GO_HEAD: Tree = {**_GO_OLD, "c.go": b"package m\n\nfunc F() {}\n"}


def _go_world(mode: BuildMode) -> World:
    if mode is BuildMode.FULL:
        return _world(_GO_HEAD)
    # The index holds the 3 files of the base; HEAD changes `c.go` only.
    return _world(
        _GO_HEAD,
        state=_state(file_count=3),
        comparison=_comparison(_changed("c.go")),
        files=_indexed(_GO_OLD),
    )


def _unfinished_discard(world: World) -> None:
    world.store.discards = [2]


def _policy_error(world: World) -> None:
    world.source.policy_tree = {_RULES: b"```review-ignore\nlegacy/**\n"}


def _unavailable_archive(world: World) -> None:
    world.source.errors["archive"] = VcsUnavailable("down")


def _failing_source_factory(world: World) -> None:
    world.source_error = RuntimeError("no source for this repository")


def _parked_parse(world: World) -> asyncio.Event:
    world.parser.hang = True
    return world.parser.entered


def _parked_claim_exit(world: World) -> asyncio.Event:
    world.store.hold = "claim_exit"
    return world.store.held


def _file(world: World, path: str) -> IndexedFile:
    return next(file for _, file in world.store.written if file.path == path)


__all__ = (
    "Ancestry",
    "Any",
    "ArchiveEntry",
    "ArchiveScan",
    "ArchiveUnsafe",
    "Archives",
    "AstFacts",
    "AsyncIterator",
    "BuildMode",
    "BuildStatus",
    "ChangeStatus",
    "ClaimRefusal",
    "ClaimRefused",
    "Comparison",
    "ExclusionClass",
    "FullBuildReason",
    "IgnoreMatcher",
    "IgnorePolicy",
    "ImportRecord",
    "ImportResolution",
    "IndexAdmission",
    "IndexBuildStats",
    "IndexLimitExceeded",
    "IndexLimits",
    "IndexState",
    "IndexedFile",
    "IndexedSymbol",
    "LeaseClaim",
    "Link",
    "Mapping",
    "PolicyError",
    "PolicyErrorCode",
    "Repositories",
    "Set",
    "Source",
    "SourceArchive",
    "Tree",
    "VcsAccessDenied",
    "VcsUnavailable",
    "_ADMISSION",
    "_CLI",
    "_DIGEST",
    "_FENCE",
    "_GENERATION",
    "_GO_A",
    "_GO_B",
    "_HEAD",
    "_HEAD_TREE",
    "_INCREMENTAL",
    "_LIMITS",
    "_MIB",
    "_MODELS",
    "_NO_INDEX",
    "_OLD",
    "_OLD_IMPORTS",
    "_OLD_TREE",
    "_PROFILE",
    "_REPO",
    "_RULES",
    "_SETTINGS",
    "_TARGETS",
    "_TREE",
    "_VERSIONS",
    "_VIEWS",
    "_access_denied",
    "_blob",
    "_changed",
    "_comparison",
    "_crashing_parser",
    "_empty",
    "_fail_at_archive",
    "_fail_at_scan",
    "_failing_source_factory",
    "_file",
    "_go_mod",
    "_go_world",
    "_incremental_world",
    "_indexed",
    "_lost_lease",
    "_parked_claim_exit",
    "_parked_parse",
    "_policy_error",
    "_state",
    "_unavailable_archive",
    "_unavailable_head",
    "_unchanged",
    "_unfinished_discard",
    "_unsafe_archive",
    "_world",
    "asyncio",
    "build_index",
    "dataclass",
    "field",
    "file_rows",
    "limits_profile_version",
    "logging",
    "parse_review_ignore",
    "parser_profile_version",
    "pytest",
    "replace",
    "symbol_rows",
    "time",
    "timedelta",
)
