"""Graph RAG candidates of the review context (#144, ADR 0001 §8-§9).

The changed exported symbols and modules of the PR are looked up in the graph of the
default branch, and their importers are the candidates. The graph only suggests: each
candidate is confirmed at the PR head by the import resolver, and every block is read
from the head blob, never placed by the index.

An export the PR removes may break an importer the PR does not touch. The removed
exports are read at the base (``diff_old_sha``) before the graph; an importer is a broken
reference when its import resolved there into the module exporting the name and no
longer does at head (ADR 0001 §8.3). It comes before any other candidate, shown at head
with the definition at the base: a block per revision.

A candidate is read at the PR revision through the shared blob cache (M10). Every
distinct blob counts toward the read limit of a run whether the cache has it or not, so
what a run may show does not depend on the cache. The cache is read in its own short
session, a miss is read from the provider outside any transaction, and the cache write
is a unit of work of its own: no DB transaction spans a GitHub read.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Collection, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Protocol
from uuid import UUID

from app.common.application.unit_of_work import UnitOfWork
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.code_index.domain.rag_index import RagIndex
from app.modules.reviews.application.ast_resolution import (
    BINARY_SOURCE,
    FileUnavailable,
    ImportRef,
    ImportResolution,
    ImportResolver,
    ModuleTarget,
    RevisionTree,
    SymbolRef,
    affected_symbols,
    exported_symbols,
    module_target,
    python_submodule_files,
    symbol_ref,
)
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstFailureReason,
    AstImport,
    AstImportName,
    AstLanguage,
    AstSymbol,
    ImportKind,
    SourceParser,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import LineRange, SourceRef, SourceRole
from app.modules.reviews.application.context_sources import (
    FactsCache,
    checked_source,
    ends_the_run,
    guarded_facts,
)
from app.modules.reviews.application.diff_map import (
    ChangeType,
    DiffFile,
    DiffMap,
    DiffSide,
    RowKind,
)
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
from app.modules.reviews.application.prompt_budget import TokenCounter

_LOGGER = logging.getLogger(__name__)

MAX_RAG_READS = 20
RAG_LIMIT = "rag_limit"
# The slot limits of a run (ADR 0001 §9); the target cap bounds the one graph read.
MAX_RAG_TARGETS = 200
MAX_RAG_FILES = 10
MAX_RAG_PLACES_PER_FILE = 3
# The base extractions of broken references take at most half the reads of a run, so
# the rest stays for the candidates.
MAX_RAG_BASE_READS = 10
# A place is shown whole within the fragment limits of L2 (``review_context``), else
# its signature.
PLACE_MAX_LINES = 200
PLACE_MAX_TOKENS = 2_000
RAG_CANDIDATE_MISSING = "rag_candidate_missing"
RAG_EDGE_UNCONFIRMED = "rag_edge_unconfirmed"
RAG_REVERSE_EDGE = "rag_reverse_edge"
RAG_BROKEN_REFERENCE = "rag_broken_reference"
OLD_REVISION_UNKNOWN = "old_revision_unknown"
EXCLUDED = "excluded"
SOURCE_READ_FAILED = "source_read_failed"
# Why a candidate file is dropped when it cannot be read; any other unusable text or
# parse leaves its edge unconfirmed.
_UNREADABLE = {
    "missing": RAG_CANDIDATE_MISSING,
    SOURCE_READ_FAILED: SOURCE_READ_FAILED,
    RAG_LIMIT: RAG_LIMIT,
}
# A resolution a module read stopped: no evidence that an import broke, nor of an
# imports edge.
_UNREAD = frozenset(
    {
        EXCLUDED,
        BINARY_SOURCE,
        "missing",
        "unsupported_language",
        SOURCE_READ_FAILED,
        RAG_LIMIT,
        *(reason.value for reason in AstFailureReason),
    }
)


class BlobSource(Protocol):
    """Blobs of the PR revision; a read failure raises."""

    async def read_blob(self, blob_sha: str) -> bytes: ...


class RevisionTrees(Protocol):
    """File listings of the repository's revisions; a read failure raises."""

    async def tree(self, revision: str) -> RevisionTree: ...


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
                return checked_source(entry.content.encode("utf-8"))
        try:
            raw = await self._source.read_blob(blob_sha)
        except Exception as exc:
            if ends_the_run(exc):
                raise
            # Extra context is best effort: the diff itself stays available.
            _LOGGER.warning("RAG blob read failed blob_sha=%s", blob_sha, exc_info=True)
            return FileUnavailable("source_read_failed")
        result = checked_source(raw)
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


