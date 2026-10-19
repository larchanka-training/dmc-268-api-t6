"""Assemble the review context L1-L4 with provenance (CONTEXT_AND_VERIFICATION_SPEC §3-4).

Deterministic code only: no model call. The ignore policy is applied before any file is
chosen or read, and again for every file reached through an import; a cache hit is never
a permission to show a file. Every block names its source lines, so a later check can
tell what a model was really shown.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from app.modules.reviews.application.ast_resolution import (
    BINARY_SOURCE,
    MAX_IMPORTED_FILES,
    FileUnavailable,
    ImportRef,
    ImportResolution,
    ImportResolver,
    RevisionTree,
    SymbolRef,
    affected_symbols,
    go_module_path,
)
from app.modules.reviews.application.ast_symbols import (
    MAX_SOURCE_BYTES,
    AstFacts,
    AstFailureReason,
    AstLanguage,
    ParseRequest,
    SourceParser,
    SymbolKind,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import (
    LineRange,
    SourceRef,
    SourceRole,
    hunk_source_refs,
)
from app.modules.reviews.application.diff_map import (
    DiffFile,
    DiffHunk,
    DiffMap,
    DiffSide,
    RowKind,
    is_supported_path,
)
from app.modules.reviews.application.policy_errors import PolicyError
from app.modules.reviews.application.policy_snapshot import PolicySnapshot
from app.modules.reviews.application.prompt_budget import (
    TokenCounter,
    _kind_weight,
    _matches_rule,
)
from app.modules.reviews.application.prompt_builder import ReviewRule
from app.modules.reviews.application.review_ignore import IgnoreMatcher, IgnoreResult
from app.modules.reviews.application.run_failures import (
    RequiredSourceUnavailable,
    RunCancelled,
    RunFailure,
)
from app.modules.reviews.application.vcs_diff import _is_binary

_LOGGER = logging.getLogger(__name__)

MAX_REVIEWABLE_LINES = 3_000
WINDOW_LINES = 30
WIDE_WINDOW_LINES = 80
FULL_FILE_MAX_LINES = 1_500
FULL_FILE_MAX_TOKENS = 12_000
FRAGMENT_MAX_LINES = 200
FRAGMENT_MAX_TOKENS = 2_000
AST_CONTEXT_MAX_TOKENS = 8_000
MAX_AST_FILES = 50
_ROW_PREFIX = {RowKind.CONTEXT: " ", RowKind.ADDED: "+", RowKind.REMOVED: "-"}
_ENCLOSING_KINDS = frozenset(
    {SymbolKind.FUNCTION, SymbolKind.METHOD, SymbolKind.CLASS, SymbolKind.TYPE}
)


class ContextLevel(StrEnum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"
    L4 = "L4"


class Representation(StrEnum):
    EXACT_LINES = "exact_lines"
    SIGNATURE = "signature"


class ContextStatus(StrEnum):
    REVIEW = "review"
    SUMMARY_ONLY = "summary_only"
    ALL_CHANGES_IGNORED = "all_changes_ignored"
    NO_CHANGES = "no_changes"


@dataclass(frozen=True)
class ContextBlock:
    block_id: str
    level: ContextLevel
    file_id: str | None
    source: SourceRef
    representation: Representation
    text: str
    tokens: int
    truncated: bool
    # The other side of a diff hunk: removed lines come from the old revision.
    related_sources: tuple[SourceRef, ...] = ()


@dataclass(frozen=True)
class ContextOmission:
    """Something that was not shown and why; an ignored file is counted, never named.

    ``path`` is canonical and may be listed for a model. A name rejected by the path
    contract is kept only in ``diagnostic_path``, for the trace and the UI.
    """

    reason: str
    path: str | None = None
    count: int = 1
    diagnostic_path: str | None = None


@dataclass(frozen=True)
class ReviewContextPayload:
    status: ContextStatus
    reason: str | None
    policy_snapshot_id: UUID
    policy_digest: str
    diff_digest: str
    blocks: tuple[ContextBlock, ...]
    omissions: tuple[ContextOmission, ...]
    imports: tuple[ImportRef, ...]
    levels_present: tuple[ContextLevel, ...]
    reviewable_lines: int
    ignored_files: int
    tokens_used: int


class RevisionSource(Protocol):
    """Files of one repository revision; a read failure raises."""

    async def tree(self, revision: str) -> RevisionTree: ...

    async def read_blob(self, blob_sha: str) -> bytes: ...


class AstFactsCache(Protocol):
    """Facts by blob; a miss or a broken cache returns ``None`` and never fails a run."""

    async def get(self, blob_sha: str, language: AstLanguage) -> AstFacts | None: ...

    async def put(self, blob_sha: str, language: AstLanguage, facts: AstFacts) -> None: ...


def _file_priority(file: DiffFile, path: str, rules: tuple[ReviewRule, ...]) -> float:
    weight = _kind_weight(path) * math.log(file.additions + file.deletions + 1)
    return weight * 1.5 if any(_matches_rule(path, rule) for rule in rules) else weight


def _hunk_text(hunk: DiffHunk, rows: int | None = None) -> str:
    header = f"@@ -{hunk.old_start},{hunk.old_count} +{hunk.new_start},{hunk.new_count} @@"
    shown = hunk.rows if rows is None else hunk.rows[:rows]
    return "\n".join([header, *(_ROW_PREFIX[row.kind] + row.text for row in shown)])


def _merge(ranges: list[LineRange]) -> list[LineRange]:
    merged: list[LineRange] = []
    for item in sorted(ranges, key=lambda value: value.start_line):
        if merged and item.start_line <= merged[-1].end_line + 1:
            merged[-1] = LineRange(merged[-1].start_line, max(merged[-1].end_line, item.end_line))
        else:
            merged.append(item)
    return merged


class _Files:
    """Guarded access to the files of the head revision, with facts and limits."""

    def __init__(
        self,
        *,
        tree: RevisionTree,
        source: RevisionSource,
        parser: SourceParser,
        cache: AstFactsCache | None,
        denied: Callable[[str], bool],
    ) -> None:
        self._tree = tree
        self._source = source
        self._parser = parser
        self._cache = cache
        self._denied = denied
        self._texts: dict[str, str | FileUnavailable] = {}
        self._facts: dict[str, AstFacts | FileUnavailable] = {}
        self.imported: set[str] = set()
        self.changed: set[str] = set()
        # Changed files past the limit of AST files: an import does not bring them back.
        self.over_limit: set[str] = set()

    async def text(self, path: str, blob_sha: str | None = None) -> str | FileUnavailable:
        if path in self._texts:
            return self._texts[path]
        result: str | FileUnavailable
        sha = blob_sha or self._tree.blob_shas.get(path)
        if self._denied(path):
            result = FileUnavailable("excluded")
        elif sha is None:
            result = FileUnavailable("missing")
        else:
            try:
                raw = await self._source.read_blob(sha)
            except Exception as exc:
                if isinstance(exc, (RunCancelled, PolicyError, RequiredSourceUnavailable)) or (
                    isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded"
                ):
                    raise
                # Extra context is best effort: the diff itself stays available.
                _LOGGER.warning("Context file read failed blob_sha=%s", sha, exc_info=True)
                result = FileUnavailable("source_read_failed")
            else:
                if len(raw) > MAX_SOURCE_BYTES:
                    result = FileUnavailable(AstFailureReason.SOURCE_TOO_LARGE.value)
                elif _is_binary(raw):
                    # The service excludes binary content wherever it is found: a file
                    # outside the diff reached through an import is no exception, and
                    # the check runs before the parser and before the facts cache.
                    result = FileUnavailable(BINARY_SOURCE)
                else:
                    try:
                        result = raw.decode("utf-8", errors="strict")
                    except UnicodeDecodeError:
                        result = FileUnavailable(AstFailureReason.SOURCE_DECODE_FAILED.value)
        self._texts[path] = result
        return result

    async def service_text(self, path: str) -> str | FileUnavailable:
        """A file read only to resolve imports (``go.mod``); counts toward the file limit."""
        if path not in self.imported:
            if len(self.imported) >= MAX_IMPORTED_FILES:
                return FileUnavailable(AstFailureReason.AST_LIMIT.value)
            self.imported.add(path)
        return await self.text(path)

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        """Facts of a file reached through an import; counts toward the file limit."""
        if path in self.over_limit:
            return FileUnavailable(AstFailureReason.AST_LIMIT.value)
        if path not in self.changed and path not in self.imported:
            if len(self.imported) >= MAX_IMPORTED_FILES:
                return FileUnavailable(AstFailureReason.AST_LIMIT.value)
            self.imported.add(path)
        return await self.facts(path)

    async def facts(self, path: str, blob_sha: str | None = None) -> AstFacts | FileUnavailable:
        if path in self._facts:
            return self._facts[path]
        result = await self._load_facts(path, blob_sha)
        self._facts[path] = result
        return result

    async def _load_facts(self, path: str, blob_sha: str | None) -> AstFacts | FileUnavailable:
        language = language_for_path(path)
        if language is None:
            return FileUnavailable("unsupported_language")
        # The guard runs before the cache: a cached blob is not a permission to show it.
        text = await self.text(path, blob_sha)
        if isinstance(text, FileUnavailable):
            return text
        sha = blob_sha or self._tree.blob_shas.get(path)
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


class BuildReviewContext:
    """Build the context payload of one run from its diff map and policy snapshot."""

    def __init__(
        self,
        *,
        source: RevisionSource,
        parser: SourceParser,
        counter: TokenCounter,
        cache: AstFactsCache | None = None,
        builtin_excluded: Callable[[str], bool] = lambda path: False,
    ) -> None:
        self._source = source
        self._parser = parser
        self._counter = counter
        self._cache = cache
        self._builtin_excluded = builtin_excluded

    async def execute(
        self,
        *,
        diff_map: DiffMap,
        snapshot: PolicySnapshot,
        matcher: IgnoreMatcher,
        ignore: IgnoreResult,
        rules: tuple[ReviewRule, ...] = (),
        token_budget: int,
    ) -> ReviewContextPayload:
        omissions: list[ContextOmission] = []

        def denied(path: str) -> bool:
            return (
                not is_supported_path(path)
                or path in ignore.denied_paths
                or self._builtin_excluded(path)
                or matcher.match(path) is not None
            )

        allowed: list[tuple[DiffFile, str]] = []
        ignored = 0
        for file in diff_map.files:
            path = file.new_path or file.old_path or ""
            if file.omission_reason is not None:
                omissions.append(
                    ContextOmission(
                        file.omission_reason.value,
                        diagnostic_path=file.provider_new_path or file.provider_old_path,
                    )
                )
            elif ignore.decision_for(file.file_id) is not None or any(
                # The deny-set of the run already holds both names of a file that a
                # built-in exclusion withholds.
                item in ignore.denied_paths or self._builtin_excluded(item)
                for item in (file.old_path, file.new_path)
                if item
            ):
                ignored += 1
            elif not file.anchorable:
                omissions.append(ContextOmission(f"patch_{file.patch_state.value}", path))
            else:
                allowed.append((file, path))
        if ignored:
            omissions.append(ContextOmission("ignored", count=ignored))
        reviewable = sum(file.additions + file.deletions for file, _ in allowed)

        def payload(
            status: ContextStatus,
            reason: str | None,
            blocks: tuple[ContextBlock, ...] = (),
            imports: tuple[ImportRef, ...] = (),
        ) -> ReviewContextPayload:
            return ReviewContextPayload(
                status=status,
                reason=reason,
                policy_snapshot_id=snapshot.snapshot_id,
                policy_digest=snapshot.policy_digest,
                diff_digest=diff_map.digest,
                blocks=blocks,
                omissions=tuple(omissions),
                imports=imports,
                levels_present=tuple(
                    level for level in ContextLevel if any(b.level is level for b in blocks)
                ),
                reviewable_lines=reviewable,
                ignored_files=ignored,
                tokens_used=sum(block.tokens for block in blocks),
            )

        # Completeness first: an empty list the provider does not confirm is not "no changes".
        if not diff_map.file_list_complete:
            return payload(ContextStatus.SUMMARY_ONLY, "diff_inventory_incomplete")
        if not diff_map.files:
            return payload(ContextStatus.NO_CHANGES, "no_changes")
        if not allowed:
            if ignored and len(omissions) == 1:
                return payload(ContextStatus.ALL_CHANGES_IGNORED, "all_changes_ignored")
            # Nothing can be reviewed line by line, and it is not because of ignore.
            return payload(ContextStatus.SUMMARY_ONLY, "no_reviewable_files")
        if reviewable > MAX_REVIEWABLE_LINES:
            return payload(ContextStatus.SUMMARY_ONLY, "diff_too_large")

        allowed.sort(key=lambda item: (-_file_priority(item[0], item[1], rules), item[1]))
        blocks: list[ContextBlock] = []
        remaining = token_budget

        def take(block: ContextBlock) -> bool:
            nonlocal remaining
            if block.tokens > remaining:
                return False
            remaining -= block.tokens
            blocks.append(block)
            return True

        # L1: the diff itself; whole hunks first, then an explicit prefix of the last one.
        truncated = False
        for file, path in allowed:
            for hunk in file.hunks:
                if truncated:
                    omissions.append(ContextOmission("budget", path))
                    break
                block = self._hunk_block(diff_map, file, hunk, _hunk_text(hunk), cut=False)
                if block is None:
                    # Removed text of an unknown old revision has no confirmed source.
                    omissions.append(ContextOmission("old_revision_unknown", path))
                    continue
                if take(block):
                    continue
                truncated = True
                rows = self._fitting_rows(hunk, remaining)
                cut = (
                    self._hunk_block(diff_map, file, hunk, _hunk_text(hunk, rows), cut=True)
                    if rows
                    else None
                )
                if cut is None or not take(cut):
                    omissions.append(ContextOmission("budget", path))
        if truncated:
            return payload(ContextStatus.REVIEW, None, tuple(blocks))

        try:
            tree = await self._source.tree(diff_map.head_sha)
        except Exception as exc:
            if isinstance(exc, (RunCancelled, PolicyError, RequiredSourceUnavailable)) or (
                isinstance(exc, RunFailure) and exc.error_code == "deadline_exceeded"
            ):
                raise
            _LOGGER.warning(
                "Context tree read failed head_sha=%s", diff_map.head_sha, exc_info=True
            )
            omissions.append(ContextOmission("tree_unavailable"))
            return payload(ContextStatus.REVIEW, None, tuple(blocks))
        files = _Files(
            tree=tree, source=self._source, parser=self._parser, cache=self._cache, denied=denied
        )
        files.changed = {path for file, path in allowed if file.new_path is not None}
        files.over_limit = {
            path
            for position, (file, path) in enumerate(allowed)
            if position >= MAX_AST_FILES and file.new_path is not None
        }
        go_module = None
        if "go.mod" in tree.blob_shas and any(path.endswith(".go") for _, path in allowed):
            go_mod = await files.service_text("go.mod")
            go_module = None if isinstance(go_mod, FileUnavailable) else go_module_path(go_mod)
        resolver = ImportResolver(tree=tree, provider=files, go_module=go_module)

        imports: list[ImportRef] = []
        windows: dict[str, list[LineRange]] = {}
        texts: dict[str, list[str]] = {}
        try:
            for position, (file, path) in enumerate(allowed):
                if file.new_path is None:
                    continue
                content = await files.text(path, file.blob_sha)
                if isinstance(content, FileUnavailable):
                    omissions.append(ContextOmission(content.reason, path))
                    continue
                lines = content.split("\n")
                texts[path] = lines
                facts: AstFacts | FileUnavailable = FileUnavailable("unsupported_language")
                if language_for_path(path) is not None:
                    if position >= MAX_AST_FILES:
                        facts = FileUnavailable(AstFailureReason.AST_LIMIT.value)
                    else:
                        facts = await files.facts(path, file.blob_sha)
                    if isinstance(facts, FileUnavailable):
                        omissions.append(ContextOmission(facts.reason, path))
                # A file too large to show whole gets wider windows where no symbol fits.
                size = len(lines) - (1 if lines[-1] == "" else 0)
                radius = WIDE_WINDOW_LINES if size > FULL_FILE_MAX_LINES else WINDOW_LINES
                windows[path] = self._windows(file, lines, facts, radius)
                if isinstance(facts, AstFacts):
                    touched = affected_symbols(facts, file).symbols
                    imports.extend(
                        await resolver.resolve(importer_path=path, facts=facts, symbols=touched)
                    )

            # L4: signatures and doc ranges of the definitions the changed code imports.
            ast_budget = AST_CONTEXT_MAX_TOKENS
            shown: set[str] = set()
            for ref in imports:
                if ref.resolution is not ImportResolution.RESOLVED:
                    continue
                for symbol in ref.resolved_symbols:
                    if symbol.symbol_id in shown:
                        continue
                    shown.add(symbol.symbol_id)
                    excerpt = await self._symbol_block(diff_map, files, symbol)
                    if isinstance(excerpt, str):
                        omissions.append(ContextOmission(excerpt, symbol.path))
                    elif excerpt.tokens > ast_budget or not take(excerpt):
                        omissions.append(ContextOmission("ast_limit", symbol.path))
                    else:
                        ast_budget -= excerpt.tokens
        finally:
            await self._parser.aclose()

        # L2 windows, then L3: a full file replaces the windows of that file.
        only_auxiliary = all(_kind_weight(path) < 1.0 for _, path in allowed)
        for file, path in allowed:
            lines = texts.get(path, [])
            if not lines:
                continue
            count = len(lines) - (1 if lines[-1] == "" else 0)
            full_text = "\n".join(lines[:count])
            full_tokens = self._counter.count(full_text)
            eligible = (
                count <= FULL_FILE_MAX_LINES
                and full_tokens <= FULL_FILE_MAX_TOKENS
                and (only_auxiliary or _kind_weight(path) == 1.0)
            )
            if (
                eligible
                and count
                and take(
                    self._excerpt(
                        diff_map,
                        file,
                        path,
                        ContextLevel.L3,
                        LineRange(1, count),
                        full_text,
                        "full_file",
                    )
                )
            ):
                continue
            ranges = windows.get(path, [])
            for item in ranges:
                text = "\n".join(lines[item.start_line - 1 : item.end_line])
                block = self._excerpt(diff_map, file, path, ContextLevel.L2, item, text, "window")
                if not take(block):
                    omissions.append(ContextOmission("budget", path))
                    break
        return payload(ContextStatus.REVIEW, None, tuple(blocks), tuple(imports))

    def _hunk_block(
        self, diff_map: DiffMap, file: DiffFile, hunk: DiffHunk, text: str, *, cut: bool
    ) -> ContextBlock | None:
        refs = hunk_source_refs(diff_map, file, hunk)
        if not refs:
            return None
        return ContextBlock(
            block_id=f"L1:{hunk.hunk_id}",
            level=ContextLevel.L1,
            file_id=file.file_id,
            source=refs[0],
            representation=Representation.EXACT_LINES,
            text=text,
            tokens=self._counter.count(text),
            truncated=cut,
            related_sources=refs[1:],
        )

    def _fitting_rows(self, hunk: DiffHunk, budget: int) -> int:
        """The longest row prefix of a hunk that fits the remaining budget."""
        low, high = 0, len(hunk.rows)
        while low < high:
            middle = (low + high + 1) // 2
            if self._counter.count(_hunk_text(hunk, middle)) <= budget:
                low = middle
            else:
                high = middle - 1
        return low

    def _windows(
        self,
        file: DiffFile,
        lines: list[str],
        facts: AstFacts | FileUnavailable,
        radius: int = WINDOW_LINES,
    ) -> list[LineRange]:
        """Lines to show around the changes: a whole small symbol, else a line window."""
        last = len(lines) - (1 if lines and lines[-1] == "" else 0)
        if last < 1:
            return []
        ranges: list[LineRange] = []
        covered: list[LineRange] = []
        if isinstance(facts, AstFacts):
            for symbol in affected_symbols(facts, file).symbols:
                declaration = symbol.declaration_range
                size = declaration.end_line - declaration.start_line + 1
                text = "\n".join(lines[declaration.start_line - 1 : declaration.end_line])
                whole = (
                    symbol.kind in _ENCLOSING_KINDS
                    and symbol.complete
                    and size <= FRAGMENT_MAX_LINES
                    and declaration.end_line <= last
                    and self._counter.count(text) <= FRAGMENT_MAX_TOKENS
                )
                if whole:
                    covered.append(declaration)
        ranges.extend(covered)
        for hunk in file.hunks:
            first = max(hunk.new_start, 1)
            final = min(max(hunk.new_start + hunk.new_count - 1, first), last)
            inside = any(item.start_line <= first and final <= item.end_line for item in covered)
            window = None if inside else self._window(lines, first, final, radius, last)
            if window is not None:
                ranges.append(window)
        return self._merge_within_limits(ranges, lines)

    def _fits(self, lines: list[str], item: LineRange) -> bool:
        """Whether a fragment respects the per-fragment limits of lines and tokens."""
        if item.end_line - item.start_line + 1 > FRAGMENT_MAX_LINES:
            return False
        text = "\n".join(lines[item.start_line - 1 : item.end_line])
        return self._counter.count(text) <= FRAGMENT_MAX_TOKENS

    def _window(
        self, lines: list[str], first: int, final: int, radius: int, last: int
    ) -> LineRange | None:
        """The widest window around a hunk that fits a fragment, or none at all.

        The expansion is reduced until the fragment fits. A hunk that does not fit by
        itself gets no window: the diff block already shows its lines.
        """
        if first > last or not self._fits(lines, LineRange(first, final)):
            return None
        low, high = 0, radius
        while low < high:
            middle = (low + high + 1) // 2
            candidate = LineRange(max(1, first - middle), min(last, final + middle))
            if self._fits(lines, candidate):
                low = middle
            else:
                high = middle - 1
        return LineRange(max(1, first - low), min(last, final + low))

    def _merge_within_limits(self, ranges: list[LineRange], lines: list[str]) -> list[LineRange]:
        """Join touching fragments only while the result still fits one fragment."""
        merged: list[LineRange] = []
        for item in sorted(ranges, key=lambda value: (value.start_line, value.end_line)):
            if not merged or item.start_line > merged[-1].end_line + 1:
                merged.append(item)
                continue
            previous = merged[-1]
            if item.end_line <= previous.end_line:
                continue
            union = LineRange(previous.start_line, item.end_line)
            if self._fits(lines, union):
                merged[-1] = union
            else:
                # Kept apart and without a repeated line.
                merged.append(LineRange(previous.end_line + 1, item.end_line))
        return merged

    def _excerpt(
        self,
        diff_map: DiffMap,
        file: DiffFile,
        path: str,
        level: ContextLevel,
        lines: LineRange,
        text: str,
        reason: str,
    ) -> ContextBlock:
        old_path = file.old_path
        return ContextBlock(
            block_id=f"{level.value}:{file.file_id}:{lines.start_line}-{lines.end_line}",
            level=level,
            file_id=file.file_id,
            source=SourceRef(
                role=SourceRole.SOURCE_EXCERPT,
                repository_id=diff_map.repository_id,
                revision=diff_map.head_sha,
                path=path,
                blob_sha=file.blob_sha,
                origin_paths=(old_path,) if old_path is not None and old_path != path else (),
                side=DiffSide.RIGHT,
                ranges=(lines,),
                reason=reason,
            ),
            representation=Representation.EXACT_LINES,
            text=text,
            tokens=self._counter.count(text),
            truncated=False,
        )

    async def _symbol_block(
        self, diff_map: DiffMap, files: _Files, symbol: SymbolRef
    ) -> ContextBlock | str:
        text = await files.text(symbol.path)
        if isinstance(text, FileUnavailable):
            return text.reason
        lines = text.split("\n")
        ranges = _merge([*symbol.docstring_ranges, *symbol.signature_ranges])
        shown = sum(item.end_line - item.start_line + 1 for item in ranges)
        body = "\n".join(
            line for item in ranges for line in lines[item.start_line - 1 : item.end_line]
        )
        tokens = self._counter.count(body)
        if not symbol.complete or shown > FRAGMENT_MAX_LINES or tokens > FRAGMENT_MAX_TOKENS:
            return AstFailureReason.AST_LIMIT.value
        return ContextBlock(
            block_id=f"L4:{symbol.symbol_id}",
            level=ContextLevel.L4,
            file_id=None,
            source=SourceRef(
                role=SourceRole.AST_EXCERPT,
                repository_id=diff_map.repository_id,
                revision=symbol.revision,
                path=symbol.path,
                blob_sha=symbol.blob_sha,
                origin_paths=(),
                side=DiffSide.RIGHT,
                ranges=tuple(ranges),
                reason="imported_definition",
            ),
            # A signature never supports a claim about the body it does not show.
            representation=Representation.SIGNATURE,
            text=body,
            tokens=tokens,
            truncated=False,
        )
