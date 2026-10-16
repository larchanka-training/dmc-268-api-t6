"""Use case `BuildRepositoryIndex` against fakes of its ports (#142).

AC5: every mode decision. AC7: no transaction is open during a VCS call. AC8: every code,
and an empty repository without an error. AC12: a CAS miss, a lost lease, the soft cutoff
and a target change. AC3: the skip counters equal literals.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import AsyncIterator, Mapping, Sequence, Set
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from types import TracebackType
from typing import Any, Self, cast
from uuid import UUID

import pytest

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
    IndexFence,
    IndexLeaseLost,
    IndexState,
    IndexStorage,
    LeaseClaim,
)
from app.modules.code_index.application.ports import IndexedRepository
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
from app.modules.reviews.application.review_ignore import parse_review_ignore
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_REPO = UUID("00000000-0000-0000-0000-000000000142")
_GENERATION = 3
_OLD = "a" * 40
_HEAD = "c" * 40
_MIB = 1_048_576
_LIMITS = IndexLimits()
_VERSIONS = IndexVersions(
    extractor_version="facts/v1",
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

    async def __aenter__(self) -> Self:
        self.tracker.open += 1
        self.tracker.transactions += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
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
        assert documents == {_RULES, "go.mod"}
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

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        if self.error is not None:
            raise self.error
        if self.first_delay and not self.parsed:
            await asyncio.sleep(self.first_delay)
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
    **store: Any,
) -> World:
    tracker = Tracker()
    return World(
        tracker,
        Store(tracker, state, **store),
        Source(tracker, comparison=comparison),
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


def test_the_profile_versions_fit_their_columns_and_follow_their_inputs() -> None:
    limits = limits_profile_version(_LIMITS)
    assert limits.startswith("limits/v1:")
    assert len(limits) <= 64
    # Only the limits that change content are in the digest.
    assert limits_profile_version(replace(_LIMITS, parsed_files=1)) == limits
    assert limits_profile_version(replace(_LIMITS, parse_bytes=1)) != limits
    assert limits_profile_version(replace(_LIMITS, read_bytes=1)) != limits
    parser = parser_profile_version(["python:1", "go:2"])
    assert parser == parser_profile_version(["go:2", "python:1"])
    assert len(parser) == 64
    assert parser != parser_profile_version(["python:1", "go:3"])
    assert _PROFILE.default_branch == "main"
    assert _PROFILE.exclusions_digest == _DIGEST


def test_a_missing_repository_is_stale_without_a_claim() -> None:
    world = _world()
    world.repositories = Repositories(known=False)

    assert world.run().status is BuildStatus.STALE
    assert world.tracker.log == []


@pytest.mark.parametrize(
    ("refusal", "status", "code"),
    [
        (ClaimRefused(ClaimRefusal.STALE), BuildStatus.STALE, None),
        (ClaimRefused(ClaimRefusal.LEASED), BuildStatus.LEASED, None),
        (ClaimRefused(ClaimRefusal.BUSY), BuildStatus.BUSY, None),
        (
            ClaimRefused(ClaimRefusal.QUOTA, "limit_exceeded:workspace_storage"),
            BuildStatus.FAILED,
            "limit_exceeded:workspace_storage",
        ),
    ],
)
def test_a_refused_claim_ends_the_job_after_committing_the_claim(
    refusal: ClaimRefused, status: BuildStatus, code: str | None
) -> None:
    world = _world(claim=refusal)

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (status, code)
    # A QUOTA refusal wrote `failed`: the refusal is committed too.
    assert world.tracker.log == ["claim_lease", "commit"]
    assert world.store.admission == _ADMISSION
    assert world.sources == []


def test_a_failing_source_factory_after_the_claim_releases_the_lease() -> None:
    world = _world()
    world.source_error = RuntimeError("no source for this repository")

    with pytest.raises(RuntimeError, match="no source"):
        world.run()

    assert world.store.releases == [None]
    assert world.tracker.log == ["claim_lease", "commit", "release", "commit"]


def test_a_full_build_writes_every_admitted_file_in_batches_and_publishes_from_the_base() -> None:
    tree = {**_TREE, "app/cli.py": _CLI}
    world = _world(tree, state=_state(indexed_commit_sha=None, profile=None))

    outcome = world.run(settings=replace(_SETTINGS, batch_files=2))

    assert outcome.status is BuildStatus.PUBLISHED
    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.NO_INDEX)
    assert outcome.commit_sha == _HEAD
    # Two files per transaction, each batch its own transaction.
    batches: dict[int, list[str]] = {}
    for transaction, file in world.store.written:
        batches.setdefault(transaction, []).append(file.path)
    assert list(batches.values()) == [["app/models.py", "app/views.py"], ["app/cli.py"]]
    views = next(file for _, file in world.store.written if file.path == "app/views.py")
    assert views.blob_sha == _blob(_VIEWS)
    assert [(record.specifier, record.target_path) for record in views.imports] == [
        ("app.models", "app/models.py")
    ]
    (publication,) = world.store.publications
    assert publication == {
        "base_commit_sha": None,
        "commit_sha": _HEAD,
        "full": True,
        "deleted_paths": (),
        "profile": _VERSIONS.profile("main", _DIGEST),
        "stats": IndexBuildStats(len(b"archive"), publication["stats"].build_duration_ms, 2),
    }
    assert world.network() == ["head", "archive"]
    assert (outcome.parsed_files, outcome.written_files, outcome.deleted_files) == (3, 3, 0)
    assert (outcome.github_request_count, outcome.downloaded_bytes) == (2, 7)
    assert world.wakeups.woken == []


def test_a_full_build_over_a_published_index_passes_the_indexed_commit_as_its_base() -> None:
    world = _world(state=_state(profile=replace(_PROFILE, extractor_version="facts/v0")))

    outcome = world.run()

    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.PROFILE_CHANGED)
    assert world.store.publications[0]["base_commit_sha"] == _OLD
    assert world.network() == ["head", "archive"]


def _go_mod(
    path: str = "go.mod",
    status: ChangeStatus = ChangeStatus.MODIFIED,
    previous_path: str | None = None,
) -> ChangedPath:
    return ChangedPath(path, status, previous_path)


@pytest.mark.parametrize(
    ("state", "comparison", "reason", "compared"),
    [
        pytest.param(_NO_INDEX, None, FullBuildReason.NO_INDEX, False, id="no-index"),
        pytest.param(
            _state(profile=replace(_PROFILE, resolver_version="resolver/v0")),
            None,
            FullBuildReason.PROFILE_CHANGED,
            False,
            id="resolver-version",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, limits_profile_version="limits/v0:0")),
            None,
            FullBuildReason.PROFILE_CHANGED,
            False,
            id="limits-version",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, default_branch="master")),
            None,
            FullBuildReason.BRANCH_CHANGED,
            False,
            id="default-branch",
        ),
        pytest.param(_state(), None, FullBuildReason.BASE_UNKNOWN, True, id="base-unknown"),
        pytest.param(
            _state(),
            _comparison(ancestry=Ancestry.DIVERGED),
            FullBuildReason.NOT_AHEAD,
            True,
            id="diverged",
        ),
        pytest.param(
            _state(),
            _comparison(ancestry=Ancestry.BEHIND),
            FullBuildReason.NOT_AHEAD,
            True,
            id="behind",
        ),
        pytest.param(
            _state(),
            Comparison(Ancestry.AHEAD, (_changed("app/models.py"),), complete=False),
            FullBuildReason.COMPARE_INCOMPLETE,
            True,
            id="incomplete",
        ),
        pytest.param(
            _state(),
            Comparison(None, (), complete=False),
            FullBuildReason.COMPARE_INCOMPLETE,
            True,
            id="unread-compare",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod()),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-mod",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod("tools/go.mod")),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="nested-go-mod",
        ),
        pytest.param(
            _state(),
            _comparison(_go_mod("go.work", status=ChangeStatus.ADDED)),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-work",
        ),
        pytest.param(
            _state(),
            _comparison(
                _go_mod("build/go.mod.txt", status=ChangeStatus.RENAMED, previous_path="go.mod")
            ),
            FullBuildReason.GO_MODULE_CHANGED,
            True,
            id="go-mod-renamed-away",
        ),
        pytest.param(
            _state(profile=replace(_PROFILE, exclusions_digest="0" * 64)),
            _comparison(_changed("app/models.py")),
            FullBuildReason.EXCLUSIONS_CHANGED,
            True,
            id="ignore-policy",
        ),
    ],
)
def test_every_full_build_decision(
    state: IndexState, comparison: Comparison | None, reason: FullBuildReason, compared: bool
) -> None:
    world = _world(state=state, comparison=comparison, files=_indexed(_TREE))

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        reason,
    )
    assert world.network() == (["head", "compare", "archive"] if compared else ["head", "archive"])
    assert world.store.publications[0]["full"] is True
    assert sorted(world.written()) == ["app/models.py", "app/views.py"]
    # A full build reads no stored rows.
    assert "iter_files" not in world.tracker.log


def test_an_incremental_build_rewrites_the_changes_and_their_importers_only() -> None:
    world = _incremental_world()

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.INCREMENTAL,
        None,
    )
    # `app/models.py` changed; `app/cli.py` imports the removed `app/util.py` (rule W).
    assert sorted(world.written()) == ["app/cli.py", "app/models.py"]
    (publication,) = world.store.publications
    assert publication["base_commit_sha"] == _OLD
    assert publication["full"] is False
    assert publication["deleted_paths"] == ("app/util.py",)
    cli = next(file for _, file in world.store.written if file.path == "app/cli.py")
    assert [(record.specifier, record.resolution) for record in cli.imports] == [
        ("app.util", ImportResolution.EXTERNAL)
    ]
    assert world.network() == ["head", "compare", "archive"]
    assert (outcome.written_files, outcome.deleted_files) == (2, 1)


def test_an_incremental_build_also_rewrites_a_stored_file_whose_blob_differs() -> None:
    stored = {**_indexed(_HEAD_TREE), "app/views.py": "f" * 40}
    world = _world(
        _HEAD_TREE,
        state=_state(),
        comparison=_comparison(_changed("app/models.py")),
        files=stored,
        imports=list(_OLD_IMPORTS[:1]),
    )

    outcome = world.run()

    assert outcome.mode is BuildMode.INCREMENTAL
    assert sorted(world.written()) == ["app/models.py", "app/views.py"]


def test_a_stored_file_that_became_a_symlink_loses_its_rows_and_wakes_its_importers() -> None:
    tree: Tree = {**_HEAD_TREE, "app/util.py": Link("cli.py")}
    world = _world(
        tree,
        state=_state(),
        comparison=_comparison(),
        files=_indexed(_OLD_TREE),
        imports=list(_OLD_IMPORTS),
    )

    outcome = world.run()

    assert outcome.mode is BuildMode.INCREMENTAL
    # `app/models.py` differs from its stored blob; `app/cli.py` imports the new symlink.
    assert sorted(world.written()) == ["app/cli.py", "app/models.py"]
    assert world.store.publications[0]["deleted_paths"] == ("app/util.py",)
    cli = next(file for _, file in world.store.written if file.path == "app/cli.py")
    assert [(record.resolution, record.target_path) for record in cli.imports] == [
        (ImportResolution.EXCLUDED, None)
    ]
    assert outcome.exclusions[ExclusionClass.SYMLINK] == 1


@pytest.mark.parametrize(
    ("limit", "comparison"),
    [
        pytest.param(1, _INCREMENTAL, id="changed-files"),
        # One change, but its importer pushes the set over the limit.
        pytest.param(
            2,
            _comparison(_changed("app/util.py", ChangeStatus.REMOVED), _changed("app/models.py")),
            id="importers",
        ),
    ],
)
def test_too_many_changes_make_the_build_full(limit: int, comparison: Comparison) -> None:
    world = _incremental_world()
    world.source.comparison = comparison

    outcome = world.run(replace(_LIMITS, incremental_files=limit))

    assert (outcome.mode, outcome.full_reason) == (
        BuildMode.FULL,
        FullBuildReason.TOO_MANY_CHANGES,
    )
    assert world.store.publications[0]["full"] is True
    assert sorted(world.written()) == ["app/cli.py", "app/models.py", "app/views.py"]


def test_head_equal_to_the_indexed_commit_with_the_same_profile_releases_unchanged() -> None:
    world = _world(state=_state(indexed_commit_sha=_HEAD))

    outcome = world.run()

    assert (outcome.status, outcome.commit_sha) == (BuildStatus.UNCHANGED, _HEAD)
    assert world.store.releases == [None]
    assert world.network() == ["head"]
    assert world.archives.scans == 0


def test_head_equal_to_the_indexed_commit_with_another_profile_builds_full() -> None:
    world = _world(
        state=_state(indexed_commit_sha=_HEAD, profile=replace(_PROFILE, default_branch="dev"))
    )

    outcome = world.run()

    assert (outcome.mode, outcome.full_reason) == (BuildMode.FULL, FullBuildReason.BRANCH_CHANGED)


@pytest.mark.parametrize(
    "version",
    ["extractor_version", "resolver_version", "parser_profile_version", "limits_profile_version"],
)
def test_head_equal_to_the_indexed_commit_built_with_other_versions_builds_full(
    version: str,
) -> None:
    profile = replace(_PROFILE, **{version: "other"})
    world = _world(state=_state(indexed_commit_sha=_HEAD, profile=profile))

    outcome = world.run()

    assert (outcome.status, outcome.mode, outcome.full_reason) == (
        BuildStatus.PUBLISHED,
        BuildMode.FULL,
        FullBuildReason.PROFILE_CHANGED,
    )
    assert world.network() == ["head", "archive"]
    assert world.store.publications[0]["base_commit_sha"] == _HEAD


def test_an_empty_repository_releases_as_empty_without_an_error() -> None:
    world = _world(state=_state())
    world.source.head_value = None

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.EMPTY, None)
    assert world.store.releases == [None]
    assert world.store.failures == []
    assert world.network() == ["head"]


def test_abandoned_rows_are_discarded_in_portions_before_any_vcs_call() -> None:
    world = _world(discards=[2, 2, 1])

    outcome = world.run(settings=replace(_SETTINGS, discard_batch_rows=2))

    assert outcome.status is BuildStatus.PUBLISHED
    assert world.tracker.log[:10] == [
        "claim_lease",
        "commit",
        "discard_abandoned",
        "commit",
        "discard_abandoned",
        "commit",
        "discard_abandoned",
        "commit",
        "head",
        "get_state",
    ]


def test_discarding_past_the_soft_cutoff_releases_and_wakes_the_repository() -> None:
    world = _world(discards=[2])

    outcome = world.run(settings=replace(_SETTINGS, discard_batch_rows=2, soft_cutoff_seconds=0))

    assert outcome.status is BuildStatus.REWAKE
    assert world.store.releases == [None]
    assert world.store.failures == []
    assert world.wakeups.woken == [(_REPO, _GENERATION)]
    assert world.network() == []


def test_a_publication_miss_wakes_the_repository() -> None:
    world = _world(published=False)

    outcome = world.run()

    assert outcome.status is BuildStatus.REWAKE
    assert world.wakeups.woken == [(_REPO, _GENERATION)]
    assert world.store.releases == []


@pytest.mark.parametrize(
    ("target", "woken"),
    [("d" * 40, [(_REPO, _GENERATION)]), (None, [])],
)
def test_a_target_that_moved_during_the_build_wakes_the_repository(
    target: str | None, woken: list[tuple[UUID, int]]
) -> None:
    world = _world(after_publish=_state(indexed_commit_sha=_HEAD, target_commit_sha=target))

    outcome = world.run()

    assert outcome.status is BuildStatus.PUBLISHED
    assert world.wakeups.woken == woken
    assert world.tracker.log[-2:] == (["get_state", "wake"] if woken else ["commit", "get_state"])


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


@pytest.mark.parametrize(
    ("arrange", "status"),
    [
        (_unchanged, BuildStatus.UNCHANGED),
        (_empty, BuildStatus.EMPTY),
        (_access_denied, BuildStatus.RELEASED),
    ],
    ids=["unchanged", "empty", "released"],
)
@pytest.mark.parametrize(
    ("target", "woken"),
    [("d" * 40, [(_REPO, _GENERATION)]), (None, [])],
    ids=["moved", "not-moved"],
)
def test_a_target_that_moved_before_a_release_wakes_the_repository(
    arrange: Any, status: BuildStatus, target: str | None, woken: list[tuple[UUID, int]]
) -> None:
    # A wake-up refused as LEASED during the job left its target on the state row.
    world = _world(state=_state(), after_release=_state(target_commit_sha=target))
    arrange(world)

    outcome = world.run()

    assert outcome.status is status
    assert world.wakeups.woken == woken
    expected = ["release", "commit", "get_state", *(["wake"] if woken else [])]
    assert world.tracker.log[-len(expected) :] == expected


def _lost_lease(world: World) -> None:
    world.store.lose_lease_at = "replace_file"


@pytest.mark.parametrize(
    ("arrange", "status", "tail"),
    [
        (_unavailable_head, BuildStatus.TRANSIENT, ["release", "commit"]),
        (_unsafe_archive, BuildStatus.FAILED, ["mark_failed", "commit"]),
        (_lost_lease, BuildStatus.LEASE_LOST, ["replace_file"]),
    ],
    ids=["transient", "failed", "lease-lost"],
)
def test_a_moved_target_is_not_rechecked_after_a_transient_failed_or_lost_lease(
    arrange: Any, status: BuildStatus, tail: list[str]
) -> None:
    world = _world(state=_state(), after_release=_state(target_commit_sha="d" * 40))
    arrange(world)

    outcome = world.run()

    assert outcome.status is status
    assert world.wakeups.woken == []
    assert world.tracker.log[-len(tail) :] == tail


@pytest.mark.parametrize("step", ["replace_file", "publish", "discard_abandoned", "release"])
def test_a_lost_lease_stops_the_job_without_a_write_or_a_retry(step: str) -> None:
    world = _world(lose_lease_at=step)
    if step == "release":
        world.source.head_value = None

    outcome = world.run()

    assert outcome.status is BuildStatus.LEASE_LOST
    assert world.store.failures == []
    assert world.store.releases == []
    assert world.wakeups.woken == []


def test_a_lost_lease_while_failing_is_still_a_lost_lease() -> None:
    world = _world(lose_lease_at="mark_failed")
    world.archives.error = ArchiveUnsafe("unsafe")

    assert world.run().status is BuildStatus.LEASE_LOST


def _fail_at_archive(world: World, error: BaseException) -> None:
    world.source.errors["archive"] = error


def _fail_at_scan(world: World, error: BaseException) -> None:
    world.archives.error = error


@pytest.mark.parametrize(
    ("inject", "error", "code", "requests"),
    [
        (
            _fail_at_archive,
            IndexLimitExceeded("download_bytes"),
            "limit_exceeded:download_bytes",
            2,
        ),
        (_fail_at_scan, IndexLimitExceeded("unpacked_bytes"), "limit_exceeded:unpacked_bytes", 2),
        (_fail_at_scan, IndexLimitExceeded("archive_entries"), "limit_exceeded:archive_entries", 2),
        (_fail_at_scan, IndexLimitExceeded("deadline"), "limit_exceeded:deadline", 2),
        (_fail_at_scan, ArchiveUnsafe("unsafe"), "archive_unsafe", 2),
    ],
)
def test_an_archive_failure_fails_the_index_with_its_code_and_the_request_count(
    inject: Any, error: BaseException, code: str, requests: int
) -> None:
    world = _world()
    inject(world, error)

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, code)
    ((failed_code, stats),) = world.store.failures
    assert failed_code == code
    assert stats is not None
    assert stats.github_request_count == requests
    assert world.store.releases == []


@pytest.mark.parametrize(
    ("limits", "admission", "other_bytes", "code"),
    [
        (replace(_LIMITS, parsed_files=1), _ADMISSION, 0, "limit_exceeded:parsed_files"),
        (replace(_LIMITS, symbols=1), _ADMISSION, 0, "limit_exceeded:symbols"),
        (replace(_LIMITS, edges=1), _ADMISSION, 0, "limit_exceeded:edges"),
        (
            _LIMITS,
            replace(_ADMISSION, repository_storage_bytes=150),
            0,
            "limit_exceeded:repository_storage",
        ),
        (
            _LIMITS,
            replace(_ADMISSION, workspace_storage_bytes=1_000),
            950,
            "limit_exceeded:workspace_storage",
        ),
    ],
)
def test_a_build_limit_fails_the_index_before_writing_over_it(
    limits: IndexLimits, admission: IndexAdmission, other_bytes: int, code: str
) -> None:
    world = _world(bytes_per_file=100, other_bytes=other_bytes)

    outcome = world.run(limits, replace(_SETTINGS, admission=admission, batch_files=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, code)
    assert [failed for failed, _ in world.store.failures] == [code]
    assert world.store.publications == []


@pytest.mark.parametrize(("limit", "total"), [("parsed_files", 3), ("symbols", 3), ("edges", 2)])
def test_the_incremental_limits_count_the_rows_the_build_keeps(limit: str, total: int) -> None:
    # 4 stored files with 6 symbols and 5 edges; closing app/models, util and cli leaves 1
    # file, 1 symbol and 2 edges, and the build writes 2 files with 2 symbols and no edge:
    # 3 files, 3 symbols and 2 edges in all.
    counts = {"app/models.py": (2, 0), "app/util.py": (2, 1), "app/cli.py": (1, 2)}
    world = _incremental_world(counts=counts)
    assert world.run(replace(_LIMITS, **{limit: total})).status is BuildStatus.PUBLISHED

    world = _incremental_world(counts=counts)
    outcome = world.run(replace(_LIMITS, **{limit: total - 1}))
    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, f"limit_exceeded:{limit}")


def test_the_repository_quota_counts_the_published_bytes_too() -> None:
    # 100 published bytes and 100 per written file: the second write is over 250.
    world = _world(state=_state(stored_bytes=100), bytes_per_file=100)
    admission = replace(_ADMISSION, repository_storage_bytes=250)

    outcome = world.run(settings=replace(_SETTINGS, admission=admission, batch_files=1))

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:repository_storage",
    )
    assert len(world.store.written) == 2
    assert world.store.publications == []


def test_the_workspace_quota_counts_the_sum_of_the_write_and_rolls_the_batch_back() -> None:
    # The other repositories of the Workspace hold 850 bytes: the second file of the
    # batch brings the sum that `replace_file` returns to 1050, over 1000.
    world = _world(bytes_per_file=100, other_bytes=850)
    admission = replace(_ADMISSION, workspace_storage_bytes=1_000)

    outcome = world.run(settings=replace(_SETTINGS, admission=admission, batch_files=2))

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.FAILED,
        "limit_exceeded:workspace_storage",
    )
    # Both writes ran in one transaction, and it ended without a commit.
    assert len({transaction for transaction, _ in world.store.written}) == 1
    log = world.tracker.log
    assert log[log.index("replace_file") :] == [
        "replace_file",
        "replace_file",
        "mark_failed",
        "commit",
    ]
    assert world.store.publications == []


def test_a_passed_deadline_fails_the_index() -> None:
    world = _world()

    outcome = world.run(replace(_LIMITS, deadline_seconds=0))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.network() == []


def test_the_deadline_stops_the_parse_pass_between_files() -> None:
    world = _world()
    world.parser.first_delay = 1.0

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    assert world.parser.parsed == ["app/models.py"]
    assert world.store.publications == []


def test_the_deadline_interrupts_a_hanging_vcs_call() -> None:
    world = _world()
    world.source.hang = "archive"

    outcome = world.run(replace(_LIMITS, deadline_seconds=1))

    assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, "limit_exceeded:deadline")
    ((_, stats),) = world.store.failures
    assert stats is not None
    assert (stats.downloaded_bytes, stats.github_request_count) == (0, 2)


@pytest.mark.parametrize(
    ("step", "error", "status", "code", "retry_after"),
    [
        (
            "head",
            VcsUnavailable("rate", rate_limited=True, retry_after=timedelta(seconds=120)),
            BuildStatus.TRANSIENT,
            "vcs_rate_limited",
            timedelta(seconds=120),
        ),
        ("archive", VcsUnavailable("down"), BuildStatus.TRANSIENT, "vcs_unavailable", None),
        ("compare", VcsAccessDenied("gone"), BuildStatus.RELEASED, "vcs_access_denied", None),
    ],
)
def test_a_vcs_failure_releases_the_lease_with_its_code(
    step: str,
    error: BaseException,
    status: BuildStatus,
    code: str,
    retry_after: timedelta | None,
) -> None:
    world = _world(state=_state())
    world.source.errors[step] = error

    outcome = world.run()

    assert (outcome.status, outcome.error_code, outcome.retry_after) == (status, code, retry_after)
    assert world.store.releases == [code]
    assert world.store.failures == []


@pytest.mark.parametrize(
    ("rules", "code"),
    [
        (b"```review-ignore\nlegacy/**\n", "policy_ignore_block_unclosed"),
        (Link("../README.md"), "policy_document_type_unsupported"),
        (b"x" * 65_537, "policy_document_too_large"),
        (b"x" * (_MIB + 1), "policy_document_too_large"),
        (b"\xff\xfe", "policy_document_decode_error"),
    ],
)
def test_a_policy_error_releases_the_lease_with_the_policy_code(
    rules: bytes | Link, code: str
) -> None:
    world = _world({**_TREE, _RULES: rules})

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (BuildStatus.RELEASED, code)
    assert world.store.releases == [code]
    assert world.store.written == []


def test_a_review_directory_that_is_not_a_directory_is_a_policy_error() -> None:
    world = _world({**_TREE, ".review": Link("elsewhere")})

    outcome = world.run()

    assert (outcome.status, outcome.error_code) == (
        BuildStatus.RELEASED,
        "policy_document_type_unsupported",
    )


def test_the_ignore_policy_of_head_excludes_its_paths() -> None:
    rules = b"```review-ignore\napp/views.py\n```\n"
    world = _world({**_TREE, _RULES: rules})

    outcome = world.run()

    assert world.written() == ["app/models.py"]
    assert outcome.exclusions[ExclusionClass.IGNORED] == 1
    profile = world.store.publications[0]["profile"]
    assert profile.exclusions_digest == parse_review_ignore(rules.decode()).digest


def test_an_unexpected_error_releases_the_lease_and_propagates() -> None:
    world = _world()
    world.parser.error = RuntimeError("parser crashed")

    with pytest.raises(RuntimeError, match="parser crashed"):
        world.run()

    assert world.store.releases == [None]
    assert world.store.failures == []


def _unfinished_discard(world: World) -> None:
    world.store.discards = [2]


def _policy_error(world: World) -> None:
    world.archives.tree = {**world.archives.tree, _RULES: b"```review-ignore\nlegacy/**\n"}


def _unavailable_archive(world: World) -> None:
    world.source.errors["archive"] = VcsUnavailable("down")


def _failing_source_factory(world: World) -> None:
    world.source_error = RuntimeError("no source for this repository")


@pytest.mark.parametrize(
    ("arrange", "status", "downloaded", "requests"),
    [
        pytest.param(_unfinished_discard, BuildStatus.REWAKE, 0, 0, id="soft-cutoff"),
        pytest.param(_empty, BuildStatus.EMPTY, 0, 1, id="empty"),
        pytest.param(_unchanged, BuildStatus.UNCHANGED, 0, 1, id="unchanged"),
        pytest.param(_access_denied, BuildStatus.RELEASED, 0, 1, id="released-access"),
        pytest.param(_policy_error, BuildStatus.RELEASED, 7, 3, id="released-policy"),
        pytest.param(_unavailable_archive, BuildStatus.TRANSIENT, 0, 3, id="transient"),
        pytest.param(_crashing_parser, None, 7, 3, id="after-an-error"),
        pytest.param(_failing_source_factory, None, 0, 0, id="after-the-claim"),
    ],
)
def test_every_release_records_the_job_metrics(
    arrange: Any, status: BuildStatus | None, downloaded: int, requests: int
) -> None:
    world = _world(state=_state())
    arrange(world)
    # A full discard portion outlasts the zero soft cutoff; the other paths discard none.
    settings = replace(_SETTINGS, discard_batch_rows=2, soft_cutoff_seconds=0)

    if status is None:
        with pytest.raises(RuntimeError):
            world.run(settings=settings)
    else:
        assert world.run(settings=settings).status is status

    (stats,) = world.store.release_stats
    assert stats is not None
    assert stats == IndexBuildStats(downloaded, stats.build_duration_ms, requests)
    assert stats.build_duration_ms >= 0


@pytest.mark.parametrize(
    ("arrange", "held", "failures", "releases"),
    [
        pytest.param(_unsafe_archive, "mark_failed", ["archive_unsafe"], [], id="mark-failed"),
        pytest.param(_unavailable_head, "release", [], ["vcs_unavailable"], id="release"),
        pytest.param(_crashing_parser, "release", [], [None], id="release-after-an-error"),
    ],
)
def test_a_cancellation_during_the_closing_write_does_not_cut_it(
    arrange: Any, held: str, failures: list[str], releases: list[str | None]
) -> None:
    world = _world(hold=held)
    arrange(world)

    async def scenario() -> None:
        job = asyncio.create_task(world.use_case().execute(_REPO, _GENERATION))
        await asyncio.wait_for(world.store.held.wait(), 5)
        job.cancel()
        with pytest.raises(asyncio.CancelledError):
            await job
        # The shielded write goes on after the job is cancelled.
        world.store.proceed.set()
        await asyncio.wait_for(world.store.finished.wait(), 5)

    asyncio.run(scenario())

    assert [code for code, _ in world.store.failures] == failures
    assert world.store.releases == releases
    assert world.tracker.log[-2:] == [held, "commit"]


def test_no_transaction_is_open_during_a_vcs_call_or_a_wake_up() -> None:
    world = _incremental_world(after_publish=_state(target_commit_sha="d" * 40))

    world.run()

    # The tracker asserts it at every call; this pins the interleaving of one build.
    assert world.tracker.log == [
        "claim_lease",
        "commit",
        "discard_abandoned",
        "commit",
        "head",
        "get_state",
        "compare",
        "archive",
        "iter_files",
        "iter_imports",
        "visible_counts",
        "replace_file",
        "replace_file",
        "commit",
        "publish",
        "commit",
        "get_state",
        "wake",
    ]


def test_the_skip_counters_equal_one_per_exclusion_class() -> None:
    tree: Tree = {
        "bad\\path.py": b"x = 1\n",
        "config/.env.local": b"TOKEN=1\n",
        "vendor/lib.py": b"x = 1\n",
        "dist/app.js": b"x = 1\n",
        "README.md": b"# readme\n",
        "legacy/old.py": b"x = 1\n",
        "app/link.py": Link("models.py"),
        "app/huge.py": b"#" * (_MIB + 1),
        "app/large.py": b"#" * (256 * 1024 + 1),
        "app/blob.py": b"\x00binary",
        "app/broken.py": b"\xff\xfe = 1\n",
        "app/models.py": _MODELS,
        _RULES: b"```review-ignore\nlegacy/**\n```\n",
    }
    world = _world(tree)

    outcome = world.run()

    assert dict(outcome.exclusions) == {
        ExclusionClass.UNSUPPORTED_PATH: 1,
        ExclusionClass.SECRET: 1,
        ExclusionClass.VENDORED: 1,
        ExclusionClass.GENERATED: 1,
        # README.md and the rules document itself.
        ExclusionClass.UNSUPPORTED_LANGUAGE: 2,
        ExclusionClass.IGNORED: 1,
        ExclusionClass.SYMLINK: 1,
        ExclusionClass.OVER_READ_LIMIT: 1,
        ExclusionClass.OVER_PARSE_LIMIT: 1,
        ExclusionClass.BINARY: 1,
        ExclusionClass.PARSE_FAILED: 1,
    }
    assert outcome.skipped_unsupported_language == 2
    assert outcome.skipped_too_large == 2
    assert outcome.parse_failed == 1
    assert world.written() == ["app/models.py"]
    # The binary file is never parsed; the undecodable one is.
    assert world.parser.parsed == ["app/broken.py", "app/models.py"]


def test_one_log_line_ends_the_job_without_paths_or_content(
    caplog: pytest.LogCaptureFixture,
) -> None:
    world = _incremental_world()

    with caplog.at_level(logging.INFO, logger="app.modules.code_index.application.build_index"):
        world.run()

    (record,) = [
        item
        for item in caplog.records
        if item.name == "app.modules.code_index.application.build_index"
    ]
    message = record.getMessage()
    assert "status=published" in message
    assert "mode=incremental" in message
    assert str(_REPO) in message
    for path in _OLD_TREE:
        assert path not in message


def _file(world: World, path: str) -> IndexedFile:
    return next(file for _, file in world.store.written if file.path == path)


def test_targets_later_in_the_archive_resolve_through_their_fact_summaries() -> None:
    # The importers come first in archive order: target facts are read in a pass of
    # their own before any importer is resolved.
    tree: Tree = {
        "cmd/main.go": b'package main\n\nimport "example.com/m/pkg"\n\nfunc main() { pkg.Run() }\n',
        "app.py": b"from pkg import sub\n\n\ndef go():\n    return sub.work()\n",
        "big/main.go": b'package main\n\nimport "example.com/m/huge"\n\nfunc main() { huge.X() }\n',
        "pkg/run.go": b"package pkg\n\nfunc Run() {}\n",
        "pkg/__init__.py": b"",
        "pkg/sub.py": b"def work():\n    return 1\n",
        "huge/x.go": b"package huge\n\nfunc X() {}\n" + b"//" * (128 * 1024 + 1),
        "go.mod": b"module example.com/m\n",
    }
    world = _world(tree)

    world.run()

    main = _file(world, "cmd/main.go")
    assert [(r.specifier, r.resolution, r.target_path) for r in main.imports] == [
        ("example.com/m/pkg", ImportResolution.RESOLVED, "pkg")
    ]
    assert [(e.kind.value, e.target_path, e.target_name) for e in main.edges] == [
        ("imports", "pkg/run.go", ""),
        ("uses_imported", "pkg/run.go", "Run"),
    ]
    app = _file(world, "app.py")
    assert [(e.kind.value, e.target_path, e.target_name) for e in app.edges] == [
        ("imports", "pkg/__init__.py", ""),
        ("imports", "pkg/sub.py", ""),
        ("uses_imported", "pkg/sub.py", "work"),
    ]
    # A member over the parse limit leaves its package unresolved, as in the review.
    big = _file(world, "big/main.go")
    assert [(r.resolution, r.target_path) for r in big.imports] == [
        (ImportResolution.UNSUPPORTED, "huge")
    ]
    assert big.edges == ()
