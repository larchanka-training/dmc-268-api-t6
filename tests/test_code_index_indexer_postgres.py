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
import io
import os
import tarfile
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field, replace
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import Connection, create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.schema import CreateSchema, DropSchema

from alembic import command
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
from app.modules.code_index.application.exclusions import ExclusionClass
from app.modules.code_index.application.index_resolution import INDEX_RESOLVER_VERSION
from app.modules.code_index.application.index_store import (
    ImportRecord,
    IndexAdmission,
    IndexBuildStats,
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
    SourceArchive,
)
from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, PurgeReason
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


@pytest.fixture
def code_index_database() -> Iterator[Connection]:
    database_url = os.environ.get("TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip("set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"test_code_indexer_{uuid4().hex}"
    engine = create_engine(database_url)
    try:
        with engine.connect() as connection:
            connection.execute(CreateSchema(schema))
            connection.execute(text(f'SET search_path TO "{schema}"'))
            connection.commit()
            try:
                config = Config("alembic.ini")
                config.attributes["connection"] = connection
                command.upgrade(config, "head")
                yield connection
            finally:
                connection.rollback()
                connection.execute(text("SET search_path TO public"))
                connection.execute(DropSchema(schema, cascade=True))
                connection.commit()
    finally:
        engine.dispose()


def _repository(connection: Connection, installation_external_id: int, full_name: str) -> UUID:
    workspace_id, installation_id, repository_id = uuid4(), uuid4(), uuid4()
    connection.execute(
        text("INSERT INTO workspaces (id, name, daily_budget_usd) VALUES (:id, 'acme', 5)"),
        {"id": workspace_id},
    )
    connection.execute(
        text(
            "INSERT INTO provider_installations "
            "(id, workspace_id, provider, external_id, metadata) "
            "VALUES (:id, :workspace, 'github', :external_id, '{\"account_login\": \"octo\"}')"
        ),
        {"id": installation_id, "workspace": workspace_id, "external_id": installation_external_id},
    )
    connection.execute(
        text(
            "INSERT INTO repositories "
            "(id, provider_installation_id, external_id, full_name, default_branch, web_url) "
            "VALUES (:id, :installation, 101, :full_name, 'main', 'https://github.com/x')"
        ),
        {"id": repository_id, "installation": installation_id, "full_name": full_name},
    )
    return repository_id


@dataclass(frozen=True)
class _Database:
    factory: async_sessionmaker[AsyncSession]
    a: UUID
    b: UUID


@pytest.fixture
def database(code_index_database: Connection) -> Iterator[_Database]:
    """Two repositories in their own installations, committed for async sessions."""
    connection = code_index_database
    repository_a = _repository(connection, 17, "octo/indexed-a")
    repository_b = _repository(connection, 18, "octo/indexed-b")
    connection.commit()
    schema = connection.scalar(text("SELECT current_schema()"))
    engine = create_async_engine(
        os.environ["TEST_DATABASE_URL"],
        connect_args={"options": f"-csearch_path={schema}"},
        poolclass=NullPool,
    )
    try:
        yield _Database(
            async_sessionmaker(engine, expire_on_commit=False), repository_a, repository_b
        )
    finally:
        asyncio.run(engine.dispose())


@pytest.mark.integration
def test_the_repository_reader_names_the_repository_and_its_installation(
    database: _Database,
) -> None:
    reader = SqlAlchemyIndexRepositoryReader(database.factory)

    async def scenario() -> tuple[IndexedRepository | None, ...]:
        return (
            await reader.get(database.a),
            await reader.get(database.b),
            await reader.get(uuid4()),
        )

    assert asyncio.run(scenario()) == (
        IndexedRepository(database.a, "github", 17, "octo/indexed-a"),
        IndexedRepository(database.b, "github", 18, "octo/indexed-b"),
        None,
    )


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

    async def archive(self, commit_sha: str, *, max_bytes: int) -> SourceArchive:
        # The tarball request and its codeload redirect.
        self.requests += 2
        content = _tarball(commit_sha, self.branch.tree(commit_sha))
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

    async def build(self, repository_id: UUID) -> BuildOutcome:
        use_case = BuildRepositoryIndex(
            uow=self.uow,
            repositories=SqlAlchemyIndexRepositoryReader(self.database.factory),
            sources=self._source,
            archives=TarArchiveReader(self.limits),
            parser=self.parser,
            wakeups=self.wakeups,
            versions=replace(_VERSIONS, limits_profile_version=limits_profile_version(self.limits)),
            limits=self.limits,
            settings=self.settings,
        )
        state = await self.state(repository_id)
        return await use_case.execute(repository_id, state.generation)

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


@pytest.mark.integration
def test_ac6_a_declaration_inserted_before_the_target(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start({"lib.py": b"def target():\n    return 1\n", "user.py": _USER})
        indexer.branch.push(
            {
                "lib.py": b"def first():\n    return 0\n\n\ndef target():\n    return 1\n",
                "user.py": _USER,
            }
        )

        outcome = await indexer.build(database.a)
        job = indexer.jobs[-1]
        after = await indexer.incremental_equals_full(outcome, before)

        assert _uses("user.py", "lib.py", "target") in after.edges
        lines = {(s.key.qualified_name, s.start_line) for s in after.symbols}
        assert {("first", 1), ("target", 5)} <= lines
        # HEAD 2, compare 1, archive 2.
        assert (job.requests, outcome.github_request_count) == (5, 5)

    _run(database, scenario)


@pytest.mark.integration
def test_ac6_a_declaration_deleted_and_a_file_renamed(database: _Database) -> None:
    user = b"from a import fa\nfrom b import fb\n\n\ndef use():\n    return fa() + fb()\n"
    module_b = b"def fb():\n    return 2\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(
            {"a.py": b"def fa():\n    return 1\n", "b.py": module_b, "user.py": user}
        )
        indexer.branch.push({"c.py": module_b, "user.py": user})

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        # AC5: deleted and renamed files lose their rows.
        assert {path for path, _ in after.files} == {"c.py", "user.py"}
        assert {(r.specifier, r.resolution) for r in after.imports} == {
            ("a", EXTERNAL),
            ("b", EXTERNAL),
        }

    _run(database, scenario)


@pytest.mark.integration
def test_ac6_an_import_resolution_changed(database: _Database) -> None:
    user = b"from pkg import thing\n\n\ndef use():\n    return thing()\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(
            {"pkg/__init__.py": b"def thing():\n    return 1\n", "user.py": user}
        )
        assert _uses("user.py", "pkg/__init__.py", "thing") in before.edges
        indexer.branch.push(
            {
                "pkg/__init__.py": b"",
                "pkg/thing.py": b"def run():\n    return 2\n",
                "user.py": user,
            }
        )

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        # `thing` is now the submodule, not a symbol of the package (rules I and W).
        assert {e for e in after.edges if e.path == "user.py"} == {
            _imports("user.py", "pkg/__init__.py"),
            _imports("user.py", "pkg/thing.py"),
        }

    _run(database, scenario)


