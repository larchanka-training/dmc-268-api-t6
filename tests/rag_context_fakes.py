"""Fakes and builders of the graph RAG context tests (test_rag_context*.py): blob stores, the
in-process parser, the graph, repositories and the selector driver."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

from app.modules.code_index.domain.enums import EdgeKind, IndexStatus
from app.modules.code_index.domain.rag_index import rag_index
from app.modules.reviews.application.ast_resolution import (
    FileUnavailable,
    RevisionTree,
    go_module_path,
)
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    ParseRequest,
    language_for_path,
)
from app.modules.reviews.application.diff_map import (
    DiffFile,
    DiffMap,
    VcsProviderName,
    build_diff_map,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.rag_context import (
    CachedBlobSource,
    RagCandidates,
    ReverseEdgeSelector,
)
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_REPOSITORY = UUID(int=1)


@dataclass
class BlobStore:
    """The shared cache rows, the PR revision blobs, and what touched them."""

    cached: dict[BlobCacheKey, tuple[BlobCacheStatus, str]] = field(default_factory=dict)
    blobs: dict[str, bytes] = field(default_factory=dict)
    failing_reads: dict[str, Exception] = field(default_factory=dict)
    fail_get: bool = False
    fail_put: bool = False
    gets: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    puts: list[tuple[BlobCacheKey, str, timedelta]] = field(default_factory=list)
    commits: int = 0
    open_units: int = 0
    # How many units of work were open at each provider read: the rule is zero.
    units_open_at_read: list[int] = field(default_factory=list)


@dataclass
class SharedBlobCache:
    store: BlobStore

    async def get(self, key: BlobCacheKey) -> BlobCacheEntry:
        self.store.gets.append(key.blob_sha)
        if self.store.fail_get:
            raise ConnectionError("cache database unavailable")
        if key not in self.store.cached:
            return BlobCacheEntry(status=BlobCacheStatus.MISS, content=None)
        status, content = self.store.cached[key]
        return BlobCacheEntry(
            status=status, content=content if status is BlobCacheStatus.HIT else None
        )

    async def put(self, key: BlobCacheKey, content: str, *, ttl: timedelta) -> None:
        if self.store.fail_put:
            raise ConnectionError("cache database unavailable")
        self.store.puts.append((key, content, ttl))


class BlobCacheUow:
    def __init__(self, store: BlobStore) -> None:
        self._store = store

    @property
    def blob_cache(self) -> SharedBlobCache:
        return SharedBlobCache(self._store)

    async def __aenter__(self) -> Self:
        self._store.open_units += 1
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._store.open_units -= 1
        return None

    async def commit(self) -> None:
        self._store.commits += 1

    async def rollback(self) -> None:
        return None


_HEAD = "a" * 40


_BASE = "c" * 40


_INDEX_COMMIT = "d" * 40


class Words:
    """One token per whitespace-separated word: easy to reason about in a test."""

    def count(self, text: str) -> int:
        return len(text.split())


@dataclass
class Repository:
    """The PR head of one repository and its base (``diff_old_sha``).

    The base holds the head files with the ``base`` changes: a text, or ``None`` for a
    file the PR adds. A blob is named by its path and text, so a file the PR does not
    touch has one blob on both sides. ``read`` lists the blobs read, a blob only the base
    has as ``path@base``; ``trees`` lists the revisions listed.
    """

    files: dict[str, str]
    failing: frozenset[str] = frozenset()
    read: list[str] = field(default_factory=list)
    base: dict[str, str | None] = field(default_factory=dict)
    base_unavailable: bool = False
    head_complete: bool = True
    trees: list[str] = field(default_factory=list)

    def sha(self, path: str) -> str:
        return _blob(path, self.files.get(path))

    def base_sha(self, path: str) -> str:
        return _blob(path, self._base_files()[path])

    def _base_files(self) -> dict[str, str]:
        merged = {**self.files, **self.base}
        return {path: text for path, text in merged.items() if text is not None}

    async def tree(self, revision: str) -> RevisionTree:
        self.trees.append(revision)
        if revision == _HEAD:
            head = {path: self.sha(path) for path in self.files}
            return RevisionTree(revision, head, self.head_complete)
        assert revision == _BASE
        if self.base_unavailable:
            raise ConnectionError("provider unavailable")
        base = {path: self.base_sha(path) for path in self._base_files()}
        return RevisionTree(revision, base, True)

    async def read_blob(self, blob_sha: str) -> bytes:
        head = next((item for item in self.files if self.sha(item) == blob_sha), None)
        if head is not None:
            self.read.append(head)
            if head in self.failing:
                raise ConnectionError("provider unavailable")
            return self.files[head].encode()
        base = self._base_files()
        path = next(item for item in base if self.base_sha(item) == blob_sha)
        self.read.append(f"{path}@base")
        if path in self.failing:
            raise ConnectionError("provider unavailable")
        return base[path].encode()


def _blob(path: str, text: str | None) -> str:
    return f"{abs(hash((path, text))) % 10**12:040d}"


@dataclass
class InProcessParser:
    parsed: list[str] = field(default_factory=list)

    async def parse_many(
        self, requests: tuple[ParseRequest, ...]
    ) -> dict[str, AstFacts | AstFailureReason]:
        self.parsed.extend(request.key for request in requests)
        return {
            request.key: extract_facts(request.language, request.source) for request in requests
        }

    async def aclose(self) -> None:
        return None


@dataclass
class Graph:
    """The default-branch graph: the hints of each repository, as a stale index may hold
    them; a hint is returned only for a target that was asked for."""

    hints: dict[UUID, tuple[GraphHint, ...]] = field(default_factory=dict)
    status: IndexStatus = IndexStatus.READY
    reads: list[tuple[UUID, tuple[GraphTarget, ...]]] = field(default_factory=list)

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        self.reads.append((repository_id, tuple(targets)))
        index = rag_index(
            rag_enabled=True,
            status=self.status,
            indexed_commit_sha=_INDEX_COMMIT,
            target_commit_sha=_INDEX_COMMIT,
            published_at=None,
            file_count=3,
            symbol_count=9,
            error_code=None,
            last_error_code=None,
        )
        if not index.usable:
            return GraphHints(index, ())
        asked = set(targets)
        return GraphHints(
            index,
            tuple(
                hint
                for hint in self.hints.get(repository_id, ())
                if GraphTarget(hint.target_path, hint.target_name) in asked
            ),
        )


def _uses(path: str, target_path: str, name: str) -> GraphHint:
    return GraphHint(path, EdgeKind.USES_IMPORTED, target_path, name)


def _vcs_file(
    repository: Repository,
    path: str,
    patch: str | None,
    *,
    additions: int = 1,
    deletions: int = 0,
    status: FileStatus = "modified",
    previous: str | None = None,
) -> VcsFile:
    return VcsFile(
        filename=path,
        status=status,
        blob_sha=None if status == "removed" else repository.sha(path),
        previous_filename=previous,
        additions=additions,
        deletions=deletions,
        changes=additions + deletions,
        patch=patch,
    )


def _diff_map(
    *files: VcsFile,
    repository_id: UUID = _REPOSITORY,
    listed: int | None = None,
    diff_old_sha: str | None = _BASE,
    provider: VcsProviderName = VcsProviderName.GITHUB,
) -> DiffMap:
    pull_request = VcsPullRequest(
        locator=PullRequestLocator(17, "octo/repo", 7),
        head_sha=_HEAD,
        base_sha="b" * 40,
        meta=PullRequestMeta(
            title="t",
            description=None,
            author="octo",
            source_branch="f",
            target_branch="main",
            labels=(),
            files_changed=len(files) if listed is None else listed,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    return build_diff_map(
        pull_request,
        files,
        provider=provider,
        repository_id=repository_id,
        diff_old_sha=diff_old_sha,
    )


def _select(
    repository: Repository,
    diff_map: DiffMap,
    graph: Graph,
    *,
    shown: frozenset[str] = frozenset(),
    denied: Callable[[str], bool] = lambda path: False,
    store: BlobStore | None = None,
    reader: CachedBlobSource | None = None,
) -> RagCandidates:
    """Select on what L4 leaves behind: the head tree and the texts and facts of the PR
    files it loaded, which are no RAG read; ``reader`` is the run's blob reader."""
    blobs = store or BlobStore()

    async def run() -> RagCandidates:
        tree = await repository.tree(diff_map.head_sha)
        changed: list[tuple[DiffFile, AstFacts]] = []
        texts: dict[str, str | FileUnavailable] = {}
        facts: dict[str, AstFacts | FileUnavailable] = {}
        for file in diff_map.files:
            language = None if file.new_path is None else language_for_path(file.new_path)
            if file.new_path is None or language is None:
                continue
            source = repository.files[file.new_path]
            parsed = extract_facts(language, source.encode())
            texts[file.new_path] = source
            facts[file.new_path] = parsed
            changed.append((file, parsed))
        go_mod = repository.files.get("go.mod")
        selector = ReverseEdgeSelector(
            retriever=graph,
            blobs=reader
            or CachedBlobSource(
                repository_id=diff_map.repository_id,
                source=repository,
                cache=SharedBlobCache(blobs),
                uow_factory=lambda: BlobCacheUow(blobs),
            ),
            parser=InProcessParser(),
            cache=None,
            counter=Words(),
            trees=repository,
        )
        return await selector.select(
            diff_map=diff_map,
            tree=tree,
            go_module=None if go_mod is None else go_module_path(go_mod),
            changed=changed,
            loaded_texts=texts,
            loaded_facts=facts,
            shown=shown,
            denied=denied,
        )

    return asyncio.run(run())


_PY_STORE = "def save(item):\n    item.check()\n    return item\n"


_PY_STORE_PATCH = "@@ -1,2 +1,3 @@\n def save(item):\n+    item.check()\n     return item"


_PY_USER = "from .store import save\n\n\ndef run(item):\n    return save(item)\n"


def _store_hints(*paths: str) -> Graph:
    return Graph({_REPOSITORY: tuple(_uses(path, "pkg/store.py", "save") for path in paths)})


_PY_VIEW = "from .store import load\n\n\ndef show(key):\n    return load(key)\n"


def _load_hints(*paths: str) -> Graph:
    return Graph({_REPOSITORY: tuple(_uses(path, "pkg/store.py", "load") for path in paths)})
