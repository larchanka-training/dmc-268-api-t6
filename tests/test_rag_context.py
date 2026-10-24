"""Graph RAG context: candidate blobs read through the blob cache (#144, M10), the
reverse-edge candidates selected from the graph and confirmed at the PR head (ADR 0001 §8.2,
§9), and their slot in the review context payload."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import timedelta
from types import TracebackType
from typing import Self
from uuid import UUID

import pytest

from app.modules.code_index.domain.enums import EdgeKind, IndexStatus, RagIndexStatus
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
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import (
    DiffFile,
    DiffMap,
    DiffSide,
    VcsProviderName,
    build_diff_map,
)
from app.modules.reviews.application.get_run_file_lines import (
    BlobCacheEntry,
    BlobCacheKey,
    BlobCacheStatus,
)
from app.modules.reviews.application.graph_retriever import GraphHint, GraphHints, GraphTarget
from app.modules.reviews.application.policy_documents import missing_document
from app.modules.reviews.application.policy_errors import PolicyError, PolicyErrorCode
from app.modules.reviews.application.policy_snapshot import (
    AGENTS_DOCUMENT_PATH,
    RULES_DOCUMENT_PATH,
    ExecutionProfile,
    PromptBindings,
)
from app.modules.reviews.application.prepare_policy_snapshot import (
    RunPolicyPins,
    build_policy_snapshot,
)
from app.modules.reviews.application.prompt_builder import FileStatus, PullRequestMeta
from app.modules.reviews.application.rag_context import (
    CachedBlobSource,
    RagCandidates,
    RagOmission,
    RetrievedPlace,
    ReverseEdgeSelector,
)
from app.modules.reviews.application.review_context import (
    BuildReviewContext,
    ContextLevel,
    ContextOmission,
    RagPlan,
    RagStats,
    Representation,
    ReviewContextPayload,
)
from app.modules.reviews.application.review_context_stage import context_summary
from app.modules.reviews.application.review_ignore import (
    IgnoreMatcher,
    apply_ignore,
    parse_review_ignore,
)
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_REPOSITORY = UUID(int=1)
_OTHER_REPOSITORY = UUID(int=2)


def _sha(number: int) -> str:
    return f"{number:040x}"


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
class PrRevisionBlobs:
    store: BlobStore

    async def read_blob(self, blob_sha: str) -> bytes:
        self.store.units_open_at_read.append(self.store.open_units)
        self.store.reads.append(blob_sha)
        if blob_sha in self.store.failing_reads:
            raise self.store.failing_reads[blob_sha]
        return self.store.blobs[blob_sha]


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


def _source(store: BlobStore, repository_id: UUID = _REPOSITORY) -> CachedBlobSource:
    return CachedBlobSource(
        repository_id=repository_id,
        source=PrRevisionBlobs(store),
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )


def test_a_hit_does_not_read_the_blob() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, "def save():\n")}
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.gets == [_sha(1)]
    assert store.reads == []
    assert store.puts == []


def test_a_miss_reads_once_and_puts_in_a_unit_of_work_of_its_own() -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"})
    source = _source(store)

    async def scenario() -> tuple[str | FileUnavailable, str | FileUnavailable]:
        return await source.text(_sha(1)), await source.text(_sha(1))

    first, again = asyncio.run(scenario())

    assert first == again == "def save():\n"
    assert store.gets == [_sha(1)]
    assert store.reads == [_sha(1)]
    # No transaction is open while the provider is read.
    assert store.units_open_at_read == [0]
    assert store.puts == [(BlobCacheKey(_REPOSITORY, _sha(1)), "def save():\n", timedelta(days=7))]
    assert store.commits == 1
    assert store.open_units == 0


def test_an_expired_entry_is_read_again_and_refreshed() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.EXPIRED, "old\n")},
        blobs={_sha(1): b"def save():\n"},
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.reads == [_sha(1)]
    assert [key for key, _, _ in store.puts] == [BlobCacheKey(_REPOSITORY, _sha(1))]


def test_the_cache_is_keyed_by_the_run_repository() -> None:
    store = BlobStore(
        cached={BlobCacheKey(_OTHER_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, "other\n")},
        blobs={_sha(1): b"mine\n"},
    )

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "mine\n"
    assert store.reads == [_sha(1)]
    assert [key for key, _, _ in store.puts] == [BlobCacheKey(_REPOSITORY, _sha(1))]


def test_hits_and_misses_both_count_and_the_21st_distinct_blob_is_rag_limit() -> None:
    hits = {
        BlobCacheKey(_REPOSITORY, _sha(number)): (BlobCacheStatus.HIT, f"hit {number}\n")
        for number in range(1, 11)
    }
    misses = {_sha(number): f"miss {number}\n".encode() for number in range(11, 22)}
    store = BlobStore(cached=hits, blobs=misses)
    source = _source(store)

    async def scenario() -> list[str | FileUnavailable]:
        texts = [await source.text(_sha(number)) for number in range(1, 22)]
        # A blob already read is not a new read.
        texts.append(await source.text(_sha(1)))
        return texts

    texts = asyncio.run(scenario())

    assert texts[:10] == [f"hit {number}\n" for number in range(1, 11)]
    assert texts[10:20] == [f"miss {number}\n" for number in range(11, 21)]
    assert texts[20] == FileUnavailable("rag_limit")
    assert texts[21] == "hit 1\n"
    # The blob over the limit is neither looked up nor read.
    assert len(store.gets) == 20
    assert _sha(21) not in store.gets
    assert store.reads == [_sha(number) for number in range(11, 21)]


def test_a_failed_read_counts_toward_the_limit() -> None:
    store = BlobStore(
        blobs={_sha(number): b"x = 1\n" for number in range(2, 22)},
        failing_reads={_sha(1): RuntimeError("GitHub 502")},
    )
    source = _source(store)

    async def scenario() -> list[str | FileUnavailable]:
        return [await source.text(_sha(number)) for number in range(1, 22)]

    texts = asyncio.run(scenario())

    assert texts[0] == FileUnavailable("source_read_failed")
    assert texts[1:20] == ["x = 1\n"] * 19
    assert texts[20] == FileUnavailable("rag_limit")
    assert [key.blob_sha for key, _, _ in store.puts] == [_sha(number) for number in range(2, 21)]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        (b"x" * 262_145, "source_too_large"),
        (b"x = 1\n\x00\n", "binary"),
        (b"%PDF-1.7\n", "binary"),
        (b"x = '\xff'\n", "source_decode_failed"),
    ],
    ids=["too_large", "nul", "magic_number", "not_utf8"],
)
def test_a_read_blob_gets_the_size_binary_and_utf8_checks(raw: bytes, reason: str) -> None:
    store = BlobStore(blobs={_sha(1): raw})

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == FileUnavailable(reason)
    assert store.puts == []


@pytest.mark.parametrize(
    ("content", "reason"),
    [("x" * 262_145, "source_too_large"), ("%PDF-1.7\n", "binary")],
    ids=["too_large", "magic_number"],
)
def test_a_cached_text_gets_the_same_checks_as_a_read(content: str, reason: str) -> None:
    # Other writers of the cache accept larger files and no magic numbers; a hit must
    # not show what a miss would withhold.
    store = BlobStore(cached={BlobCacheKey(_REPOSITORY, _sha(1)): (BlobCacheStatus.HIT, content)})

    text = asyncio.run(_source(store).text(_sha(1)))

    assert text == FileUnavailable(reason)
    assert store.reads == []


def test_a_failing_put_is_logged_and_the_text_is_returned(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"}, fail_put=True)

    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.rag_context"):
        text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.commits == 0
    assert store.open_units == 0
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG blob cache put failed blob_sha={_sha(1)}"
    ]


def test_a_failing_cache_read_is_logged_and_the_blob_is_read(
    caplog: pytest.LogCaptureFixture,
) -> None:
    store = BlobStore(blobs={_sha(1): b"def save():\n"}, fail_get=True)

    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.rag_context"):
        text = asyncio.run(_source(store).text(_sha(1)))

    assert text == "def save():\n"
    assert store.reads == [_sha(1)]
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG blob cache get failed blob_sha={_sha(1)}"
    ]


@pytest.mark.parametrize(
    "error", [RunCancelled(), RunFailure("deadline_exceeded", "the run deadline passed")]
)
def test_cancellation_and_the_deadline_are_not_a_read_failure(error: Exception) -> None:
    store = BlobStore(failing_reads={_sha(1): error})

    with pytest.raises(type(error)):
        asyncio.run(_source(store).text(_sha(1)))


# --- Reverse-edge candidates: selected from the graph, confirmed at the PR head ---------

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
        provider=VcsProviderName.GITHUB,
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


@dataclass(frozen=True)
class Language:
    """A PR that changes the exported ``save`` of one file, in one language, and the
    importer sources of the F6 cases."""

    changed: str
    source: str
    patch: str
    name: str
    candidate: str
    others: dict[str, str]
    importers: dict[str, str]


_PYTHON = Language(
    changed="pkg/store.py",
    source="def save(item):\n    item.check()\n    return item\n",
    patch="@@ -1,2 +1,3 @@\n def save(item):\n+    item.check()\n     return item",
    name="save",
    candidate="pkg/app.py",
    others={"pkg/other.py": "def save(item):\n    return item\n"},
    importers={
        "used": "from .store import save\n\n\ndef run(item):\n    return save(item)\n",
        "other_module": "from .other import save\n\n\ndef run(item):\n    return save(item)\n",
        "parameter": "from .store import save\n\n\ndef run(save):\n    return save(1)\n",
        "rebinding": (
            "from .store import save\n\n\ndef run(item):\n    save = print\n    return save(item)\n"
        ),
        "receiver": (
            "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
        ),
    },
)
_TYPESCRIPT = Language(
    changed="src/store.ts",
    source="export function save(item: number): number {\n  check(item);\n  return item;\n}\n",
    patch=(
        "@@ -1,3 +1,4 @@\n export function save(item: number): number {\n"
        "+  check(item);\n   return item;\n }"
    ),
    name="save",
    candidate="src/app.ts",
    others={"src/other.ts": "export function save(item: number): number {\n  return item;\n}\n"},
    importers={
        "used": (
            'import { save } from "./store";\n\n'
            "export function run(item: number): number {\n  return save(item);\n}\n"
        ),
        "other_module": (
            'import { save } from "./other";\n\n'
            "export function run(item: number): number {\n  return save(item);\n}\n"
        ),
        "parameter": (
            'import { save } from "./store";\n\n'
            "export function run(save: Saver): number {\n  return save(1);\n}\n"
        ),
        "rebinding": (
            'import { save } from "./store";\n\n'
            "export function run(item: number): number {\n"
            "  const save = check;\n  return save(item);\n}\n"
        ),
        "receiver": (
            'import { save } from "./store";\n\n'
            "export function run(repository: Repository): number {\n"
            "  return repository.save();\n}\n"
        ),
    },
)
_GO = Language(
    changed="pkg/store/store.go",
    source="package store\n\nfunc Save(item int) int {\n\tcheck(item)\n\treturn item\n}\n",
    patch="@@ -3,3 +3,4 @@\n func Save(item int) int {\n+\tcheck(item)\n \treturn item\n }",
    name="Save",
    candidate="cmd/app/app.go",
    others={
        "go.mod": "module example.com/m\n\ngo 1.22\n",
        "pkg/other/other.go": "package other\n\nfunc Save(item int) int {\n\treturn item\n}\n",
    },
    importers={
        "used": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(item int) int {\n\treturn store.Save(item)\n}\n"
        ),
        "other_module": (
            'package app\n\nimport "example.com/m/pkg/other"\n\n'
            "func Run(item int) int {\n\treturn other.Save(item)\n}\n"
        ),
        "parameter": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(store Saver) int {\n\treturn store.Save(1)\n}\n"
        ),
        "rebinding": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(item int) int {\n\tstore := open()\n\treturn store.Save(item)\n}\n"
        ),
        "receiver": (
            'package app\n\nimport "example.com/m/pkg/store"\n\n'
            "func Run(repository Saver) int {\n\treturn repository.Save()\n}\n"
        ),
    },
)
_LANGUAGES = {"python": _PYTHON, "typescript": _TYPESCRIPT, "go": _GO}


def _language_case(
    language: Language, importer: str, repository_id: UUID = _REPOSITORY
) -> tuple[Repository, DiffMap, Graph]:
    repository = Repository(
        {
            language.changed: language.source,
            language.candidate: language.importers[importer],
            **language.others,
        }
    )
    diff_map = _diff_map(
        _vcs_file(repository, language.changed, language.patch), repository_id=repository_id
    )
    graph = Graph({repository_id: (_uses(language.candidate, language.changed, language.name),)})
    return repository, diff_map, graph


@pytest.mark.parametrize(
    ("language", "ranges", "text"),
    [
        (
            "python",
            (LineRange(1, 1), LineRange(4, 5)),
            "from .store import save\ndef run(item):\n    return save(item)",
        ),
        (
            "typescript",
            (LineRange(1, 1), LineRange(3, 5)),
            'import { save } from "./store";\n'
            "export function run(item: number): number {\n  return save(item);\n}",
        ),
        (
            "go",
            (LineRange(3, 3), LineRange(5, 7)),
            'import "example.com/m/pkg/store"\n'
            "func Run(item int) int {\n\treturn store.Save(item)\n}",
        ),
    ],
)
def test_a_reverse_edge_confirmed_at_head_is_used(
    language: str, ranges: tuple[LineRange, ...], text: str
) -> None:
    case = _LANGUAGES[language]
    repository, diff_map, graph = _language_case(case, "used")

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget(case.changed, case.name),))]
    assert result.places == (
        RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=_REPOSITORY,
                revision=_HEAD,
                path=case.candidate,
                blob_sha=repository.sha(case.candidate),
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=ranges,
                reason="rag_reverse_edge",
            ),
            edge_kind=EdgeKind.USES_IMPORTED,
            index_commit_sha=_INDEX_COMMIT,
            signature=False,
            text=text,
            tokens=len(text.split()),
        ),
    )
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})
    assert result.omissions == ()
    # The changed file was loaded by L4: only the candidate is a RAG read.
    assert repository.read == [case.candidate]
    # The PR only adds lines, so it removes no export: the base is never listed.
    assert repository.trees == [_HEAD]


@pytest.mark.parametrize(
    ("language", "importer"),
    [
        (language, importer)
        for language in ("python", "typescript", "go")
        for importer in ("other_module", "parameter", "rebinding", "receiver")
    ],
)
def test_f6_a_name_the_head_does_not_bind_to_the_change_is_no_candidate(
    language: str, importer: str
) -> None:
    # The graph still names the edge; at head the name comes from another module, is
    # shadowed by a parameter, rebound locally, or read only through a receiver.
    case = _LANGUAGES[language]
    repository, diff_map, graph = _language_case(case, importer)

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", case.candidate),)
    assert (result.found, result.confirmed) == (1, 0)
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 1}


_PY_STORE = "def save(item):\n    item.check()\n    return item\n"
_PY_STORE_PATCH = "@@ -1,2 +1,3 @@\n def save(item):\n+    item.check()\n     return item"
_PY_USER = "from .store import save\n\n\ndef run(item):\n    return save(item)\n"


def _python_repository(**importers: str) -> tuple[Repository, DiffMap]:
    repository = Repository({"pkg/store.py": _PY_STORE, **importers})
    return repository, _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))


def _store_hints(*paths: str) -> Graph:
    return Graph({_REPOSITORY: tuple(_uses(path, "pkg/store.py", "save") for path in paths)})


def test_a_pr_file_and_a_definition_l4_shows_are_not_repeated() -> None:
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/api.py": _PY_USER, "pkg/app.py": _PY_USER}
    )
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH),
        _vcs_file(
            repository,
            "pkg/api.py",
            "@@ -1,1 +1,1 @@\n-from .store import x\n+from .store import save",
            deletions=1,
        ),
    )

    result = _select(
        repository,
        diff_map,
        _store_hints("pkg/api.py", "pkg/app.py"),
        # L4 already shows `run` of pkg/app.py: the changed code imports it.
        shown=frozenset({"aaaaaaaaaaaa:pkg/app.py#0"}),
    )

    assert result.places == ()
    assert result.omissions == ()
    # The PR file is no candidate at all; the importer whose place L4 shows is confirmed.
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})


def test_an_ignored_candidate_is_never_read_and_only_counted() -> None:
    repository, diff_map = _python_repository(
        **{"generated/app.py": _PY_USER, "pkg/app.py": _PY_USER}
    )

    result = _select(
        repository,
        diff_map,
        _store_hints("generated/app.py", "pkg/app.py"),
        denied=lambda path: path.startswith("generated/"),
    )

    assert repository.read == ["pkg/app.py"]
    assert [place.source.path for place in result.places] == ["pkg/app.py"]
    assert result.omissions == ()
    assert (result.found, result.confirmed, dict(result.dropped)) == (2, 1, {"excluded": 1})


def test_a_candidate_absent_from_the_head_tree_is_missing() -> None:
    repository, diff_map = _python_repository()

    result = _select(repository, diff_map, _store_hints("pkg/gone.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_candidate_missing", "pkg/gone.py"),)
    assert dict(result.dropped) == {"rag_candidate_missing": 1}
    assert repository.read == []


def test_a_candidate_whose_blob_read_fails_is_source_read_failed() -> None:
    repository, diff_map = _python_repository(**{"pkg/app.py": _PY_USER})
    repository.failing = frozenset({"pkg/app.py"})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("source_read_failed", "pkg/app.py"),)
    assert dict(result.dropped) == {"source_read_failed": 1}


def test_the_eleventh_file_is_over_the_limit_and_never_read() -> None:
    paths = [f"pkg/user_{number:02d}.py" for number in range(11)]
    repository, diff_map = _python_repository(**{path: _PY_USER for path in paths})

    result = _select(repository, diff_map, _store_hints(*paths))

    assert [place.source.path for place in result.places] == paths[:10]
    assert result.omissions == (RagOmission("rag_limit", "pkg/user_10.py"),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (11, 10, {"rag_limit": 1})
    assert "pkg/user_10.py" not in repository.read


def test_the_fourth_place_of_a_file_is_over_the_limit() -> None:
    user = "from .store import save\n" + "".join(
        f"\n\ndef run_{number}(item):\n    return save(item)\n" for number in range(4)
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [
        (LineRange(1, 1), LineRange(4, 5)),
        (LineRange(1, 1), LineRange(8, 9)),
        (LineRange(1, 1), LineRange(12, 13)),
    ]
    assert result.omissions == (RagOmission("rag_limit", "pkg/app.py"),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})


def test_f5_the_graph_points_at_new_utils_but_head_imports_old_utils() -> None:
    repository = Repository(
        {
            "pkg/new_utils.py": "def helper(value):\n    value.check()\n    return value\n",
            "pkg/old_utils.py": "def helper(value):\n    return value\n",
            "pkg/app.py": (
                "from .old_utils import helper\n\n\ndef run(item):\n    return helper(item)\n"
            ),
        }
    )
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/new_utils.py",
            "@@ -1,2 +1,3 @@\n def helper(value):\n+    value.check()\n     return value",
        )
    )
    graph = Graph({_REPOSITORY: (_uses("pkg/app.py", "pkg/new_utils.py", "helper"),)})

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/app.py"),)


def test_f5_the_ranges_are_the_head_lines_of_a_moved_declaration() -> None:
    # On the default branch `run` came first; at head another function precedes it.
    user = (
        "from .store import save\n\n\n"
        "def first(item):\n    return item\n\n\n"
        "def run(item):\n    return save(item)\n"
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(8, 9))]
    assert result.places[0].text == "from .store import save\ndef run(item):\n    return save(item)"


def test_a_declaration_too_large_to_show_whole_gives_its_signature() -> None:
    body = "".join(f"    step_{number} = save(item)\n" for number in range(200))
    user = f"from .store import save\n\n\ndef run(item):\n{body}    return item\n"
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [(place.source.ranges, place.signature) for place in result.places] == [
        ((LineRange(1, 1), LineRange(4, 4)), True)
    ]
    assert result.places[0].text == "from .store import save\ndef run(item):"


def test_importers_of_a_changed_symbol_come_before_importers_of_a_changed_module() -> None:
    store = (
        "import os\n\n\ndef save(item):\n    item.check()\n    return item\n\n\n"
        "def load(key):\n    return key\n"
    )
    patch = (
        "@@ -1,3 +1,7 @@\n+import os\n+\n+\n def save(item):\n+    item.check()\n"
        "     return item\n "
    )
    repository = Repository(
        {
            "pkg/store.py": store,
            "pkg/app.py": _PY_USER,
            "pkg/view.py": "from .store import load\n\n\ndef show(key):\n    return load(key)\n",
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", patch, additions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                GraphHint("pkg/app.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                _uses("pkg/app.py", "pkg/store.py", "save"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert graph.reads == [
        (_REPOSITORY, (GraphTarget("pkg/store.py", "save"), GraphTarget("pkg/store.py", "")))
    ]
    assert [(place.source.path, place.edge_kind) for place in result.places] == [
        ("pkg/app.py", EdgeKind.USES_IMPORTED),
        ("pkg/view.py", EdgeKind.IMPORTS),
    ]
    assert [place.source.ranges for place in result.places] == [
        (LineRange(1, 1), LineRange(4, 5)),
        (LineRange(1, 1), LineRange(4, 5)),
    ]
    assert (result.found, result.confirmed) == (2, 2)


def test_a_python_submodule_binding_is_confirmed() -> None:
    repository = Repository(
        {
            "pkg/__init__.py": "",
            "pkg/store.py": _PY_STORE,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))
    graph = Graph({_REPOSITORY: (_uses("app/main.py", "pkg/store.py", "save"),)})

    result = _select(repository, diff_map, graph)

    assert [(place.source.path, place.source.ranges) for place in result.places] == [
        ("app/main.py", (LineRange(1, 1), LineRange(4, 5)))
    ]


def test_the_graph_is_asked_under_the_default_branch_path_of_a_renamed_file() -> None:
    repository = Repository({"pkg/store.py": _PY_STORE})
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/store.py",
            _PY_STORE_PATCH,
            status="renamed",
            previous="pkg/old_store.py",
        )
    )
    graph = Graph()

    _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/old_store.py", "save"),))]


def test_targets_past_two_hundred_are_not_asked_and_count_as_rag_limit() -> None:
    source = "".join(f"def f{number}():\n    return {number}\n" for number in range(201))
    lines = source.rstrip("\n").split("\n")
    patch = f"@@ -0,0 +1,{len(lines)} @@\n" + "\n".join(f"+{line}" for line in lines)
    repository = Repository({"pkg/many.py": source})
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/many.py", patch, additions=len(lines), status="added")
    )
    graph = Graph()

    result = _select(repository, diff_map, graph)

    [(_, targets)] = graph.reads
    assert len(targets) == 200
    assert (targets[0], targets[-1]) == (
        GraphTarget("pkg/many.py", "f0"),
        GraphTarget("pkg/many.py", "f199"),
    )
    assert result.omissions == (RagOmission("rag_limit", None, 1),)
    assert (result.found, dict(result.dropped)) == (1, {"rag_limit": 1})


def test_an_index_not_usable_in_the_graph_read_selects_and_reads_nothing() -> None:
    repository, diff_map = _python_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")
    graph.status = IndexStatus.EMPTY

    result = _select(repository, diff_map, graph)

    assert result.index.status is RagIndexStatus.EMPTY
    assert (result.places, result.omissions, result.found) == ((), (), 0)
    assert repository.read == []


def test_two_repositories_with_the_same_paths_stay_apart() -> None:
    first, second = UUID(int=11), UUID(int=12)
    graph = Graph(
        {
            first: (_uses("pkg/app.py", "pkg/store.py", "save"),),
            second: (_uses("pkg/tool.py", "pkg/store.py", "save"),),
        }
    )
    results = []
    stores = []
    for repository_id in (first, second):
        repository = Repository(
            {"pkg/store.py": _PY_STORE, "pkg/app.py": _PY_USER, "pkg/tool.py": _PY_USER}
        )
        diff_map = _diff_map(
            _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH), repository_id=repository_id
        )
        store = BlobStore()
        stores.append(store)
        results.append(_select(repository, diff_map, graph, store=store))

    assert [reads[0] for reads in graph.reads] == [first, second]
    assert [
        [(place.source.repository_id, place.source.path) for place in result.places]
        for result in results
    ] == [[(first, "pkg/app.py")], [(second, "pkg/tool.py")]]
    assert [[key.repository_id for key, _, _ in store.puts] for store in stores] == [
        [first],
        [second],
    ]


def test_a_place_is_the_innermost_declaration_that_reads_the_name() -> None:
    # A class reads what its methods read: the method is the place, not the class.
    user = (
        "from .store import save\n\n\n"
        "class Service:\n"
        "    def run(self, item):\n        return save(item)\n\n"
        "    def other(self):\n        return 0\n"
    )
    repository, diff_map = _python_repository(**{"pkg/app.py": user})

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(5, 6))]


def test_an_importer_of_a_changed_module_that_rebinds_the_name_is_unconfirmed() -> None:
    store = "import os\n\n\ndef load(key):\n    return key\n"
    repository = Repository(
        {
            "pkg/store.py": store,
            "pkg/view.py": "from .store import load\n\n\ndef show(load):\n    return load(1)\n",
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", "@@ -1,1 +1,2 @@\n+import os\n \n"))
    graph = Graph({_REPOSITORY: (GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),)})

    result = _select(repository, diff_map, graph)

    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", ""),))]
    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)


def test_a_module_the_edge_is_not_about_is_never_read() -> None:
    user = (
        "from .store import save\nfrom .text import shout\n\n\n"
        "def run(item):\n    return shout(save(item))\n"
    )
    repository, diff_map = _python_repository(
        **{"pkg/app.py": user, "pkg/text.py": "def shout(value):\n    return value\n"}
    )

    result = _select(repository, diff_map, _store_hints("pkg/app.py"))

    assert [place.source.ranges for place in result.places] == [(LineRange(1, 1), LineRange(5, 6))]
    assert repository.read == ["pkg/app.py"]


def test_a_resolution_stopped_by_the_read_limit_is_rag_limit() -> None:
    # 19 unconfirmed candidates spend 19 reads; the 20th read is the candidate itself,
    # and its import needs `pkg/__init__.py`, which would be the 21st.
    receivers = [f"pkg/user_{number:02d}.py" for number in range(19)]
    receiver = "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
    repository = Repository(
        {
            "pkg/__init__.py": "",
            "pkg/store.py": _PY_STORE,
            "app/main.py": (
                "from pkg import store\n\n\ndef run(item):\n    return store.save(item)\n"
            ),
            **{path: receiver for path in receivers},
        }
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH))
    graph = Graph(
        {
            _REPOSITORY: tuple(
                _uses(path, "pkg/store.py", "save") for path in [*receivers, "app/main.py"]
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert result.places == ()
    assert result.omissions[-1] == RagOmission("rag_limit", "app/main.py")
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 19, "rag_limit": 1}
    assert len(repository.read) == 20
    assert "pkg/__init__.py" not in repository.read


def test_a_candidate_past_the_read_limit_is_rag_limit_with_its_path() -> None:
    # 20 unconfirmed candidates take no file slot but spend the 20 reads of the run.
    paths = [f"pkg/user_{number:02d}.py" for number in range(21)]
    receiver = "from .store import save\n\n\ndef run(repository):\n    return repository.save()\n"
    repository, diff_map = _python_repository(**{path: receiver for path in paths})

    result = _select(repository, diff_map, _store_hints(*paths))

    assert result.places == ()
    assert result.omissions[-1] == RagOmission("rag_limit", "pkg/user_20.py")
    assert dict(result.dropped) == {"rag_edge_unconfirmed": 20, "rag_limit": 1}
    assert "pkg/user_20.py" not in repository.read


# Broken references (ADR 0001 §8.3): an export the PR removes, checked at both revisions.

_PY_BASE_STORE = "def save(item):\n    return item\n\n\ndef load(key):\n    return key\n"
_PY_VIEW = "from .store import load\n\n\ndef show(key):\n    return load(key)\n"
# The PR changes `save` and removes `load` (the base above, `_PY_STORE` at head).
_PY_REMOVAL_PATCH = (
    "@@ -1,6 +1,3 @@\n def save(item):\n+    item.check()\n     return item\n"
    "-\n-\n-def load(key):\n-    return key"
)


def _deletion(repository: Repository, path: str = "pkg/store.py") -> VcsFile:
    text = repository.base[path]
    assert text is not None
    removed = text.rstrip("\n").split("\n")
    patch = f"@@ -1,{len(removed)} +0,0 @@\n" + "\n".join(f"-{line}" for line in removed)
    return _vcs_file(repository, path, patch, additions=0, deletions=len(removed), status="removed")


def _load_hints(*paths: str) -> Graph:
    return Graph({_REPOSITORY: tuple(_uses(path, "pkg/store.py", "load") for path in paths)})


def _removal(case: str) -> tuple[Repository, DiffMap]:
    """A PR that takes the export ``load`` away from ``pkg/store.py``; ``pkg/view.py``
    imports it, and the PR leaves that file alone."""
    if case == "renamed_symbol":
        head = "def save(item):\n    return item\n\n\ndef fetch(key):\n    return key\n"
        repository = Repository(
            {"pkg/store.py": head, "pkg/view.py": _PY_VIEW}, base={"pkg/store.py": _PY_BASE_STORE}
        )
        patch = "@@ -5,2 +5,2 @@\n-def load(key):\n+def fetch(key):\n     return key"
        return repository, _diff_map(_vcs_file(repository, "pkg/store.py", patch, deletions=1))
    if case == "deleted_file":
        repository = Repository({"pkg/view.py": _PY_VIEW}, base={"pkg/store.py": _PY_BASE_STORE})
        return repository, _diff_map(_deletion(repository))
    assert case == "moved_file"
    repository = Repository(
        {"pkg/storage.py": _PY_BASE_STORE, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/storage.py": None},
    )
    moved = _vcs_file(
        repository, "pkg/storage.py", None, additions=0, status="renamed", previous="pkg/store.py"
    )
    return repository, _diff_map(moved)


def _view_place(repository: Repository) -> RetrievedPlace:
    """The broken reference of ``pkg/view.py``: the import and its use at head, with the
    definition of ``load`` at the base."""
    return RetrievedPlace(
        source=SourceRef(
            role=SourceRole.RETRIEVED_EXCERPT,
            repository_id=_REPOSITORY,
            revision=_HEAD,
            path="pkg/view.py",
            blob_sha=repository.sha("pkg/view.py"),
            origin_paths=(),
            side=DiffSide.RIGHT,
            ranges=(LineRange(1, 1), LineRange(4, 5)),
            reason="rag_broken_reference",
        ),
        edge_kind=EdgeKind.USES_IMPORTED,
        index_commit_sha=_INDEX_COMMIT,
        signature=False,
        text="from .store import load\ndef show(key):\n    return load(key)",
        tokens=8,
        definitions=(
            RetrievedPlace(
                source=SourceRef(
                    role=SourceRole.RETRIEVED_EXCERPT,
                    repository_id=_REPOSITORY,
                    revision=_BASE,
                    path="pkg/store.py",
                    blob_sha=repository.base_sha("pkg/store.py"),
                    origin_paths=(),
                    side=DiffSide.LEFT,
                    ranges=(LineRange(5, 5),),
                    reason="rag_broken_reference",
                ),
                edge_kind=EdgeKind.USES_IMPORTED,
                index_commit_sha=_INDEX_COMMIT,
                signature=True,
                text="def load(key):",
                tokens=2,
            ),
        ),
    )


@pytest.mark.parametrize(
    ("case", "targets"),
    [
        ("renamed_symbol", (("pkg/store.py", "load"), ("pkg/store.py", "fetch"))),
        ("deleted_file", (("pkg/store.py", "save"), ("pkg/store.py", "load"))),
        ("moved_file", (("pkg/store.py", "save"), ("pkg/store.py", "load"))),
    ],
)
def test_an_importer_of_a_removed_export_is_a_broken_reference(
    case: str, targets: tuple[tuple[str, str], ...]
) -> None:
    repository, diff_map = _removal(case)
    graph = _load_hints("pkg/view.py")

    result = _select(repository, diff_map, graph)

    # The removed exports come first, under the default-branch path of the module.
    assert graph.reads == [(_REPOSITORY, tuple(GraphTarget(path, name) for path, name in targets))]
    # Two blocks, one revision each: the use at head and the definition at the base.
    assert result.places == (_view_place(repository),)
    assert (result.found, result.confirmed, dict(result.dropped)) == (1, 1, {})
    assert result.omissions == ()
    # The base extraction of the module is a RAG read; the importer has one blob on both
    # sides and is read once.
    assert repository.read == ["pkg/store.py@base", "pkg/view.py"]
    assert repository.trees == [_HEAD, _BASE]


@pytest.mark.parametrize(
    ("base_store", "view", "found"),
    [
        # At the base `load` came from a module that did not exist; a stale graph still
        # names the edge to the file the PR deletes.
        (_PY_BASE_STORE, "from .legacy import load\n\n\ndef show(key):\n    return load(key)\n", 1),
        # The deleted file had no `load` at the base: the PR did not remove it.
        ("def save(item):\n    return item\n", _PY_VIEW, 0),
    ],
)
def test_an_import_broken_before_the_pr_is_not_blamed(
    base_store: str, view: str, found: int
) -> None:
    repository = Repository({"pkg/view.py": view}, base={"pkg/store.py": base_store})

    result = _select(repository, _diff_map(_deletion(repository)), _load_hints("pkg/view.py"))

    assert result.places == ()
    assert (result.found, result.confirmed) == (found, 0)
    assert dict(result.dropped) == ({"rag_edge_unconfirmed": 1} if found else {})


def test_an_importer_the_pr_fixes_is_no_candidate() -> None:
    # The PR moves the module and changes the import of `pkg/view.py` to the new path.
    view = "from .storage import load\n\n\ndef show(key):\n    return load(key)\n"
    repository = Repository(
        {"pkg/storage.py": _PY_BASE_STORE, "pkg/view.py": view},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/storage.py": None, "pkg/view.py": _PY_VIEW},
    )
    diff_map = _diff_map(
        _vcs_file(
            repository,
            "pkg/storage.py",
            None,
            additions=0,
            status="renamed",
            previous="pkg/store.py",
        ),
        _vcs_file(
            repository,
            "pkg/view.py",
            "@@ -1,1 +1,1 @@\n-from .store import load\n+from .storage import load",
            deletions=1,
        ),
    )

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert (result.places, result.omissions, result.found) == ((), (), 0)


def _package_move() -> tuple[Repository, DiffMap]:
    """A PR that turns the module ``pkg/store.py`` into the package ``pkg/store``."""
    repository = Repository(
        {"pkg/store/__init__.py": _PY_BASE_STORE, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE, "pkg/store/__init__.py": None},
    )
    added = "@@ -0,0 +1,6 @@\n" + "\n".join(
        f"+{line}" for line in _PY_BASE_STORE.rstrip("\n").split("\n")
    )
    diff_map = _diff_map(
        _deletion(repository),
        _vcs_file(repository, "pkg/store/__init__.py", added, additions=6, status="added"),
    )
    return repository, diff_map


def test_an_import_that_still_resolves_at_head_is_no_broken_reference() -> None:
    # The same specifier still binds `load`, now from the package.
    repository, diff_map = _package_move()

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)
    assert (result.found, result.confirmed) == (1, 0)


def test_a_head_module_that_cannot_be_read_is_no_evidence_of_a_broken_reference() -> None:
    # The package lies under an ignored path: at head the import cannot be looked into,
    # which shows nothing about this PR.
    repository, diff_map = _package_move()

    result = _select(
        repository,
        diff_map,
        _load_hints("pkg/view.py"),
        denied=lambda path: path.startswith("pkg/store/"),
    )

    assert result.places == ()
    assert result.omissions == (RagOmission("rag_edge_unconfirmed", "pkg/view.py"),)
    assert (result.found, result.confirmed) == (1, 0)


@pytest.mark.parametrize(
    ("case", "view", "broken"),
    [
        # The module is gone: an incomplete listing cannot show its absence, neither for
        # a relative nor for an absolute import.
        ("deleted_file", _PY_VIEW, False),
        (
            "deleted_file",
            "from pkg.store import load\n\n\ndef show(key):\n    return load(key)\n",
            False,
        ),
        # The module is there without the name: that needs no complete listing.
        ("renamed_symbol", _PY_VIEW, True),
    ],
)
def test_on_an_incomplete_head_tree_only_a_module_without_the_name_shows_a_break(
    case: str, view: str, broken: bool
) -> None:
    repository, diff_map = _removal(case)
    repository.files["pkg/view.py"] = view
    repository.head_complete = False

    result = _select(repository, diff_map, _load_hints("pkg/view.py"))

    assert [place.source.reason for place in result.places] == (
        ["rag_broken_reference"] if broken else []
    )
    assert dict(result.dropped) == ({} if broken else {"rag_edge_unconfirmed": 1})


@pytest.mark.parametrize("reason", ["source_read_failed", "rag_limit"])
def test_a_removed_module_whose_base_cannot_be_read_is_counted_with_its_path(
    reason: str,
) -> None:
    fillers = {f"pkg/filler_{number:02d}.py": "" for number in range(20)}
    repository = Repository(
        {"pkg/view.py": _PY_VIEW, **fillers}, base={"pkg/store.py": _PY_BASE_STORE}
    )
    store = BlobStore()
    reader = CachedBlobSource(
        repository_id=_REPOSITORY,
        source=repository,
        cache=SharedBlobCache(store),
        uow_factory=lambda: BlobCacheUow(store),
    )
    if reason == "source_read_failed":
        repository.failing = frozenset({"pkg/store.py"})
    else:
        # The run's reader spent its reads already, as on an earlier attempt.

        async def spend() -> None:
            for path in fillers:
                await reader.text(repository.sha(path))

        asyncio.run(spend())
    graph = _load_hints("pkg/view.py")

    result = _select(repository, _diff_map(_deletion(repository)), graph, reader=reader)

    # Its exports are unknown, so none is asked; the file is counted, never lost.
    assert graph.reads == [(_REPOSITORY, ())]
    assert result.omissions == (RagOmission(reason, "pkg/store.py"),)
    assert (result.found, dict(result.dropped)) == (1, {reason: 1})


def test_base_extractions_take_at_most_ten_reads_and_leave_the_rest_to_candidates() -> None:
    kept = "def keep():\n    return 0\n"
    removing = [f"pkg/mod_{number:02d}.py" for number in range(12)]
    callers = [f"pkg/user_{number}.py" for number in range(9)]
    repository = Repository(
        {
            "pkg/store.py": _PY_STORE,
            **{path: kept for path in removing},
            **{path: _PY_USER for path in callers},
        },
        base={path: kept + "\n\ndef gone():\n    return 1\n" for path in removing},
    )
    patch = "@@ -1,6 +1,2 @@\n def keep():\n     return 0\n-\n-\n-def gone():\n-    return 1"
    diff_map = _diff_map(
        _vcs_file(repository, "pkg/store.py", _PY_STORE_PATCH),
        *(_vcs_file(repository, path, patch, additions=0, deletions=4) for path in removing),
    )

    result = _select(repository, diff_map, _store_hints(*callers))

    # Twelve base reads would leave eight, too few for the nine callers of `save`.
    assert len([item for item in repository.read if item.endswith("@base")]) == 10
    assert [place.source.path for place in result.places] == callers
    assert result.omissions == (
        RagOmission("rag_limit", "pkg/mod_10.py"),
        RagOmission("rag_limit", "pkg/mod_11.py"),
    )
    assert (result.found, result.confirmed, dict(result.dropped)) == (11, 9, {"rag_limit": 2})


@pytest.mark.parametrize(
    ("diff_old_sha", "unavailable", "reason", "trees"),
    [
        (None, False, "old_revision_unknown", [_HEAD]),
        (_BASE, True, "source_read_failed", [_HEAD, _BASE]),
    ],
)
def test_without_a_readable_base_the_importers_of_a_removed_module_are_dropped(
    diff_old_sha: str | None, unavailable: bool, reason: str, trees: list[str]
) -> None:
    repository = Repository(
        {"pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE},
        base_unavailable=unavailable,
    )
    diff_map = _diff_map(_deletion(repository), diff_old_sha=diff_old_sha)
    graph = Graph(
        {
            _REPOSITORY: (
                GraphHint("pkg/view.py", EdgeKind.IMPORTS, "pkg/store.py", ""),
                _uses("pkg/view.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # The names the deleted module exported are unknown: the graph gives its importers.
    assert graph.reads == [(_REPOSITORY, (GraphTarget("pkg/store.py", ""),))]
    assert result.places == ()
    assert result.omissions == (RagOmission(reason, "pkg/view.py"),)
    assert (result.found, dict(result.dropped)) == (1, {reason: 1})
    assert repository.read == []
    assert repository.trees == trees


def test_a_broken_reference_comes_before_a_reverse_edge() -> None:
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/app.py": _PY_USER, "pkg/view.py": _PY_VIEW},
        base={"pkg/store.py": _PY_BASE_STORE},
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/app.py", "pkg/store.py", "save"),
                _uses("pkg/view.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    assert graph.reads == [
        (_REPOSITORY, (GraphTarget("pkg/store.py", "load"), GraphTarget("pkg/store.py", "save")))
    ]
    assert result.places == (
        _view_place(repository),
        RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=_REPOSITORY,
                revision=_HEAD,
                path="pkg/app.py",
                blob_sha=repository.sha("pkg/app.py"),
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=(LineRange(1, 1), LineRange(4, 5)),
                reason="rag_reverse_edge",
            ),
            edge_kind=EdgeKind.USES_IMPORTED,
            index_commit_sha=_INDEX_COMMIT,
            signature=False,
            text="from .store import save\ndef run(item):\n    return save(item)",
            tokens=8,
        ),
    )
    assert (result.found, result.confirmed) == (2, 2)


def test_an_importer_of_a_removed_and_a_changed_export_shows_both() -> None:
    user = (
        "from .store import load, save\n\n\n"
        "def run(item):\n    return save(item)\n\n\n"
        "def show(key):\n    return load(key)\n"
    )
    repository = Repository(
        {"pkg/store.py": _PY_STORE, "pkg/app.py": user}, base={"pkg/store.py": _PY_BASE_STORE}
    )
    diff_map = _diff_map(_vcs_file(repository, "pkg/store.py", _PY_REMOVAL_PATCH, deletions=4))
    graph = Graph(
        {
            _REPOSITORY: (
                _uses("pkg/app.py", "pkg/store.py", "save"),
                _uses("pkg/app.py", "pkg/store.py", "load"),
            )
        }
    )

    result = _select(repository, diff_map, graph)

    # The broken use first, then the caller of the changed export, in one candidate.
    assert [
        (place.source.ranges, place.source.reason, len(place.definitions))
        for place in result.places
    ] == [
        ((LineRange(1, 1), LineRange(8, 9)), "rag_broken_reference", 1),
        ((LineRange(1, 1), LineRange(4, 5)), "rag_reverse_edge", 0),
    ]
    assert (result.found, result.confirmed) == (1, 1)


# The RAG slot of the review context (ADR 0001 §9): after L1 and L4, before L2 and L3.

_PLAN = RagPlan(index_commit_sha=_INDEX_COMMIT, stale=False)
_MODELS = 'class Item:\n    """An item."""\n\n    def check(self):\n        return True\n'
_SLOT_STORE = (
    "from .models import Item\n\n\ndef save(item: Item):\n    item.check()\n    return item\n"
)
_SLOT_PATCH = "@@ -4,2 +4,3 @@\n def save(item: Item):\n+    item.check()\n     return item"
_TOO_LARGE_PATCH = "@@ -0,0 +1,3001 @@\n" + "\n".join("+x" for _ in range(3_001))


class TreeUnavailable(Repository):
    async def tree(self, revision: str) -> RevisionTree:
        raise ConnectionError("provider unavailable")


def _build(
    repository: Repository,
    diff_map: DiffMap,
    graph: Graph | None,
    *,
    plan: RagPlan | None = _PLAN,
    budget: int = 100_000,
    rules_text: str | None = None,
) -> ReviewContextPayload:
    """Build the context of a run; with a graph, the builder has the RAG sources of the run."""
    policy = parse_review_ignore(rules_text)
    snapshot = build_policy_snapshot(
        RunPolicyPins(
            UUID(int=2),
            diff_map.repository_id,
            "b" * 40,
            UUID(int=4),
            "2" * 64,
            PromptBindings("3" * 64, "", ""),
            ExecutionProfile.fast("model-a", ""),
        ),
        rules=missing_document(RULES_DOCUMENT_PATH),
        agents=missing_document(AGENTS_DOCUMENT_PATH),
        ignore=policy,
        snapshot_id=UUID(int=3),
    )
    matcher = IgnoreMatcher(policy)
    store = BlobStore()
    builder = BuildReviewContext(
        source=repository,
        parser=InProcessParser(),
        counter=Words(),
        retriever=graph,
        rag_blobs=None
        if graph is None
        else CachedBlobSource(
            repository_id=diff_map.repository_id,
            source=repository,
            cache=SharedBlobCache(store),
            uow_factory=lambda: BlobCacheUow(store),
        ),
    )
    return asyncio.run(
        builder.execute(
            diff_map=diff_map,
            snapshot=snapshot,
            matcher=matcher,
            ignore=apply_ignore(matcher, diff_map),
            token_budget=budget,
            rag=plan,
        )
    )


def _slot_repository(**importers: str) -> tuple[Repository, DiffMap]:
    """A PR that changes ``save``, which reads the imported ``Item``: L1 is 11 tokens, the
    L4 signature of ``Item`` 4, the full file (L3) 10 and its window (L2) 6."""
    repository = Repository({"pkg/models.py": _MODELS, "pkg/store.py": _SLOT_STORE, **importers})
    return repository, _diff_map(_vcs_file(repository, "pkg/store.py", _SLOT_PATCH))


def _bulky_user(words: int) -> str:
    """An importer of ``save`` whose one place is ``words + 8`` tokens."""
    return (
        "from .store import save\n\n\ndef run(item):\n"
        f'    """{" ".join(["w"] * words)}"""\n    return save(item)\n'
    )


def test_a_retrieved_block_is_read_at_the_pr_head_and_counted_in_the_payload() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    payload = _build(repository, diff_map, _store_hints("pkg/app.py", "pkg/gone.py"))

    (block,) = payload.retrieved
    assert (block.block_id, block.level, block.representation) == (
        "RAG:pkg/app.py:1-1,4-5",
        ContextLevel.RAG,
        Representation.EXACT_LINES,
    )
    assert block.source == SourceRef(
        role=SourceRole.RETRIEVED_EXCERPT,
        repository_id=_REPOSITORY,
        # The revision of the PR side, never the commit of the index.
        revision=_HEAD,
        path="pkg/app.py",
        blob_sha=repository.sha("pkg/app.py"),
        origin_paths=(),
        side=DiffSide.RIGHT,
        ranges=(LineRange(1, 1), LineRange(4, 5)),
        reason="rag_reverse_edge",
    )
    assert (block.edge_kind, block.index_commit_sha) == (EdgeKind.USES_IMPORTED, _INDEX_COMMIT)
    assert (block.text, block.tokens) == (
        "from .store import save\ndef run(item):\n    return save(item)",
        8,
    )
    # A retrieved block belongs to no diff file: it is kept apart, and still counted.
    assert ContextLevel.RAG not in {item.level for item in payload.blocks}
    assert payload.levels_present == (
        ContextLevel.L1,
        ContextLevel.L3,
        ContextLevel.L4,
        ContextLevel.RAG,
    )
    assert payload.tokens_used == 11 + 10 + 4 + 8
    assert payload.omissions == (ContextOmission("rag_candidate_missing", "pkg/gone.py"),)
    stats = payload.rag
    assert stats is not None and stats.index is not None and stats.index.usable
    assert (stats.reason, stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (
        None,
        2,
        1,
        {"rag_candidate_missing": 1},
        8,
    )

    summary = context_summary(diff_map, payload)

    assert summary["levels_present"] == ["L1", "L3", "L4", "RAG"]
    assert summary["tokens_used"] == 33
    assert summary["tokens_by_role"] == {
        "diff": 11,
        "ast_excerpt": 4,
        "source_excerpt": 10,
        "retrieved_excerpt": 8,
    }
    assert summary["tokens_by_level"] == {"L1": 11, "L4": 4, "L3": 10, "RAG": 8}
    assert summary["blocks_by_level"] == {"L1": 1, "L4": 1, "L3": 1, "RAG": 1}


def test_the_rag_slot_displaces_l3_but_never_l1_or_l4() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")

    # L1, L4 and the full file fill the budget exactly.
    without = _build(repository, diff_map, graph, plan=None, budget=25)
    with_rag = _build(repository, diff_map, graph, budget=25)

    assert [item.level for item in without.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    assert without.omissions == ()
    assert with_rag.blocks == tuple(
        item for item in without.blocks if item.level is not ContextLevel.L3
    )
    assert [item.tokens for item in with_rag.retrieved] == [8]
    # Two tokens are left: neither the full file nor its window fits.
    assert with_rag.omissions == (ContextOmission("budget", "pkg/store.py"),)
    assert with_rag.tokens_used == 11 + 4 + 8
    assert len(graph.reads) == 1


def test_the_rag_slot_holds_at_most_4000_tokens() -> None:
    paths = ["pkg/user_0.py", "pkg/user_1.py", "pkg/user_2.py"]
    repository, diff_map = _slot_repository(**{path: _bulky_user(1_492) for path in paths})

    payload = _build(repository, diff_map, _store_hints(*paths))

    # Two places of 1 500 tokens fit the slot; the third would pass 4 000.
    assert [(item.source.path, item.tokens) for item in payload.retrieved] == [
        ("pkg/user_0.py", 1_500),
        ("pkg/user_1.py", 1_500),
    ]
    assert payload.omissions == (ContextOmission("rag_limit", "pkg/user_2.py"),)
    # The budget left after the slot still goes to L3.
    assert [item.level for item in payload.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    stats = payload.rag
    assert stats is not None
    # A candidate whose place does not fit is still confirmed.
    assert (stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (3, 3, {}, 3_000)


def test_the_rag_slot_is_bounded_by_what_l1_and_l4_leave() -> None:
    paths = ["pkg/user_0.py", "pkg/user_1.py"]
    repository, diff_map = _slot_repository(**{path: _bulky_user(1_492) for path in paths})

    # L1 and L4 take 15 tokens and leave 1 600, less than the slot.
    payload = _build(repository, diff_map, _store_hints(*paths), budget=15 + 1_600)

    assert [item.source.path for item in payload.retrieved] == ["pkg/user_0.py"]
    assert payload.omissions == (ContextOmission("rag_limit", "pkg/user_1.py"),)
    # The full file takes 10 of the 100 tokens the slot left.
    assert [item.level for item in payload.blocks] == [
        ContextLevel.L1,
        ContextLevel.L4,
        ContextLevel.L3,
    ]
    assert payload.tokens_used == 15 + 1_500 + 10


def test_an_index_not_usable_at_the_graph_read_gives_no_block_and_its_fresh_status() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")
    graph.status = IndexStatus.EMPTY

    payload = _build(repository, diff_map, graph)

    assert payload.retrieved == ()
    stats = payload.rag
    assert stats is not None and stats.index is not None
    assert stats.index.status is RagIndexStatus.EMPTY
    assert (stats.reason, stats.found, stats.tokens) == (None, 0, 0)
    assert "pkg/app.py" not in repository.read


def _returned_early(reason: str, graph: Graph) -> ReviewContextPayload:
    """A build with a plan that returns before the RAG slot, for each reason it has."""
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    changed = _vcs_file(repository, "pkg/store.py", _SLOT_PATCH)
    match reason:
        case "diff_inventory_incomplete":
            return _build(repository, _diff_map(changed, listed=2), graph)
        case "no_changes":
            return _build(repository, _diff_map(), graph)
        case "all_changes_ignored":
            return _build(repository, diff_map, graph, rules_text="```review-ignore\n**\n```\n")
        case "no_reviewable_files":
            unanchorable = _vcs_file(repository, "pkg/store.py", None, additions=3)
            return _build(repository, _diff_map(unanchorable), graph)
        case "diff_too_large":
            large = _vcs_file(
                repository, "pkg/big.py", _TOO_LARGE_PATCH, additions=3_001, status="added"
            )
            return _build(repository, _diff_map(large), graph)
        case "budget":
            # Not even the first hunk fits: L1 is cut.
            return _build(repository, diff_map, graph, budget=1)
        case "tree_unavailable":
            unavailable = TreeUnavailable(repository.files)
            unavailable_map = _diff_map(_vcs_file(unavailable, "pkg/store.py", _SLOT_PATCH))
            return _build(unavailable, unavailable_map, graph)
    raise AssertionError(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "diff_inventory_incomplete",
        "no_changes",
        "all_changes_ignored",
        "no_reviewable_files",
        "diff_too_large",
        "budget",
        "tree_unavailable",
    ],
)
def test_a_build_that_returns_before_the_slot_gives_its_reason_and_zero_counters(
    reason: str,
) -> None:
    graph = _store_hints("pkg/app.py")

    payload = _returned_early(reason, graph)

    stats = payload.rag
    assert stats == RagStats(reason=reason)
    assert stats is not None and (stats.index, stats.found, stats.tokens) == (None, 0, 0)
    assert payload.retrieved == ()
    assert graph.reads == []


def test_without_a_plan_the_rag_sources_change_nothing() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = _store_hints("pkg/app.py")

    with_sources = _build(repository, diff_map, graph, plan=None)
    without_sources = _build(repository, diff_map, None, plan=None)

    assert with_sources == without_sources
    assert (with_sources.retrieved, with_sources.rag) == ((), None)
    assert graph.reads == []
    assert "pkg/app.py" not in repository.read


def test_a_plan_without_the_rag_sources_is_refused_before_any_read() -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    with pytest.raises(ValueError, match="RAG plan"):
        _build(repository, diff_map, None)
    assert repository.read == []


@dataclass
class FailingGraph(Graph):
    """A graph read that fails once it is asked."""

    error: Exception = field(default_factory=lambda: RuntimeError("graph unavailable"))

    async def read_hints(self, repository_id: UUID, targets: Sequence[GraphTarget]) -> GraphHints:
        self.reads.append((repository_id, tuple(targets)))
        raise self.error


def test_a_failing_rag_slot_keeps_every_other_level_and_reports_the_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})
    graph = FailingGraph(error=RuntimeError("x" * 1_500))

    without = _build(repository, diff_map, graph, plan=None)
    with caplog.at_level(logging.WARNING, logger="app.modules.reviews.application.review_context"):
        payload = _build(repository, diff_map, graph)

    assert len(graph.reads) == 1
    # No block of the slot; L1, L4 and L3 are those of a build without RAG.
    assert (payload.blocks, payload.omissions, payload.tokens_used) == (
        without.blocks,
        without.omissions,
        without.tokens_used,
    )
    assert payload.retrieved == ()
    assert payload.rag == RagStats(error_type="RuntimeError", error_message="x" * 1_000)
    assert [record.getMessage() for record in caplog.records] == [
        f"RAG slot failed head_sha={_HEAD}"
    ]


@pytest.mark.parametrize(
    "error",
    [
        RunCancelled("installation_unavailable"),
        RunFailure("deadline_exceeded", "the run deadline passed"),
        PolicyError(PolicyErrorCode.ACCESS_DENIED, "GitHub answered 403"),
    ],
    ids=["cancel", "deadline", "policy"],
)
def test_control_raised_in_the_rag_slot_ends_the_build(error: Exception) -> None:
    repository, diff_map = _slot_repository(**{"pkg/app.py": _PY_USER})

    with pytest.raises(type(error)) as caught:
        _build(repository, diff_map, FailingGraph(error=error))

    assert caught.value is error


_SLOT_BASE_STORE = (
    "from .models import Item\n\n\ndef save(item: Item):\n    return item\n\n\n"
    "def load(key):\n    return key\n"
)
_SLOT_REMOVAL_PATCH = (
    "@@ -4,6 +4,3 @@\n def save(item: Item):\n+    item.check()\n     return item\n"
    "-\n-\n-def load(key):\n-    return key"
)


def _slot_removal(**importers: str) -> tuple[Repository, DiffMap]:
    """The PR of the slot tests that also removes ``load`` from ``pkg/store.py``."""
    repository = Repository(
        {"pkg/models.py": _MODELS, "pkg/store.py": _SLOT_STORE, **importers},
        base={"pkg/store.py": _SLOT_BASE_STORE},
    )
    patch = _vcs_file(repository, "pkg/store.py", _SLOT_REMOVAL_PATCH, deletions=4)
    return repository, _diff_map(patch)


def test_a_broken_reference_gives_a_block_per_revision_and_its_definition_once() -> None:
    repository, diff_map = _slot_removal(**{"pkg/panel.py": _PY_VIEW, "pkg/view.py": _PY_VIEW})

    payload = _build(repository, diff_map, _load_hints("pkg/panel.py", "pkg/view.py"))

    assert [
        (block.block_id, block.representation, block.source.revision, block.source.side)
        for block in payload.retrieved
    ] == [
        ("RAG:pkg/panel.py:1-1,4-5", Representation.EXACT_LINES, _HEAD, DiffSide.RIGHT),
        ("RAG:pkg/store.py:8-8", Representation.SIGNATURE, _BASE, DiffSide.LEFT),
        ("RAG:pkg/view.py:1-1,4-5", Representation.EXACT_LINES, _HEAD, DiffSide.RIGHT),
    ]
    assert payload.retrieved[1].text == "def load(key):"
    stats = payload.rag
    assert stats is not None
    assert (stats.found, stats.confirmed, dict(stats.dropped), stats.tokens) == (2, 2, {}, 18)


def test_a_broken_reference_is_shown_with_its_definition_or_not_at_all() -> None:
    repository, diff_map = _slot_removal(**{"pkg/view.py": _PY_VIEW})
    without = _build(repository, diff_map, None, plan=None)
    kept = sum(
        block.tokens
        for block in without.blocks
        if block.level in {ContextLevel.L1, ContextLevel.L4}
    )

    # The use (8 tokens) would fit alone; with its definition (2 tokens) it does not.
    payload = _build(repository, diff_map, _load_hints("pkg/view.py"), budget=kept + 9)

    assert payload.retrieved == ()
    assert ContextOmission("rag_limit", "pkg/view.py") in payload.omissions
    stats = payload.rag
    assert stats is not None
    assert (stats.confirmed, stats.tokens) == (1, 0)