@pytest.mark.integration
def test_ac6_the_target_file_deleted(database: _Database) -> None:
    user = b'import { f } from "./lib";\n\nexport function g() {\n  return f();\n}\n'

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(
            {"lib.ts": b"export function f() {\n  return 1;\n}\n", "user.ts": user}
        )
        assert _uses("user.ts", "lib.ts", "f") in before.edges
        indexer.branch.push({"user.ts": user})

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        assert after.edges == frozenset()
        assert after.imports == {_record("user.ts", "./lib", ImportResolution.MISSING)}

    _run(database, scenario)


@pytest.mark.integration
def test_ac6_a_missed_direct_push(database: _Database) -> None:
    lib = b"def target():\n    return 1\n"
    extra = lib + b"\n\ndef extra():\n    return 2\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start({"lib.py": lib, "user.py": _USER})
        # A direct push nobody indexed, then the commit a job sees.
        indexer.branch.push({"lib.py": extra, "user.py": _USER})
        indexer.branch.push(
            {
                "lib.py": extra,
                "user.py": b"from lib import extra\n\n\ndef use():\n    return extra()\n",
            }
        )

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        assert _uses("user.py", "lib.py", "extra") in after.edges
        assert _uses("user.py", "lib.py", "target") not in after.edges

    _run(database, scenario)


