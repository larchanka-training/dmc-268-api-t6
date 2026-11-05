"""PostgreSQL evidence for the graph indexer (#142).

A fake VCS serves a tarball per commit; the archive reader, the parser, the resolver and
the store are the real ones. AC6: an incremental build of repository A equals a fresh
full build of repository B at the same commit, compared as the normalized sets of files,
symbols, edges and import records. Each scenario also proves that A really built
incrementally and that the sets changed.

Opt-in: TEST_DATABASE_URL must target a test database whose user can create schemas.
Each test migrates its own schema to head and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import tarfile
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.modules.code_index.application.build_index import (
    BuildMode,
    BuildOutcome,
    BuildRepositoryIndex,
    BuildStatus,
    IndexBuildSettings,
    IndexVersions,
    limits_profile_version,
    parser_profile_version,
)
from app.modules.code_index.application.index_resolution import INDEX_RESOLVER_VERSION
from app.modules.code_index.application.index_store import (
    ImportRecord,
    IndexAdmission,
    IndexedEdge,
    IndexedSymbol,
    IndexState,
)
from app.modules.code_index.application.ports import IndexedRepository
from app.modules.code_index.application.vcs import (
    Ancestry,
    BranchHead,
    ChangedPath,
    ChangeStatus,
    Comparison,
    IndexLimitExceeded,
    RepositorySnapshotSource,
    SourceArchive,
)
from app.modules.code_index.domain.enums import EdgeKind, PurgeReason
from app.modules.code_index.domain.limits import IndexLimits
from app.modules.code_index.infrastructure.index_store import SqlAlchemyCodeIndexUnitOfWork
from app.modules.code_index.infrastructure.repository_reader import (
    SqlAlchemyIndexRepositoryReader,
)
from app.modules.code_index.infrastructure.tar_archive import TarArchiveReader
from app.modules.reviews.application.ast_resolution import ImportResolution
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
    SourceParser,
)
from app.modules.reviews.application.policy_documents import missing_document, normalize_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import PolicyDocument
from app.modules.reviews.infrastructure.subprocess_source_parser import SubprocessSourceParser
from app.modules.reviews.infrastructure.tree_sitter_facts import QUERY_VERSION, profile_version

_MIB = 1_048_576
_KIB = 1_024
_LIMITS = IndexLimits()
# As the composition root computes them (plan, "Versions").
_VERSIONS = IndexVersions(
    extractor_version=QUERY_VERSION,
    resolver_version=INDEX_RESOLVER_VERSION,
    parser_profile_version=parser_profile_version(
        profile_version(language) for language in AstLanguage
    ),
    limits_profile_version=limits_profile_version(_LIMITS),
)
_ADMISSION = IndexAdmission(2, 1, 512 * _MIB, 2_048 * _MIB)
_SETTINGS = IndexBuildSettings(admission=_ADMISSION)


@dataclass(frozen=True)
class _Database:
    factory: async_sessionmaker[AsyncSession]
    a: UUID
    b: UUID


# --- the fake VCS --------------------------------------------------------------------


@dataclass(frozen=True)
class Link:
    """A symlink of a tree."""

    target: str


Tree = Mapping[str, bytes | Link]


def _sha(number: int) -> str:
    return f"{number:02x}" * 20


@dataclass
class Branch:
    """A linear default branch: every pushed tree is a commit; HEAD is the last one."""

    commits: list[tuple[str, Tree]] = field(default_factory=list)
    export_ignored: frozenset[str] = frozenset()
    policy_errors: dict[str, PolicyError] = field(default_factory=dict)

    def push(self, tree: Tree) -> str:
        sha = _sha(len(self.commits) + 1)
        self.commits.append((sha, dict(tree)))
        return sha

    @property
    def head(self) -> str | None:
        return self.commits[-1][0] if self.commits else None

    def tree(self, sha: str) -> Tree:
        return dict(self.commits)[sha]

    def position(self, sha: str) -> int | None:
        return next((index for index, (item, _) in enumerate(self.commits) if item == sha), None)


def _changes(old: Tree, new: Tree) -> tuple[ChangedPath, ...]:
    """What a provider compare lists: an exact-content move is a rename."""
    removed = [path for path in old if path not in new]
    renamed: dict[str, str] = {}
    for path in (path for path in new if path not in old):
        source = next(
            (item for item in removed if old[item] == new[path] and item not in renamed.values()),
            None,
        )
        if source is not None:
            renamed[path] = source
    changes = []
    for path in sorted(set(old) | set(new)):
        if path in renamed:
            changes.append(ChangedPath(path, ChangeStatus.RENAMED, renamed[path]))
        elif path in renamed.values():
            continue
        elif path not in old:
            changes.append(ChangedPath(path, ChangeStatus.ADDED))
        elif path not in new:
            changes.append(ChangedPath(path, ChangeStatus.REMOVED))
        elif old[path] != new[path]:
            changes.append(ChangedPath(path, ChangeStatus.MODIFIED))
    return tuple(changes)


def _tarball(sha: str, tree: Tree) -> bytes:
    """A GitHub-like tarball: a global pax header with the commit, then one root."""
    buffer = io.BytesIO()
    root = f"octo-repo-{sha[:7]}"
    with tarfile.open(
        fileobj=buffer,
        mode="w:gz",
        format=tarfile.PAX_FORMAT,
        pax_headers={"comment": sha},
        compresslevel=1,
    ) as archive:
        directory = tarfile.TarInfo(root)
        directory.type = tarfile.DIRTYPE
        archive.addfile(directory)
        for path, value in tree.items():
            info = tarfile.TarInfo(f"{root}/{path}")
            if isinstance(value, Link):
                info.type = tarfile.SYMTYPE
                info.linkname = value.target
                archive.addfile(info)
            else:
                info.size = len(value)
                archive.addfile(info, io.BytesIO(value))
    return buffer.getvalue()


@dataclass
class Vcs:
    """`RepositorySnapshotSource` of one job over a `Branch`."""

    branch: Branch
    requests: int = 0
    compares: int = 0

    @property
    def request_count(self) -> int:
        return self.requests

    async def head(self) -> BranchHead | None:
        # As the GitHub source counts them: the repository, then its branch commit.
        self.requests += 2
        head = self.branch.head
        return None if head is None else BranchHead("main", head)

    async def compare(self, base_sha: str, head_sha: str) -> Comparison | None:
        self.requests += 1
        self.compares += 1
        base, head = self.branch.position(base_sha), self.branch.position(head_sha)
        if base is None or head is None:
            return None
        ancestry = Ancestry.AHEAD if base < head else Ancestry.BEHIND
        if base == head:
            ancestry = Ancestry.IDENTICAL
        changes = _changes(self.branch.tree(base_sha), self.branch.tree(head_sha))
        return Comparison(ancestry, changes, complete=True)

    async def rules_document(self, commit_sha: str) -> PolicyDocument:
        # Mirror real GitHub requests, including proof of absence via complete trees.
        tree = self.branch.tree(commit_sha)
        self.requests += 1
        if commit_sha in self.branch.policy_errors:
            raise self.branch.policy_errors[commit_sha]
        if ".review" in tree:
            raise PolicyError(PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED, "nonregular parent")
        if not any(path.startswith(".review/") for path in tree):
            return missing_document(".review/rules.md")
        self.requests += 1
        raw = tree.get(".review/rules.md")
        if isinstance(raw, Link):
            raise PolicyError(PolicyErrorCode.DOCUMENT_TYPE_UNSUPPORTED, "nonregular policy")
        if raw is None:
            return missing_document(".review/rules.md")
        self.requests += 1
        return normalize_document(
            ".review/rules.md",
            raw,
            blob_oid=hashlib.sha1(b"blob %d\0" % len(raw) + raw).hexdigest(),
            policy_base_sha=commit_sha,
        )

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        # The tarball request and its codeload redirect.
        self.requests += 2
        original = self.branch.tree(commit_sha)
        archive_tree = {
            path: value
            for path, value in original.items()
            if not any(
                path == excluded or path.startswith(excluded.rstrip("/") + "/")
                for excluded in self.branch.export_ignored
            )
        }
        content = _tarball(commit_sha, archive_tree)
        if len(content) > max_bytes:
            raise IndexLimitExceeded("download_bytes")
        return SourceArchive(commit_sha, content)


@dataclass
class Wakeups:
    woken: list[tuple[UUID, int]] = field(default_factory=list)

    async def wake(self, repository_id: UUID, generation: int) -> None:
        self.woken.append((repository_id, generation))


# --- the indexer over PostgreSQL -------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """The visible rows of a repository's index, normalized: rows carry paths only."""

    files: frozenset[tuple[str, str]]
    symbols: frozenset[IndexedSymbol]
    edges: frozenset[IndexedEdge]
    imports: frozenset[ImportRecord]