@dataclass(frozen=True)
class RetrievedPlace:
    """A confirmed place of a candidate, read at a PR revision: one block of the RAG slot.

    ``signature`` is true when the declaration is too large to show whole: the block then
    holds the import line and the signature only. A place of a broken reference carries
    the ``definitions`` it read at the base, a block each: the slot shows them with the
    place or not at all, and a definition once.
    """

    source: SourceRef
    edge_kind: EdgeKind
    index_commit_sha: str
    signature: bool
    text: str
    tokens: int
    definitions: tuple[RetrievedPlace, ...] = ()


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


class _RevisionFiles:
    """The files of one PR revision a candidate reads, without the per-run file limit of L4.

    The ignore policy comes first. A text or facts L4 already loaded in this run is
    reused and is no RAG read; any other file is read through the blob cache and counts
    toward the read limit. A base file with the blob it has at head is the ``head`` file,
    read and parsed once for both sides. As in L4, the guard runs before the facts cache.
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
        head: _RevisionFiles | None = None,
    ) -> None:
        self._tree = tree
        self._blobs = blobs
        self._parser = parser
        self._cache = cache
        self._denied = denied
        self._texts = dict(texts)
        self._facts = dict(facts)
        self._head = head

    async def text(self, path: str) -> str | FileUnavailable:
        if self._denied(path):
            return FileUnavailable(EXCLUDED)
        head = self._same_at_head(path)
        if head is not None:
            return await head.text(path)
        if path not in self._texts:
            sha = self._tree.blob_shas.get(path)
            self._texts[path] = (
                FileUnavailable("missing") if sha is None else await self._blobs.text(sha)
            )
        return self._texts[path]

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        if self._denied(path):
            return FileUnavailable(EXCLUDED)
        head = self._same_at_head(path)
        if head is not None:
            return await head.facts_for(path)
        if path not in self._facts:
            self._facts[path] = await self._load_facts(path)
        return self._facts[path]

    def _same_at_head(self, path: str) -> _RevisionFiles | None:
        sha = self._tree.blob_shas.get(path)
        if self._head is None or sha is None or self._head._tree.blob_shas.get(path) != sha:
            return None
        return self._head

    async def _load_facts(self, path: str) -> AstFacts | FileUnavailable:
        return await guarded_facts(
            path,
            lambda: self.text(path),
            blob_sha=self._tree.blob_shas.get(path),
            parser=self._parser,
            cache=self._cache,
        )


@dataclass(frozen=True)
class _Base:
    """The base of the PR (``diff_old_sha``) as a broken reference reads it."""

    tree: RevisionTree
    files: _RevisionFiles
    resolver: ImportResolver


@dataclass(frozen=True)
class _Removed:
    """The exports the PR takes away, as graph targets (ADR 0001 §8.3).

    Each target maps to the ids of its definitions at the base. Without a readable base
    the names are unknown: the targets are then the modules the PR deletes or renames
    (name ""), and ``unknown`` says why none of their importers can be checked; a
    modified file is in ``omissions`` under that reason. A file whose base extraction
    is not read or fails is in ``omissions``, counted once; one the ignore policy
    denies is only counted in ``excluded``, never named.
    """

    targets: Mapping[GraphTarget, frozenset[str]] = field(default_factory=dict)
    base: _Base | None = None
    unknown: str | None = None
    omissions: tuple[RagOmission, ...] = ()
    excluded: int = 0


@dataclass(frozen=True)
class _Reader:
    """A declaration through which an edge is confirmed, with the imports it reads."""

    symbol: AstSymbol
    items: tuple[AstImport, ...]
    kind: EdgeKind
    reason: str
    definitions: tuple[RetrievedPlace, ...] = ()


class ReverseEdgeSelector:
    """The importers of a PR's changed and removed code, from the graph and confirmed at
    the PR revisions.

    One reverse step beyond L4 (ADR 0001 §3): a file is a candidate through an edge
    ``uses_imported`` to a changed exported symbol or ``imports`` to a changed module, or
    through an edge to an export the PR removes (§8.3). Broken references come first,
    then callers of a changed symbol, then importers of a changed module (§9).
    """

    def __init__(
        self,
        *,
        retriever: GraphRetriever,
        blobs: CachedBlobSource,
        parser: SourceParser,
        cache: FactsCache | None,
        counter: TokenCounter,
        trees: RevisionTrees,
    ) -> None:
        self._retriever = retriever
        self._blobs = blobs
        self._parser = parser
        self._cache = cache
        self._counter = counter
        self._trees = trees

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
        files = _RevisionFiles(
            tree=tree,
            blobs=self._blobs,
            parser=self._parser,
            cache=self._cache,
            denied=denied,
            texts=loaded_texts,
            facts=loaded_facts,
        )
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
        # The removed exports are targets too, so the base is read before the graph.
        removed = await self._removed(
            diff_map=diff_map, go_module=go_module, changed=changed, files=files, denied=denied
        )
        targets = list(dict.fromkeys([*removed.targets, *named, *modules]))
        hints = await self._retriever.read_hints(diff_map.repository_id, targets[:MAX_RAG_TARGETS])
        commit = hints.indexed_commit_sha
        if not hints.index.usable or commit is None:
            return RagCandidates(hints.index)
        selection = _Selection(
            diff_map=diff_map,
            tree=tree,
            go_module=go_module,
            heads=heads,
            files=files,
            removed=removed,
            shown=shown,
            denied=denied,
            counter=self._counter,
            index_commit_sha=commit,
        )
        for omission in removed.omissions:
            selection.drop(omission.reason, omission.path)
        if removed.excluded:
            selection.exclude(removed.excluded)
        if len(targets) > MAX_RAG_TARGETS:
            selection.drop(RAG_LIMIT, None, len(targets) - MAX_RAG_TARGETS)
        in_pr = {path for file in diff_map.files for path in (file.old_path, file.new_path) if path}
        importers: dict[str, list[GraphHint]] = {}
        for hint in hints.hints:
            # The PR's own files are in the context already.
            if hint.path not in in_pr:
                importers.setdefault(hint.path, []).append(hint)
        for path, edges in sorted(
            importers.items(), key=lambda item: _rank(item[1], removed.targets)
        ):
            await selection.candidate(
                path, sorted(edges, key=lambda hint: _rank((hint,), removed.targets))
            )
        return RagCandidates(
            hints.index,
            tuple(selection.places),
            tuple(selection.omissions),
            selection.confirmed,
            dict(selection.dropped),
        )

    async def _removed(
        self,
        *,
        diff_map: DiffMap,
        go_module: str | None,
        changed: Sequence[tuple[DiffFile, AstFacts]],
        files: _RevisionFiles,
        denied: Callable[[str], bool],
    ) -> _Removed:
        """The exports the PR takes away from its old paths, read at the base: each export
        of a deleted or renamed file, and those a modified file loses. A modification
        counts only when it removes lines and its head extraction is complete. The base
        extractions are RAG reads, at most ``MAX_RAG_BASE_READS``; a file past them or
        whose extraction fails is counted with its path (ADR 0001 §8.3)."""
        after = {file.file_id: facts for file, facts in changed}
        sources: list[tuple[DiffFile, str]] = []
        for file in diff_map.files:
            path = file.old_path
            if (
                path is None
                or file.omission_reason is not None
                or language_for_path(path) is None
                or any(denied(name) for name in (path, file.new_path) if name)
            ):
                continue
            if file.change_type in {ChangeType.DELETED, ChangeType.RENAMED} or (
                file.change_type is ChangeType.MODIFIED
                and file.file_id in after
                and not after[file.file_id].has_errors
                and any(row.kind is RowKind.REMOVED for hunk in file.hunks for row in hunk.rows)
            ):
                sources.append((file, path))
        if not sources:
            return _Removed()
        unknown = OLD_REVISION_UNKNOWN
        base: RevisionTree | None = None
        if diff_map.diff_old_sha is not None:
            try:
                base = await self._trees.tree(diff_map.diff_old_sha)
            except Exception as exc:
                if ends_the_run(exc):
                    raise
                _LOGGER.warning(
                    "RAG base tree read failed diff_old_sha=%s",
                    diff_map.diff_old_sha,
                    exc_info=True,
                )
                unknown = SOURCE_READ_FAILED
        if base is None:
            # The names are unknown: the importers of a deleted or renamed module are
            # candidates that cannot be checked, and a modified file is counted instead
            # of the importers of what it may remove.
            return _Removed(
                {
                    GraphTarget(path, ""): frozenset()
                    for file, path in sources
                    if file.change_type is not ChangeType.MODIFIED
                },
                unknown=unknown,
                omissions=tuple(
                    RagOmission(unknown, path)
                    for file, path in sources
                    if file.change_type is ChangeType.MODIFIED
                ),
            )
        base_files = _RevisionFiles(
            tree=base,
            blobs=self._blobs,
            parser=self._parser,
            cache=self._cache,
            denied=denied,
            texts={},
            facts={},
            head=files,
        )
        targets: dict[GraphTarget, frozenset[str]] = {}
        omissions: list[RagOmission] = []
        excluded = 0
        for position, (file, path) in enumerate(sources):
            # Each extraction is at most one read.
            if position >= MAX_RAG_BASE_READS:
                omissions.append(RagOmission(RAG_LIMIT, path))
                continue
            before = await base_files.facts_for(path)
            if isinstance(before, FileUnavailable) and before.reason == EXCLUDED:
                # Never read, and counted without its path.
                excluded += 1
                continue
            if isinstance(before, FileUnavailable):
                # Its exports are unknown: the file is counted instead of its importers.
                reason = RAG_LIMIT if before.reason == RAG_LIMIT else SOURCE_READ_FAILED
                omissions.append(RagOmission(reason, path))
                continue
            kept = (
                set(_exports(after[file.file_id]))
                if file.change_type is ChangeType.MODIFIED
                else set()
            )
            for name in _exports(before):
                if name not in kept:
                    targets.setdefault(
                        GraphTarget(path, name),
                        frozenset(
                            symbol_ref(
                                before, symbol, path=path, revision=base.revision, blob_sha=None
                            ).symbol_id
                            for symbol in exported_symbols(before, name)
                        ),
                    )
        # The base resolves with the module path of the head: a PR that renames the Go
        # module resolves no import at the base, so it blames no importer.
        resolver = ImportResolver(tree=base, provider=base_files, go_module=go_module)
        return _Removed(
            targets,
            _Base(base, base_files, resolver),
            omissions=tuple(omissions),
            excluded=excluded,
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
        files: _RevisionFiles,
        removed: _Removed,
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
        self._removed = removed
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

    def exclude(self, count: int = 1) -> None:
        """An ignored file is counted, never named: no omission."""
        self.dropped[EXCLUDED] = self.dropped.get(EXCLUDED, 0) + count

    async def candidate(self, path: str, hints: Sequence[GraphHint]) -> None:
        if self._denied(path):
            # Never read, and counted without its path.
            self.exclude()
            return
        broken = [hint for hint in hints if _target(hint) in self._removed.targets]
        base = self._removed.base
        if broken and base is None:
            # Without the base no import of it can be checked: nothing is read.
            self.drop(self._removed.unknown or OLD_REVISION_UNKNOWN, path)
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
        found: list[_Reader] = []
        limited = False
        if broken and base is not None:
            found, limited = await self._broken(base, path, facts, broken)
        reverse, stopped = await self._places(path, facts, hints)
        # A broken use comes first; other declarations may still call a changed export. A
        # broken use that calls one too shows the import of both.
        calls = {reader.symbol.index: reader.items for reader in reverse}
        found = [
            replace(reader, items=_joined(reader.items, calls.get(reader.symbol.index, ())))
            for reader in found
        ]
        taken = {reader.symbol.index for reader in found}
        places = [*found, *(reader for reader in reverse if reader.symbol.index not in taken)]
        if not places:
            self.drop(RAG_LIMIT if limited or stopped else RAG_EDGE_UNCONFIRMED, path)
            return
        self.confirmed += 1
        revision = self._tree.revision
        # A definition L4 shows already is not shown twice.
        new = [
            reader
            for reader in places
            if symbol_ref(
                facts, reader.symbol, path=path, revision=revision, blob_sha=None
            ).symbol_id
            not in self._shown
        ]
        if not new:
            return
        self._files_shown += 1
        lines = text.split("\n")
        for reader in new[:MAX_RAG_PLACES_PER_FILE]:
            self.places.append(self._place(path, lines, reader))
        if len(new) > MAX_RAG_PLACES_PER_FILE:
            self.omissions.append(RagOmission(RAG_LIMIT, path, len(new) - MAX_RAG_PLACES_PER_FILE))

    async def _places(
        self, path: str, facts: AstFacts, hints: Sequence[GraphHint]
    ) -> tuple[list[_Reader], bool]:
        """The innermost declarations through which a hint is confirmed at head, each with
        the imports it reads; and whether the read limit stopped a resolution."""
        found: dict[int, _Reader] = {}
        limited = False
        for hint in hints:
            head = self._heads.get(hint.target_path)
            if head is None:
                continue
            head_path, head_facts = head
            if head_facts.has_errors:
                # A syntax error can hide what the changed module declares or binds: no
                # edge to it is confirmed, also one whose resolution would not read it.
                continue
            expected = {
                symbol_ref(
                    head_facts, symbol, path=head_path, revision=self._tree.revision, blob_sha=None
                ).symbol_id
                for symbol in exported_symbols(head_facts, hint.target_name)
            }
            for item, binding, target in self._reaching(path, facts, self._tree, head_path):
                # `from pkg import sub` reaches the changed file as a submodule.
                submodule = head_path not in target.files
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
                        # The module is confirmed by its path, a submodule by the name it
                        # binds: the declaration only has to read the binding without
                        # rebinding it, and no member has to resolve. A module the
                        # resolver refused (a syntax error in a file of its package, a
                        # file it cannot read) and a wildcard or dot import, whose use
                        # is a guess, confirm nothing.
                        confirms = (
                            ref.reason != "shadowed"
                            and ref.reason not in _UNREAD
                            and binding.kind is not ImportKind.WILDCARD
                        )
                    else:
                        # The binding resolves to the same export of the changed file.
                        confirms = ref.resolution is ImportResolution.RESOLVED and any(
                            resolved.symbol_id in expected for resolved in ref.resolved_symbols
                        )
                    if confirms and submodule:
                        confirms = not await self._package_binds(
                            target.files[0], binding, head_path
                        )
                    readers[symbol.index] = confirms
                for index in _innermost(facts, readers):
                    reader = found.get(index)
                    if reader is None:
                        found[index] = _Reader(
                            facts.symbols[index], (item,), hint.kind, RAG_REVERSE_EDGE
                        )
                    elif item not in reader.items:
                        # The place shows the import of every binding it reads.
                        found[index] = replace(reader, items=(*reader.items, item))
        return list(found.values()), limited

    async def _broken(
        self, base: _Base, path: str, facts: AstFacts, hints: Sequence[GraphHint]
    ) -> tuple[list[_Reader], bool]:
        """The innermost declarations whose import of a removed export resolved at the
        base and no longer does at head, each with the definitions it read there; and
        whether the read limit stopped a resolution (ADR 0001 §8.3).

        A candidate is no PR file, so it has one blob at both sides: the one extraction
        is resolved in both trees, and the import keeps its specifier. Another blob is no
        evidence about this PR.
        """
        if base.tree.blob_shas.get(path) != self._tree.blob_shas.get(path):
            return [], False
        found: dict[int, tuple[tuple[AstImport, ...], EdgeKind]] = {}
        read: dict[int, dict[str, SymbolRef]] = {}
        limited = False
        for hint in hints:
            expected = self._removed.targets[_target(hint)]
            for item, binding, _ in self._reaching(path, facts, base.tree, hint.target_path):
                readers: dict[int, bool] = {}
                for symbol in facts.symbols:
                    before = await base.resolver.resolve_binding(
                        importer_path=path,
                        facts=facts,
                        symbol=symbol,
                        item=item,
                        binding=binding,
                    )
                    if before is None:
                        continue
                    limited = limited or before.reason == RAG_LIMIT
                    # Causality: at the base the binding brought the removed export.
                    removed = (
                        [ref for ref in before.resolved_symbols if ref.symbol_id in expected]
                        if before.resolution is ImportResolution.RESOLVED
                        else []
                    )
                    breaks = False
                    if removed:
                        after = await self._resolver.resolve_binding(
                            importer_path=path,
                            facts=facts,
                            symbol=symbol,
                            item=item,
                            binding=binding,
                        )
                        limited = limited or (after is not None and after.reason == RAG_LIMIT)
                        breaks = _breaks(after, self._tree)
                    readers[symbol.index] = breaks
                    if breaks:
                        read.setdefault(symbol.index, {}).update(
                            (ref.symbol_id, ref) for ref in removed
                        )
                for index in _innermost(facts, readers):
                    # The place shows the import of every binding that broke.
                    items, kind = found.get(index, ((), hint.kind))
                    found[index] = (_joined(items, (item,)), kind)
        readers_found: list[_Reader] = []
        for index, (items, kind) in found.items():
            definitions = [await self._definition(base, ref) for ref in read[index].values()]
            readers_found.append(
                _Reader(
                    facts.symbols[index],
                    items,
                    kind,
                    RAG_BROKEN_REFERENCE,
                    tuple(place for place in definitions if place is not None),
                )
            )
        return readers_found, limited

    def _reaching(
        self, path: str, facts: AstFacts, tree: RevisionTree, changed_path: str
    ) -> Iterator[tuple[AstImport, AstImportName, ModuleTarget]]:
        """The bindings of ``path`` that can reach ``changed_path`` in ``tree``, with the
        module each imports, by path alone: a binding that cannot is not resolved, so no
        read is spent on a module the edge is not about. Python ``from pkg import sub``
        reaches the submodule for any edge kind: the indexer gives it an ``imports``
        edge."""
        for item in facts.imports:
            target = module_target(path, item.module, facts.language, tree, self._go_module)
            if target.resolution is not ImportResolution.RESOLVED:
                continue
            for binding in item.names:
                if changed_path in target.files or _binds_submodule(
                    facts.language, binding, target, changed_path, tree
                ):
                    yield item, binding, target

    async def _package_binds(
        self, package_init: str, binding: AstImportName, submodule: str
    ) -> bool:
        """Whether the package binds the name of ``binding`` over its ``submodule``: by a
        top-level declaration of that name, or by an import of it from anything but the
        submodule itself. ``from pkg import name`` then binds that, not the submodule,
        which the resolver does not tell (#130)."""
        facts = await self._files.facts_for(package_init)
        if isinstance(facts, FileUnavailable):
            # The resolver reads the `__init__` always, unless the binding is shadowed:
            # this is its outcome. An ignored package, one past the read limit or one
            # that cannot be read cannot show that the name is the submodule.
            return True
        if any(item.enclosing is None and item.name == binding.name for item in facts.symbols):
            return True
        for item in facts.imports:
            target = module_target(
                package_init, item.module, facts.language, self._tree, self._go_module
            )
            for name in item.names:
                if name.alias == binding.name and not _imports_module(
                    facts.language, name, target, submodule, self._tree
                ):
                    return True
        return False

    async def _definition(self, base: _Base, ref: SymbolRef) -> RetrievedPlace | None:
        """The signature of a removed definition, read at the base."""
        text = await base.files.text(ref.path)
        if isinstance(text, FileUnavailable):
            return None
        lines = text.split("\n")
        ranges = _merged(ref.signature_ranges)
        body = "\n".join(
            line for part in ranges for line in lines[part.start_line - 1 : part.end_line]
        )
        return RetrievedPlace(
            source=SourceRef(
                role=SourceRole.RETRIEVED_EXCERPT,
                repository_id=self._diff_map.repository_id,
                revision=base.tree.revision,
                path=ref.path,
                blob_sha=ref.blob_sha,
                origin_paths=(),
                side=DiffSide.LEFT,
                ranges=ranges,
                reason=RAG_BROKEN_REFERENCE,
            ),
            edge_kind=EdgeKind.USES_IMPORTED,
            index_commit_sha=self._index_commit_sha,
            signature=True,
            text=body,
            tokens=self._counter.count(body),
        )

    def _place(self, path: str, lines: list[str], reader: _Reader) -> RetrievedPlace:
        symbol = reader.symbol
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
                *(LineRange(item.line, item.line) for item in reader.items),
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
                reason=reader.reason,
            ),
            edge_kind=reader.kind,
            index_commit_sha=self._index_commit_sha,
            signature=not whole,
            text=text,
            tokens=self._counter.count(text),
            definitions=reader.definitions,
        )


