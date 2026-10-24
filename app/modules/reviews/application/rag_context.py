"""Graph RAG candidates of the review context (#144, ADR 0001 §8-§9).

The changed exported symbols and modules of the PR are looked up in the graph of the
default branch, and their importers are the candidates. The graph only suggests: each
candidate is confirmed at the PR head by the import resolver, and every block is read
from the head blob, never placed by the index.

A candidate is read at the PR revision through the shared blob cache (M10). Every
distinct blob counts toward the read limit of a run whether the cache has it or not, so
what a run may show does not depend on the cache. The cache is read in its own short
session, a miss is read from the provider outside any transaction, and the cache write
is a unit of work of its own: no DB transaction spans a GitHub read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.ast_resolution import (
    BINARY_SOURCE,
    FileUnavailable,
    ImportResolution,
    ImportResolver,
    ModuleTarget,
    RevisionTree,
    affected_symbols,
    exported_symbols,
    module_target,
    python_submodule_files,
    symbol_ref,
)
from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    AstFacts,
    AstFailureReason,
    AstImport,
    AstImportName,
    AstLanguage,
    AstSymbol,
    ImportKind,
    ParseRequest,
    SourceParser,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.diff_map import DiffFile, DiffMap, DiffSide
from app.modules.reviews.application.get_run_file_lines import (
    BLOB_CACHE_TTL,
    BlobCache,
    BlobCacheKey,
    BlobCacheStatus,
    BlobCacheWriter,
)
from app.modules.reviews.application.graph_retriever import (
    GraphHint,
    GraphRetriever,
    GraphTarget,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.prompt_budget import TokenCounter
from app.modules.reviews.application.run_failures import RunCancelled, RunFailure
from app.modules.reviews.application.vcs_diff import is_binary_content

_LOGGER = logging.getLogger(__name__)

MAX_RAG_READS = 20
RAG_LIMIT = "rag_limit"
# The slot limits of a run (ADR 0001 §9); the target cap bounds the one graph read.
MAX_RAG_TARGETS = 200
MAX_RAG_FILES = 10
MAX_RAG_PLACES_PER_FILE = 3
# A place is shown whole within the fragment limits of L2 (``review_context``), else
# its signature.
PLACE_MAX_LINES = 200
PLACE_MAX_TOKENS = 2_000
RAG_CANDIDATE_MISSING = "rag_candidate_missing"
RAG_EDGE_UNCONFIRMED = "rag_edge_unconfirmed"
RAG_REVERSE_EDGE = "rag_reverse_edge"
EXCLUDED = "excluded"
SOURCE_READ_FAILED = "source_read_failed"
# Why a candidate file is dropped when it cannot be read; any other unusable text or
# parse leaves its edge unconfirmed.
_UNREADABLE = {
    "missing": RAG_CANDIDATE_MISSING,
    SOURCE_READ_FAILED: SOURCE_READ_FAILED,
    RAG_LIMIT: RAG_LIMIT,
}


class BlobSource(Protocol):
    """Blobs of the PR revision; a read failure raises."""

    async def read_blob(self, blob_sha: str) -> bytes: ...


class BlobCacheUnitOfWork(UnitOfWork, Protocol):
    @property
    def blob_cache(self) -> BlobCacheWriter | None: ...


class CachedBlobSource:
    """Candidate texts of one run's PR revision, read through the blob cache.

    A text gets the same size, binary and UTF-8 checks as an L4 read, on a hit as on a
    miss. A failed read, an unusable blob and a broken cache cost a read like any other
    and never fail the run. Reads are awaited one at a time, so the blobs admitted under
    the limit follow the order of the calls.
    """

    def __init__(
        self,
        *,
        repository_id: UUID,
        source: BlobSource,
        cache: BlobCache,
        uow_factory: Callable[[], BlobCacheUnitOfWork],
    ) -> None:
        self._repository_id = repository_id
        self._source = source
        self._cache = cache
        self._uow_factory = uow_factory
        self._texts: dict[str, str | FileUnavailable] = {}

    async def text(self, blob_sha: str) -> str | FileUnavailable:
        if blob_sha in self._texts:
            return self._texts[blob_sha]
        if len(self._texts) >= MAX_RAG_READS:
            return FileUnavailable(RAG_LIMIT)
        result = await self._read(blob_sha)
        self._texts[blob_sha] = result
        return result

    async def _read(self, blob_sha: str) -> str | FileUnavailable:
        key = BlobCacheKey(self._repository_id, blob_sha)
        try:
            entry = await self._cache.get(key)
        except Exception:
            # A broken cache is a miss: the blob is still read from the provider.
            _LOGGER.warning("RAG blob cache get failed blob_sha=%s", blob_sha, exc_info=True)
        else:
            if entry.status is BlobCacheStatus.HIT and entry.content is not None:
                # Other writers of the cache check less: a hit gets the checks of a read.
                return _checked(entry.content.encode("utf-8"))
        try:
            raw = await self._source.read_blob(blob_sha)
        except Exception as exc:
            if isinstance(exc, (RunCancelled, PolicyError)) or (
                isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded"
            ):
                raise
            # Extra context is best effort: the diff itself stays available.
            _LOGGER.warning("RAG blob read failed blob_sha=%s", blob_sha, exc_info=True)
            return FileUnavailable("source_read_failed")
        result = _checked(raw)
        if isinstance(result, str):
            await self._put(key, result)
        return result

    async def _put(self, key: BlobCacheKey, content: str) -> None:
        try:
            async with self._uow_factory() as uow:
                if uow.blob_cache is not None:
                    await uow.blob_cache.put(key, content, ttl=BLOB_CACHE_TTL)
                await uow.commit()
        except Exception:
            # The cache only saves a later read; the text is already in hand.
            _LOGGER.warning("RAG blob cache put failed blob_sha=%s", key.blob_sha, exc_info=True)


def _checked(raw: bytes) -> str | FileUnavailable:
    """The checks of an L4 read (``_Files.text`` in ``review_context``), in its order."""
    if len(raw) > MAX_SOURCE_BYTES:
        return FileUnavailable(AstFailureReason.SOURCE_TOO_LARGE.value)
    if is_binary_content(raw):
        return FileUnavailable(BINARY_SOURCE)
    try:
        return raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return FileUnavailable(AstFailureReason.SOURCE_DECODE_FAILED.value)


@dataclass(frozen=True)
class RetrievedPlace:
    """A confirmed place of a candidate, read at the PR head: one block of the RAG slot.

    ``signature`` is true when the declaration is too large to show whole: the block then
    holds the import line and the signature only.
    """

    source: SourceRef
    edge_kind: EdgeKind
    index_commit_sha: str
    signature: bool
    text: str
    tokens: int


@dataclass(frozen=True)
class RagOmission:
    """A candidate or place not shown and why; an excluded candidate is never named."""

    reason: str
    path: str | None = None
    count: int = 1


@dataclass(frozen=True)
class RagCandidates:
    """The outcome of one selection, places in the priority order of the slot.

    ``index`` is the status read with the hints; when it is not usable nothing is
    selected. Every candidate is counted once: confirmed, or dropped under one reason.
    """

    index: RagIndex
    places: tuple[RetrievedPlace, ...] = ()
    omissions: tuple[RagOmission, ...] = ()
    confirmed: int = 0
    dropped: Mapping[str, int] = field(default_factory=dict)

    @property
    def found(self) -> int:
        return self.confirmed + sum(self.dropped.values())


class FactsCache(Protocol):
    """Facts by blob, as ``AstFactsCache`` of ``review_context``; a miss or a broken
    cache returns ``None`` and never fails a run."""

    async def get(self, blob_sha: str, language: AstLanguage) -> AstFacts | None: ...

    async def put(self, blob_sha: str, language: AstLanguage, facts: AstFacts) -> None: ...


class _HeadFiles:
    """The PR head files a confirmation reads, without the per-run file limit of L4.

    The ignore policy comes first. A text or facts L4 already loaded in this run is
    reused and is no RAG read; any other file is read through the blob cache and counts
    toward the read limit. As in L4, the guard runs before the facts cache.
    """

    def __init__(
        self,
        *,
        tree: RevisionTree,
        blobs: CachedBlobSource,
        parser: SourceParser,
        cache: FactsCache | None,
        denied: Callable[[str], bool],
        texts: Mapping[str, str | FileUnavailable],
        facts: Mapping[str, AstFacts | FileUnavailable],
    ) -> None:
        self._tree = tree
        self._blobs = blobs
        self._parser = parser
        self._cache = cache
        self._denied = denied
        self._texts = dict(texts)
        self._facts = dict(facts)

    async def text(self, path: str) -> str | FileUnavailable:
        if self._denied(path):
            return FileUnavailable(EXCLUDED)
        if path not in self._texts:
            sha = self._tree.blob_shas.get(path)
            self._texts[path] = (
                FileUnavailable("missing") if sha is None else await self._blobs.text(sha)
            )
        return self._texts[path]

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        if self._denied(path):
            return FileUnavailable(EXCLUDED)
        if path not in self._facts:
            self._facts[path] = await self._load_facts(path)
        return self._facts[path]

    async def _load_facts(self, path: str) -> AstFacts | FileUnavailable:
        language = language_for_path(path)
        if language is None:
            return FileUnavailable("unsupported_language")
        text = await self.text(path)
        if isinstance(text, FileUnavailable):
            return text
        sha = self._tree.blob_shas.get(path)
        if self._cache is not None and sha is not None:
            cached = await self._cache.get(sha, language)
            if cached is not None:
                return cached
        parsed = (await self._parser.parse_many((ParseRequest(path, language, text.encode()),)))[
            path
        ]
        if isinstance(parsed, AstFailureReason):
            return FileUnavailable(parsed.value)
        if self._cache is not None and sha is not None:
            await self._cache.put(sha, language, parsed)
        return parsed


class ReverseEdgeSelector:
    """The importers of a PR's changed code, from the graph and confirmed at the PR head.

    One reverse step beyond L4 (ADR 0001 §3): a file is a candidate through an edge
    ``uses_imported`` to a changed exported symbol or ``imports`` to a changed module.
    Callers of a changed symbol come before importers of a changed module (§9).
    """

    def __init__(
        self,
        *,
        retriever: GraphRetriever,
        blobs: CachedBlobSource,
        parser: SourceParser,
        cache: FactsCache | None,
        counter: TokenCounter,
    ) -> None:
        self._retriever = retriever
        self._blobs = blobs
        self._parser = parser
        self._cache = cache
        self._counter = counter

    async def select(
        self,
        *,
        diff_map: DiffMap,
        tree: RevisionTree,
        go_module: str | None,
        changed: Sequence[tuple[DiffFile, AstFacts]],
        loaded_texts: Mapping[str, str | FileUnavailable],
        loaded_facts: Mapping[str, AstFacts | FileUnavailable],
        shown: Collection[str],
        denied: Callable[[str], bool],
    ) -> RagCandidates:
        """Select on what L4 left: the head ``tree``, the PR files with head facts in
        their L4 order, what L4 loaded, and the symbol ids of the definitions it shows."""
        heads: dict[str, tuple[str, AstFacts]] = {}
        named: list[GraphTarget] = []
        modules: list[GraphTarget] = []
        for file, facts in changed:
            if file.new_path is None:
                continue
            # The graph knows a file by its default-branch path.
            graph_path = file.old_path or file.new_path
            heads[graph_path] = (file.new_path, facts)
            affected = affected_symbols(facts, file)
            for symbol in affected.symbols:
                named.extend(GraphTarget(graph_path, name) for name in _export_names(facts, symbol))
            if affected.module_level:
                modules.append(GraphTarget(graph_path, ""))
        targets = list(dict.fromkeys([*named, *modules]))
        hints = await self._retriever.read_hints(diff_map.repository_id, targets[:MAX_RAG_TARGETS])
        commit = hints.indexed_commit_sha
        if not hints.index.usable or commit is None:
            return RagCandidates(hints.index)
        selection = _Selection(
            diff_map=diff_map,
            tree=tree,
            go_module=go_module,
            heads=heads,
            files=_HeadFiles(
                tree=tree,
                blobs=self._blobs,
                parser=self._parser,
                cache=self._cache,
                denied=denied,
                texts=loaded_texts,
                facts=loaded_facts,
            ),
            shown=shown,
            denied=denied,
            counter=self._counter,
            index_commit_sha=commit,
        )
        if len(targets) > MAX_RAG_TARGETS:
            selection.drop(RAG_LIMIT, None, len(targets) - MAX_RAG_TARGETS)
        in_pr = {path for file in diff_map.files for path in (file.old_path, file.new_path) if path}
        importers: dict[str, list[GraphHint]] = {}
        for hint in hints.hints:
            # The PR's own files are in the context already.
            if hint.path not in in_pr:
                importers.setdefault(hint.path, []).append(hint)
        for path, edges in sorted(importers.items(), key=lambda item: _rank(item[1])):
            await selection.candidate(path, sorted(edges, key=lambda hint: _rank((hint,))))
        return RagCandidates(
            hints.index,
            tuple(selection.places),
            tuple(selection.omissions),
            selection.confirmed,
            dict(selection.dropped),
        )


class _Selection:
    """The candidates of one selection, confirmed one at a time in priority order."""

    def __init__(
        self,
        *,
        diff_map: DiffMap,
        tree: RevisionTree,
        go_module: str | None,
        heads: Mapping[str, tuple[str, AstFacts]],
        files: _HeadFiles,
        shown: Collection[str],
        denied: Callable[[str], bool],
        counter: TokenCounter,
        index_commit_sha: str,
    ) -> None:
        self._diff_map = diff_map
        self._tree = tree
        self._go_module = go_module
        self._heads = heads
        self._files = files
        self._resolver = ImportResolver(tree=tree, provider=files, go_module=go_module)
        self._shown = shown
        self._denied = denied
        self._counter = counter
        self._index_commit_sha = index_commit_sha
        self._files_shown = 0
        self.places: list[RetrievedPlace] = []
        self.omissions: list[RagOmission] = []
        self.confirmed = 0
        self.dropped: dict[str, int] = {}

    def drop(self, reason: str, path: str | None, count: int = 1) -> None:
        self.dropped[reason] = self.dropped.get(reason, 0) + count
        self.omissions.append(RagOmission(reason, path, count))

    async def candidate(self, path: str, hints: Sequence[GraphHint]) -> None:
        if self._denied(path):
            # Never read, and counted without its path.
            self.dropped[EXCLUDED] = self.dropped.get(EXCLUDED, 0) + 1
            return
        if self._files_shown >= MAX_RAG_FILES:
            self.drop(RAG_LIMIT, path)
            return
        facts = await self._files.facts_for(path)
        if isinstance(facts, FileUnavailable):
            self.drop(_UNREADABLE.get(facts.reason, RAG_EDGE_UNCONFIRMED), path)
            return
        text = await self._files.text(path)
        if isinstance(text, FileUnavailable):
            self.drop(_UNREADABLE.get(text.reason, RAG_EDGE_UNCONFIRMED), path)
            return
        places, limited = await self._places(path, facts, hints)
        if not places:
            self.drop(RAG_LIMIT if limited else RAG_EDGE_UNCONFIRMED, path)
            return
        self.confirmed += 1
        revision = self._tree.revision
        # A definition L4 shows already is not shown twice.
        new = [
            place
            for place in places
            if symbol_ref(facts, place[0], path=path, revision=revision, blob_sha=None).symbol_id
            not in self._shown
        ]
        if not new:
            return
        self._files_shown += 1
        lines = text.split("\n")
        for symbol, item, kind in new[:MAX_RAG_PLACES_PER_FILE]:
            self.places.append(self._place(path, lines, symbol, item, kind))
        if len(new) > MAX_RAG_PLACES_PER_FILE:
            self.omissions.append(RagOmission(RAG_LIMIT, path, len(new) - MAX_RAG_PLACES_PER_FILE))

    async def _places(
        self, path: str, facts: AstFacts, hints: Sequence[GraphHint]
    ) -> tuple[list[tuple[AstSymbol, AstImport, EdgeKind]], bool]:
        """The innermost declarations through which a hint is confirmed at head, each with
        the import it reads; and whether the read limit stopped a resolution.

        A class reads what its methods read, so a declaration is a place only when none
        of the declarations it encloses reads the same binding.
        """
        found: dict[int, tuple[AstSymbol, AstImport, EdgeKind]] = {}
        limited = False
        for hint in hints:
            head = self._heads.get(hint.target_path)
            if head is None:
                continue
            head_path, head_facts = head
            expected = {
                symbol_ref(
                    head_facts, symbol, path=head_path, revision=self._tree.revision, blob_sha=None
                ).symbol_id
                for symbol in exported_symbols(head_facts, hint.target_name)
            }
            for item in facts.imports:
                target = module_target(
                    path, item.module, facts.language, self._tree, self._go_module
                )
                if target.resolution is not ImportResolution.RESOLVED:
                    continue
                for binding in item.names:
                    # By path alone: a binding that cannot reach the changed file is not
                    # resolved, so no read is spent on a module the edge is not about.
                    reaches = head_path in target.files or (
                        hint.kind is EdgeKind.USES_IMPORTED
                        and _binds_submodule(facts.language, binding, target, head_path, self._tree)
                    )
                    if not reaches:
                        continue
                    readers: dict[int, bool] = {}
                    for symbol in facts.symbols:
                        ref = await self._resolver.resolve_binding(
                            importer_path=path,
                            facts=facts,
                            symbol=symbol,
                            item=item,
                            binding=binding,
                        )
                        if ref is None:
                            continue
                        limited = limited or ref.reason == RAG_LIMIT
                        if hint.kind is EdgeKind.IMPORTS:
                            # The module is confirmed by its path: the declaration only has
                            # to read the binding without rebinding it.
                            confirms = ref.reason != "shadowed"
                        else:
                            # The binding resolves to the same export of the changed file.
                            confirms = ref.resolution is ImportResolution.RESOLVED and any(
                                resolved.symbol_id in expected for resolved in ref.resolved_symbols
                            )
                        readers[symbol.index] = confirms
                    outer = {
                        parent for index in readers for parent in _enclosing_indexes(facts, index)
                    }
                    for index, confirms in readers.items():
                        if confirms and index not in outer:
                            found.setdefault(index, (facts.symbols[index], item, hint.kind))
        return list(found.values()), limited

    def _place(
        self, path: str, lines: list[str], symbol: AstSymbol, item: AstImport, kind: EdgeKind
    ) -> RetrievedPlace:
        declaration = symbol.declaration_range
        last = len(lines) - (1 if lines and lines[-1] == "" else 0)
        body = "\n".join(lines[declaration.start_line - 1 : declaration.end_line])
        whole = (
            symbol.complete
            and declaration.end_line - declaration.start_line + 1 <= PLACE_MAX_LINES
            and declaration.end_line <= last
            and self._counter.count(body) <= PLACE_MAX_TOKENS
        )
        ranges = _merged(
            (
                LineRange(item.line, item.line),
                *((declaration,) if whole else symbol.signature_ranges),
            )
        )
        text = "\n".join(
            line for part in ranges for line in lines[part.start_line - 1 : part.end_line]
        )
        return RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=self._diff_map.repository_id,
                revision=self._diff_map.head_sha,
                path=path,
                blob_sha=self._tree.blob_shas.get(path),
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=ranges,
                reason=RAG_REVERSE_EDGE,
            ),
            edge_kind=kind,
            index_commit_sha=self._index_commit_sha,
            signature=not whole,
            text=text,
            tokens=self._counter.count(text),
        )


def _export_names(facts: AstFacts, symbol: AstSymbol) -> tuple[str, ...]:
    """The names the graph knows a changed symbol by: the exports of its top-level
    declaration (as ``exported_symbols`` reads them)."""
    while symbol.enclosing is not None:
        symbol = facts.symbols[symbol.enclosing]
    if facts.language in {AstLanguage.PYTHON, AstLanguage.GO}:
        return (symbol.name,) if symbol.exported and symbol.name is not None else ()
    return tuple(name for name, index in facts.exports if index == symbol.index)


def _rank(hints: Iterable[GraphHint]) -> int:
    """Callers of a changed symbol first, then importers of a changed module."""
    return 0 if any(hint.kind is EdgeKind.USES_IMPORTED for hint in hints) else 1


def _binds_submodule(
    language: AstLanguage,
    binding: AstImportName,
    target: ModuleTarget,
    head_path: str,
    tree: RevisionTree,
) -> bool:
    """``from package import name`` can bind the submodule at ``head_path``, as the
    resolver reads it."""
    return (
        language is AstLanguage.PYTHON
        and binding.kind is ImportKind.NAMED
        and binding.name is not None
        and target.files[0].endswith("__init__.py")
        and python_submodule_files(target.files[0], binding.name, tree) == [head_path]
    )


def _enclosing_indexes(facts: AstFacts, index: int) -> Iterator[int]:
    parent = facts.symbols[index].enclosing
    while parent is not None:
        yield parent
        parent = facts.symbols[parent].enclosing


def _merged(ranges: Iterable[LineRange]) -> tuple[LineRange, ...]:
    """Sorted, and joined where they touch (as ``_merge`` of ``review_context``)."""
    merged: list[LineRange] = []
    for part in sorted(ranges, key=lambda value: value.start_line):
        if merged and part.start_line <= merged[-1].end_line + 1:
            merged[-1] = LineRange(merged[-1].start_line, max(merged[-1].end_line, part.end_line))
        else:
            merged.append(part)
    return tuple(merged)