@dataclass
class Indexer:
    database: _Database
    parser: SourceParser
    branch: Branch = field(default_factory=Branch)
    limits: IndexLimits = _LIMITS
    settings: IndexBuildSettings = _SETTINGS
    jobs: list[Vcs] = field(default_factory=list)
    wakeups: Wakeups = field(default_factory=Wakeups)

    def uow(self) -> SqlAlchemyCodeIndexUnitOfWork:
        return SqlAlchemyCodeIndexUnitOfWork(self.database.factory)

    def _source(self, repository: IndexedRepository) -> Vcs:
        job = Vcs(self.branch)
        self.jobs.append(job)
        return job

    async def state(self, repository_id: UUID) -> IndexState:
        async with self.uow() as uow:
            state = await uow.index.get_state(repository_id)
        assert state is not None
        return state

    async def enable(self, repository_id: UUID) -> None:
        async with self.uow() as uow:
            await uow.index.enable(repository_id)
            await uow.commit()

    async def purge(self, repository_id: UUID) -> None:
        async with self.uow() as uow:
            await uow.index.purge(repository_id, PurgeReason.DISABLED)
            await uow.commit()

    async def build(
        self,
        repository_id: UUID,
        generation: int | None = None,
        *,
        source: Callable[[IndexedRepository], RepositorySnapshotSource] | None = None,
    ) -> BuildOutcome:
        """A job for the current generation, or for `generation` (an old message)."""
        use_case = BuildRepositoryIndex(
            uow=self.uow,
            repositories=SqlAlchemyIndexRepositoryReader(self.database.factory),
            sources=self._source if source is None else source,
            archives=TarArchiveReader(self.limits),
            parser=self.parser,
            wakeups=self.wakeups,
            versions=replace(_VERSIONS, limits_profile_version=limits_profile_version(self.limits)),
            limits=self.limits,
            settings=self.settings,
        )
        if generation is None:
            generation = (await self.state(repository_id)).generation
        return await use_case.execute(repository_id, generation)

    async def fresh_full(self, repository_id: UUID) -> BuildOutcome:
        """A full build from scratch: the toggle off and on again."""
        await self.purge(repository_id)
        await self.enable(repository_id)
        outcome = await self.build(repository_id)
        assert (outcome.status, outcome.mode) == (BuildStatus.PUBLISHED, BuildMode.FULL)
        return outcome

    async def snapshot(self, repository_id: UUID) -> Snapshot:
        files: list[tuple[str, str]] = []
        symbols: set[IndexedSymbol] = set()
        edges: set[IndexedEdge] = set()
        imports: set[ImportRecord] = set()
        async with self.uow() as uow:
            after = None
            while page := await uow.index.iter_files(repository_id, after, 100):
                files.extend(page)
                after = page[-1][0]
            for path, _ in files:
                file = await uow.index.get_file(repository_id, path)
                assert file is not None
                symbols.update(file.symbols)
                edges.update(file.edges)
                imports.update(file.imports)
        return Snapshot(frozenset(files), frozenset(symbols), frozenset(edges), frozenset(imports))

    async def incremental_equals_full(self, outcome: BuildOutcome, before: Snapshot) -> Snapshot:
        """AC6: A built incrementally, and a fresh full build of B at HEAD equals it."""
        assert (outcome.status, outcome.mode) == (BuildStatus.PUBLISHED, BuildMode.INCREMENTAL)
        assert self.jobs[-1].compares == 1
        incremental = await self.snapshot(self.database.a)
        await self.fresh_full(self.database.b)
        assert incremental == await self.snapshot(self.database.b)
        assert incremental.edges or incremental.imports
        assert incremental != before
        state = await self.state(self.database.a)
        assert state.indexed_commit_sha == self.branch.head
        return incremental

    async def start(self, tree: Tree) -> Snapshot:
        """Push the first commit and build A in full; B is enabled for comparisons."""
        await self.enable(self.database.a)
        await self.enable(self.database.b)
        self.branch.push(tree)
        outcome = await self.build(self.database.a)
        assert (outcome.status, outcome.mode) == (BuildStatus.PUBLISHED, BuildMode.FULL)
        return await self.snapshot(self.database.a)