@pytest.mark.integration
def test_ac6_two_merges_before_the_queue_drains(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start({"lib.py": b"def target():\n    return 1\n", "user.py": _USER})
        indexer.branch.push(
            {"lib.py": b"def target():\n    return 1\n", "user.py": _USER, "two.py": b"x = 2\n"}
        )
        indexer.branch.push(
            {
                "lib.py": b"def target():\n    return 3\n",
                "user.py": _USER,
                "two.py": b"x = 2\n",
                "three.py": b"from two import x\n\ny = x\n",
            }
        )

        # Two wake-ups of one generation: the first job takes the latest HEAD.
        first = await indexer.build(database.a)
        after = await indexer.incremental_equals_full(first, before)
        second = await indexer.build(database.a)

        assert second.status is BuildStatus.UNCHANGED
        assert await indexer.snapshot(database.a) == after
        assert _imports("three.py", "two.py") in after.edges

    _run(database, scenario)


# --- incremental = full: the other resolver rules ----------------------------------------


@pytest.mark.integration
def test_a_python_runtime_module_wins_over_its_stub_and_two_roots_tie(database: _Database) -> None:
    user = b"import util\n\n\ndef use():\n    return util.f()\n"
    stub = b"def f() -> int: ...\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start({"util.pyi": stub, "user.py": user})
        assert _uses("user.py", "util.pyi", "f") in before.edges
        runtime = b"def f():\n    return 1\n"
        indexer.branch.push({"util.pyi": stub, "util.py": runtime, "user.py": user})

        middle = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        assert _uses("user.py", "util.py", "f") in middle.edges
        indexer.branch.push(
            {"util.pyi": stub, "util.py": runtime, "src/util.py": runtime, "user.py": user}
        )
        after = await indexer.incremental_equals_full(await indexer.build(database.a), middle)

        assert _record("user.py", "util", ImportResolution.AMBIGUOUS) in after.imports

    _run(database, scenario)


_GO_MOD = b"module example.com/m\n"
_MAIN = b'package main\n\nimport "example.com/m/pkg"\n\nfunc main() {\n\tpkg.Run()\n}\n'


@pytest.mark.integration
def test_a_go_declaration_moved_to_another_member_file(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(
            {
                "go.mod": _GO_MOD,
                "cmd/main.go": _MAIN,
                "pkg/a.go": b"package pkg\n\nfunc Run() {}\n",
                "pkg/b.go": b"package pkg\n\nfunc Other() {}\n",
            }
        )
        assert _uses("cmd/main.go", "pkg/a.go", "Run") in before.edges
        indexer.branch.push(
            {
                "go.mod": _GO_MOD,
                "cmd/main.go": _MAIN,
                "pkg/a.go": b"package pkg\n",
                "pkg/b.go": b"package pkg\n\nfunc Other() {}\n\nfunc Run() {}\n",
            }
        )

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        # Rule G: the importer of the package is re-extracted.
        assert _uses("cmd/main.go", "pkg/b.go", "Run") in after.edges
        assert _record("cmd/main.go", "example.com/m/pkg", RESOLVED, "pkg") in after.imports

    _run(database, scenario)


@pytest.mark.integration
def test_an_external_import_resolves_once_its_file_is_added(database: _Database) -> None:
    user = b"import requests\n\n\ndef use():\n    return requests.get()\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start({"user.py": user})
        assert before.imports == {_record("user.py", "requests", EXTERNAL)}
        indexer.branch.push({"user.py": user, "requests.py": b"def get():\n    return 1\n"})

        after = await indexer.incremental_equals_full(await indexer.build(database.a), before)

        assert _record("user.py", "requests", RESOLVED, "requests.py") in after.imports
        assert _uses("user.py", "requests.py", "get") in after.edges

    _run(database, scenario)


@pytest.mark.integration
def test_a_file_that_became_too_large_binary_or_a_symlink_loses_its_rows(
    database: _Database,
) -> None:
    user = b"from big import g\nfrom link import h\n\n\ndef use():\n    return g() + h()\n"

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(
            {
                "big.py": b"def g():\n    return 1\n",
                "bin.py": b"def b():\n    return 2\n",
                "link.py": b"def h():\n    return 3\n",
                "real.py": b"def h():\n    return 4\n",
                "user.py": user,
            }
        )
        indexer.branch.push(
            {
                "big.py": b"def g():\n    return 1\n" + b"#" * (256 * _KIB),
                "bin.py": b"\x00\x01binary",
                "link.py": Link("real.py"),
                "real.py": b"def h():\n    return 4\n",
                "user.py": user,
            }
        )

        outcome = await indexer.build(database.a)
        after = await indexer.incremental_equals_full(outcome, before)

        assert {path for path, _ in after.files} == {"real.py", "user.py"}
        assert {
            ExclusionClass.OVER_PARSE_LIMIT: 1,
            ExclusionClass.BINARY: 1,
            ExclusionClass.SYMLINK: 1,
        }.items() <= dict(outcome.exclusions).items()
        # A large or binary target still resolves by path; a symlink target is excluded.
        assert {(r.specifier, r.resolution) for r in after.imports} == {
            ("big", RESOLVED),
            ("link", ImportResolution.EXCLUDED),
        }

    _run(database, scenario)


@pytest.mark.integration
def test_a_nul_character_after_the_binary_check_drops_only_its_rows(database: _Database) -> None:
    # The binary check reads the first 8 KiB; PostgreSQL text cannot hold U+0000.
    source = (
        "// " + "x" * 9_000 + "\n"
        'import { a } from "./a\0b";\n'
        'export class A {\n  "m\0n"() {\n    return a();\n  }\n}\n'
        "export function run() {\n  return a();\n}\n"
    ).encode()

    async def scenario(indexer: Indexer) -> None:
        snapshot = await indexer.start({"src/run.ts": source})

        assert {path for path, _ in snapshot.files} == {"src/run.ts"}
        assert {symbol.key.qualified_name for symbol in snapshot.symbols} == {"A", "run"}
        assert snapshot.imports == frozenset()
        assert snapshot.edges == frozenset()

    _run(database, scenario)


# --- AC7, AC12 ---------------------------------------------------------------------------


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


@pytest.mark.integration
def test_a_redelivery_at_the_indexed_head_is_an_unchanged_no_op(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        await indexer.start({"lib.py": b"def target():\n    return 1\n", "user.py": _USER})
        head = indexer.branch.push({"lib.py": b"def target():\n    return 2\n", "user.py": _USER})
        assert (await indexer.build(database.a)).mode is BuildMode.INCREMENTAL
        snapshot, rows = await indexer.snapshot(database.a), await _raw_rows(database, database.a)

        for _ in range(2):
            redelivered = await indexer.build(database.a)
            assert redelivered.status is BuildStatus.UNCHANGED

        assert await indexer.snapshot(database.a) == snapshot
        assert await _raw_rows(database, database.a) == rows
        assert (await indexer.state(database.a)).indexed_commit_sha == head

    _run(database, scenario)


@pytest.mark.integration
def test_a_job_released_without_a_publication_records_its_metrics(database: _Database) -> None:
    async def scenario(indexer: Indexer) -> None:
        await indexer.start({"lib.py": b"def target():\n    return 1\n", "user.py": _USER})
        published = await indexer.state(database.a)
        # The full build: the repository, its branch commit, the tarball and its redirect.
        assert published.stats.github_request_count == 4
        assert published.stats.downloaded_bytes > 0

        outcome = await indexer.build(database.a)

        assert outcome.status is BuildStatus.UNCHANGED
        state = await indexer.state(database.a)
        # The release wrote this job's metrics; the publication's counters stay.
        assert state.stats == IndexBuildStats(0, state.stats.build_duration_ms, 2)
        assert (state.status, state.file_count, state.symbol_count, state.stored_bytes) == (
            IndexStatus.READY,
            published.file_count,
            published.symbol_count,
            published.stored_bytes,
        )

    _run(database, scenario)


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
class _CrashOnParse:
    """The parser, raising at its `call`-th file; 0 never raises."""

    inner: SourceParser
    call: int = 0
    calls: int = 0

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.calls += 1
        if self.calls == self.call:
            raise RuntimeError("the parser crashed")
        return await self.inner.parse_many(requests)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.mark.integration
def test_a_retry_after_a_crash_in_an_incremental_build_equals_a_fresh_full_build(
    database: _Database,
) -> None:
    parsers: list[_CrashOnParse] = []

    def crashing(real: SourceParser) -> SourceParser:
        parsers.append(_CrashOnParse(real))
        return parsers[0]

    async def scenario(indexer: Indexer) -> None:
        before = await indexer.start(_PACKAGE)
        indexed, rows = indexer.branch.head, await _raw_rows(database, database.a)
        indexer.branch.push(_PACKAGE_V2)
        parser = parsers[0]
        # The summary of `pkg/__init__.py`, then one file per batch: the third parse
        # crashes after the attempt wrote `pkg/__init__.py`.
        parser.calls, parser.call = 0, 3

        with pytest.raises(RuntimeError, match="crashed"):
            await indexer.build(database.a)

        # The attempt's rows stay behind, unpublished; the lease is released.
        assert await _raw_rows(database, database.a) != rows
        state = await indexer.state(database.a)
        assert (state.indexed_commit_sha, state.lease_expires_at) == (indexed, None)
        parser.call = 0
        await indexer.incremental_equals_full(await indexer.build(database.a), before)

    _run(database, scenario, settings=replace(_SETTINGS, batch_files=1), parser=crashing)


@dataclass
class _GateOnParse:
    """The parser, parking its first file until `go` is set."""

    inner: SourceParser
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    go: asyncio.Event = field(default_factory=asyncio.Event)
    calls: int = 0

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.calls += 1
        if self.calls == 1:
            self.reached.set()
            await self.go.wait()
        return await self.inner.parse_many(requests)

    async def aclose(self) -> None:
        await self.inner.aclose()


@pytest.mark.integration
def test_a_job_that_resumes_after_its_lease_was_taken_over_writes_nothing(
    database: _Database,
) -> None:
    gates: list[_GateOnParse] = []

    def gated(real: SourceParser) -> SourceParser:
        gates.append(_GateOnParse(real))
        return gates[0]

    async def scenario(indexer: Indexer) -> None:
        gate = gates[0]
        await indexer.enable(database.a)
        indexer.branch.push(_PACKAGE)
        zombie = asyncio.create_task(indexer.build(database.a))
        await asyncio.wait_for(gate.reached.wait(), 30)
        # The parked job read the first HEAD; its lease expires, and a second job takes
        # it over and publishes a later HEAD.
        head = indexer.branch.push(_PACKAGE_V2)
        async with indexer.database.factory() as session:
            await session.execute(
                text(
                    "UPDATE code_index_states SET lease_expires_at = now() - interval '1 second'"
                    " WHERE repository_id = :id"
                ),
                {"id": database.a},
            )
            await session.commit()
        second = await indexer.build(database.a)
        assert (second.status, second.commit_sha) == (BuildStatus.PUBLISHED, head)
        rows = await _raw_rows(database, database.a)

        gate.go.set()
        first = await asyncio.wait_for(zombie, 60)

        assert first.status is BuildStatus.LEASE_LOST
        assert await _raw_rows(database, database.a) == rows
        assert (await indexer.state(database.a)).indexed_commit_sha == head

    _run(database, scenario, parser=gated)


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


@pytest.mark.integration
def test_a_purge_between_batches_stops_the_job_without_writes(database: _Database) -> None:
    holder: list[Indexer] = []

    async def purge() -> None:
        await holder[0].purge(database.a)

    async def scenario(indexer: Indexer) -> None:
        holder.append(indexer)
        await indexer.enable(database.a)
        indexer.branch.push({f"m{index}.py": f"x{index} = 1\n".encode() for index in range(3)})

        outcome = await indexer.build(database.a)

        assert outcome.status is BuildStatus.LEASE_LOST
        # Only the batch before the purge was written; nothing was published.
        assert outcome.written_files == 0
        assert await _raw_rows(database, database.a) == (1, 1, 0, 0)
        state = await indexer.state(database.a)
        assert (state.purge_reason, state.published_publication) == (PurgeReason.DISABLED, 0)

    _run(
        database,
        scenario,
        settings=replace(_SETTINGS, batch_files=1),
        parser=lambda real: _ActOnParse(real, purge, call=2),
    )


# --- AC4, AC10: one test per exclusion class and per limit --------------------------------

_OK = b"def ok():\n    return 1\n"


@pytest.mark.integration
@pytest.mark.parametrize(
    ("path", "content", "excluded"),
    [
        ("bad\\name.py", _OK, ExclusionClass.UNSUPPORTED_PATH),
        ("config/.env.py", _OK, ExclusionClass.SECRET),
        ("vendor/lib.py", _OK, ExclusionClass.VENDORED),
        ("dist/app.js", b"export const x = 1;\n", ExclusionClass.GENERATED),
        ("notes.txt", b"notes\n", ExclusionClass.UNSUPPORTED_LANGUAGE),
        ("legacy/old.py", _OK, ExclusionClass.IGNORED),
        ("link.py", Link("ok.py"), ExclusionClass.SYMLINK),
        ("huge.py", b"#" * (_MIB + 1), ExclusionClass.OVER_READ_LIMIT),
        ("large.py", b"#" * (256 * _KIB + 1), ExclusionClass.OVER_PARSE_LIMIT),
        ("blob.py", b"\x00\x01\x02", ExclusionClass.BINARY),
        ("broken.py", b"\xff\xfe = 1\n", ExclusionClass.PARSE_FAILED),
    ],
    ids=[
        "unsupported-path",
        "secret",
        "vendored",
        "generated",
        "unsupported-language",
        "ignored",
        "symlink",
        "over-the-read-limit",
        "over-the-parse-limit",
        "binary",
        "parse-failed",
    ],
)
def test_each_exclusion_class_keeps_its_file_out_of_the_index(
    database: _Database, path: str, content: bytes | Link, excluded: ExclusionClass
) -> None:
    async def scenario(indexer: Indexer) -> None:
        await indexer.enable(database.a)
        tree: dict[str, bytes | Link] = {"ok.py": _OK, path: content}
        if excluded is ExclusionClass.IGNORED:
            tree[".review/rules.md"] = b"```review-ignore\nlegacy/**\n```\n"
        indexer.branch.push(tree)

        outcome = await indexer.build(database.a)

        assert outcome.status is BuildStatus.PUBLISHED
        assert outcome.exclusions[excluded] == 1
        snapshot = await indexer.snapshot(database.a)
        assert {item for item, _ in snapshot.files} == {"ok.py"}

    _run(database, scenario)


@pytest.mark.integration
@pytest.mark.parametrize(
    ("limits", "admission", "code", "requests"),
    [
        (replace(_LIMITS, download_bytes=10), _ADMISSION, "limit_exceeded:download_bytes", 4),
        (replace(_LIMITS, unpacked_bytes=10), _ADMISSION, "limit_exceeded:unpacked_bytes", 4),
        (replace(_LIMITS, archive_entries=2), _ADMISSION, "limit_exceeded:archive_entries", 4),
        (replace(_LIMITS, parsed_files=1), _ADMISSION, "limit_exceeded:parsed_files", 4),
        (replace(_LIMITS, symbols=1), _ADMISSION, "limit_exceeded:symbols", 4),
        (replace(_LIMITS, edges=1), _ADMISSION, "limit_exceeded:edges", 4),
        (replace(_LIMITS, deadline_seconds=0), _ADMISSION, "limit_exceeded:deadline", 0),
        (
            _LIMITS,
            replace(_ADMISSION, repository_storage_bytes=100),
            "limit_exceeded:repository_storage",
            4,
        ),
        (
            _LIMITS,
            replace(_ADMISSION, workspace_storage_bytes=100),
            "limit_exceeded:workspace_storage",
            4,
        ),
    ],
)
def test_each_limit_fails_the_index_with_its_code(
    database: _Database, limits: IndexLimits, admission: IndexAdmission, code: str, requests: int
) -> None:
    async def scenario(indexer: Indexer) -> None:
        await indexer.enable(database.a)
        indexer.branch.push({"lib.py": b"def target():\n    return 1\n", "user.py": _USER})

        outcome = await indexer.build(database.a)

        assert (outcome.status, outcome.error_code) == (BuildStatus.FAILED, code)
        state = await indexer.state(database.a)
        assert (state.status, state.error_code) == (IndexStatus.FAILED, code)
        assert state.stats.github_request_count == requests
        assert state.published_publication == 0

    _run(database, scenario, limits=limits, settings=replace(_SETTINGS, admission=admission))


_MODULES: Tree = {f"m{index}.py": f"x{index} = 1\n".encode() for index in range(3)}


@pytest.mark.integration
def test_two_live_builds_of_one_workspace_never_commit_over_its_quota(
    database: _Database,
) -> None:
    holder: list[Indexer] = []
    parsers: list[_ActOnParse] = []
    builds: list[BuildOutcome] = []

    async def build_a() -> None:
        builds.append(await holder[0].build(database.a))

    def acting(real: SourceParser) -> SourceParser:
        parsers.append(_ActOnParse(real, build_a, call=0))
        return parsers[0]

    async def scenario(indexer: Indexer) -> None:
        holder.append(indexer)
        # B's installation moves into A's Workspace: two installations, one Workspace.
        async with indexer.database.factory() as session:
            await session.execute(
                text(
                    "UPDATE provider_installations SET workspace_id = ("
                    " SELECT workspace_id FROM provider_installations WHERE id ="
                    " (SELECT provider_installation_id FROM repositories WHERE id = :a))"
                    " WHERE id = (SELECT provider_installation_id FROM repositories WHERE id = :b)"
                ),
                {"a": database.a, "b": database.b},
            )
            await session.commit()
        await indexer.enable(database.a)
        await indexer.enable(database.b)
        indexer.branch.push(_MODULES)
        # The bytes of one publication of the tree; then A starts over.
        assert (await indexer.build(database.a)).status is BuildStatus.PUBLISHED
        stored = (await indexer.state(database.a)).stored_bytes
        await indexer.purge(database.a)
        await indexer.enable(database.a)
        per_file = stored // len(_MODULES)
        assert per_file * len(_MODULES) == stored
        # Each repository fits its own quota; the Workspace holds A and two files of B.
        admission = replace(
            _ADMISSION,
            repository_storage_bytes=stored,
            workspace_storage_bytes=stored + 2 * per_file,
        )
        indexer.settings = replace(_SETTINGS, admission=admission, batch_files=1)
        # B claims its lease in an empty Workspace; at its first parse, before its first
        # write, A builds and publishes.
        parsers[0].calls, parsers[0].call = 0, 1

        outcome = await indexer.build(database.b)

        (published,) = builds
        assert published.status is BuildStatus.PUBLISHED
        assert (outcome.status, outcome.error_code) == (
            BuildStatus.FAILED,
            "limit_exceeded:workspace_storage",
        )
        state_a, state_b = await indexer.state(database.a), await indexer.state(database.b)
        assert state_a.stored_bytes == stored <= admission.repository_storage_bytes
        assert (state_b.status, state_b.stored_bytes, state_b.pending_bytes) == (
            IndexStatus.FAILED,
            0,
            0,
        )
        # B's first two batches committed, the Workspace at its quota and B within its
        # own; the third batch, over the Workspace quota, rolled back.
        assert await _raw_rows(database, database.b) == (2, 2, 0, 0)
        assert 2 * per_file <= admission.repository_storage_bytes

    _run(database, scenario, parser=acting)