def _export_names(facts: AstFacts, symbol: AstSymbol) -> tuple[str, ...]:
    """The names the graph knows a changed symbol by: the exports of its top-level
    declaration (as ``exported_symbols`` reads them)."""
    while symbol.enclosing is not None:
        symbol = facts.symbols[symbol.enclosing]
    if facts.language in {AstLanguage.PYTHON, AstLanguage.GO}:
        return (symbol.name,) if symbol.exported and symbol.name is not None else ()
    return tuple(name for name, index in facts.exports if index == symbol.index)


def _exports(facts: AstFacts) -> tuple[str, ...]:
    """Every name a file exports, as ``exported_symbols`` looks it up."""
    if facts.language in {AstLanguage.PYTHON, AstLanguage.GO}:
        names = (item.name for item in facts.symbols if item.enclosing is None and item.exported)
        return tuple(dict.fromkeys(name for name in names if name is not None))
    return tuple(dict.fromkeys(name for name, _ in facts.exports))


def _target(hint: GraphHint) -> GraphTarget:
    return GraphTarget(hint.target_path, hint.target_name)


def _rank(hints: Iterable[GraphHint], removed: Collection[GraphTarget]) -> int:
    """Broken references first, then callers of a changed symbol, then importers of a
    changed module."""
    hints = tuple(hints)
    if any(_target(hint) in removed for hint in hints):
        return 0
    return 1 if any(hint.kind is EdgeKind.USES_IMPORTED for hint in hints) else 2