def _run(
    database: _Database,
    scenario: Callable[[Indexer], Awaitable[None]],
    *,
    limits: IndexLimits = _LIMITS,
    settings: IndexBuildSettings = _SETTINGS,
    parser: Callable[[SourceParser], SourceParser] | None = None,
) -> None:
    async def run() -> None:
        # The indexer's own parser: no CPU budget per attempt, only the per-file limit.
        real = SubprocessSourceParser(attempt_cpu_seconds=None)
        try:
            used = parser(real) if parser is not None else real
            await scenario(Indexer(database, used, limits=limits, settings=settings))
        finally:
            await real.aclose()

    asyncio.run(run())


def _imports(path: str, target: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.IMPORTS, target, "")


def _uses(path: str, target: str, name: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.USES_IMPORTED, target, name)


def _record(
    path: str, specifier: str, resolution: ImportResolution, target: str | None = None
) -> ImportRecord:
    return ImportRecord(path, specifier, resolution, target)


RESOLVED = ImportResolution.RESOLVED
EXTERNAL = ImportResolution.EXTERNAL


# --- AC6: incremental = full ------------------------------------------------------------

_USER = b"from lib import target\n\n\ndef use():\n    return target()\n"
_GO_MOD = b"module example.com/m\n"


async def _raw_rows(database: _Database, repository_id: UUID) -> tuple[int, ...]:
    async with database.factory() as session:
        counts = []
        for table in ("files", "symbols", "edges", "imports"):
            counts.append(
                await session.scalar(
                    text(f"SELECT count(*) FROM code_index_{table} WHERE repository_id = :id"),
                    {"id": repository_id},
                )
            )
        return tuple(counts)


_PACKAGE: Tree = {
    "pkg/__init__.py": b"def one():\n    return 1\n",
    "pkg/a.py": b"from pkg import one\n\n\ndef a1():\n    return one()\n",
    "pkg/b.py": b"from pkg.a import a1\n\n\nclass B:\n    def m(self):\n        return a1()\n",
    "top.py": b"import pkg.a\nfrom pkg import one\n\n\ndef t():\n    return one()\n",
}
# Rule I: the changed `__init__.py` re-extracts `pkg/a.py` and `top.py`.
_PACKAGE_V2: Tree = {
    **_PACKAGE,
    "pkg/__init__.py": b"def one():\n    return 1\n\n\ndef extra():\n    return 3\n",
}


@dataclass
class _ActOnParse:
    """The parser, running `act` just before its `call`-th file; 0 never runs it."""

    inner: SourceParser
    act: Callable[[], Awaitable[None]]
    call: int
    calls: int = 0

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.calls += 1
        if self.calls == self.call:
            await self.act()
        return await self.inner.parse_many(requests)

    async def aclose(self) -> None:
        await self.inner.aclose()