def _breaks(after: ImportRef | None, tree: RevisionTree) -> bool:
    """Whether the head outcome of an import that resolved at the base shows it broken
    (ADR 0001 §8.3): any outcome but a resolution, except one a module read stopped and
    an absence an incomplete head listing cannot show. ``None``: the import no longer
    binds the name the declaration reads."""
    if after is None:
        return True
    if after.resolution is ImportResolution.RESOLVED or after.reason in _UNREAD:
        return False
    return (
        tree.complete
        or after.resolution not in {ImportResolution.EXTERNAL, ImportResolution.MISSING}
        or after.reason == "name_not_declared"
    )


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


def _joined(items: tuple[AstImport, ...], more: Iterable[AstImport]) -> tuple[AstImport, ...]:
    """``items`` and then each import of ``more`` it lacks, once."""
    joined = list(items)
    for item in more:
        if item not in joined:
            joined.append(item)
    return tuple(joined)


def _imports_module(
    language: AstLanguage,
    binding: AstImportName,
    target: ModuleTarget,
    path: str,
    tree: RevisionTree,
) -> bool:
    """Whether an import binds the module at ``path`` itself: ``import pkg.sub as sub``,
    or ``from pkg import sub`` as the resolver reads it."""
    if target.resolution is not ImportResolution.RESOLVED:
        return False
    if binding.kind is ImportKind.MODULE:
        return path in target.files
    return _binds_submodule(language, binding, target, path, tree)


def _innermost(facts: AstFacts, readers: Mapping[int, bool]) -> list[int]:
    """The confirming declarations (``True``) of ``readers``, innermost only: a class
    reads what its methods read, so a declaration is a place only when none of the
    declarations it encloses reads the same binding."""
    outer = {parent for index in readers for parent in _enclosing_indexes(facts, index)}
    return [index for index, reads in readers.items() if reads and index not in outer]


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
