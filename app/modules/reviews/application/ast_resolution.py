"""Affected symbols and one-step import resolution (CONTEXT_AND_VERIFICATION_SPEC §3.3).

Resolver v1 follows one hop from a changed file and never guesses: anything it cannot
tie to exactly one declaration keeps a reason instead of a target. No dependency is
downloaded, no project configuration is executed, no package manager is run.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstImport,
    AstImportName,
    AstLanguage,
    AstSymbol,
    ImportForm,
    ImportKind,
    SymbolKind,
    language_for_path,
)
from app.modules.reviews.application.context_provenance import LineRange
from app.modules.reviews.application.diff_map import DiffFile, RowKind

RESOLVER_VERSION = "resolver/v1"
MAX_IMPORTED_FILES = 20
# Why a file is unavailable when its content is binary: an exclusion, not a failure.
BINARY_SOURCE = "binary"
MAX_IMPORTED_SYMBOLS_PER_FILE = 10
MAX_IMPORTED_SYMBOLS_PER_RUN = 100

_PYTHON_ROOTS = ("", "src/")
_TS_EXTENSIONS = (".ts", ".tsx", ".d.ts")
_JS_EXTENSIONS = (".js", ".jsx", ".mjs")
_ALIAS_PREFIXES = ("@/", "~/", "#")
_GO_MODULE = re.compile(r"^module\s+(\S+)\s*$", re.MULTILINE)
_GO_REPLACE = re.compile(r"^\s*replace\b", re.MULTILINE)
_GO_PLATFORMS = frozenset(
    {
        *("aix", "android", "darwin", "dragonfly", "freebsd", "illumos", "ios", "js"),
        *("linux", "netbsd", "openbsd", "plan9", "solaris", "wasip1", "windows"),
        *("386", "amd64", "arm", "arm64", "loong64", "mips", "mips64", "mips64le"),
        *("mipsle", "ppc64", "ppc64le", "riscv64", "s390x", "wasm"),
    }
)


class ImportResolution(StrEnum):
    RESOLVED = "resolved"
    EXTERNAL = "external"
    AMBIGUOUS = "ambiguous"
    DYNAMIC = "dynamic"
    EXCLUDED = "excluded"
    UNSUPPORTED = "unsupported"
    MISSING = "missing"


@dataclass(frozen=True)
class RevisionTree:
    """File paths of one revision; an incomplete listing proves nothing about absence."""

    revision: str
    blob_shas: Mapping[str, str]
    complete: bool


@dataclass(frozen=True)
class SymbolRef:
    symbol_id: str
    language: AstLanguage
    kind: SymbolKind
    name: str | None
    path: str
    revision: str
    blob_sha: str | None
    declaration_range: LineRange
    signature_ranges: tuple[LineRange, ...]
    docstring_ranges: tuple[LineRange, ...]
    enclosing_symbol_id: str | None
    declaration_kind: str | None
    exported: bool
    complete: bool
    parser_profile_version: str


@dataclass(frozen=True)
class ImportRef:
    importer_symbol_id: str
    module: str
    names: tuple[str, ...]
    resolved_symbols: tuple[SymbolRef, ...]
    resolution: ImportResolution
    reason: str | None
    resolution_profile_version: str = RESOLVER_VERSION


@dataclass(frozen=True)
class FileUnavailable:
    """Why the facts of a file cannot be used: ``excluded``, ``ast_limit`` or a failure."""

    reason: str


class FactsProvider(Protocol):
    """Facts of a file of the revision; applies the ignore policy and the file limits."""

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable: ...


def symbol_ref(
    facts: AstFacts, symbol: AstSymbol, *, path: str, revision: str, blob_sha: str | None
) -> SymbolRef:
    prefix = f"{revision[:12]}:{path}#"
    return SymbolRef(
        symbol_id=f"{prefix}{symbol.index}",
        language=facts.language,
        kind=symbol.kind,
        name=symbol.name,
        path=path,
        revision=revision,
        blob_sha=blob_sha,
        declaration_range=symbol.declaration_range,
        signature_ranges=symbol.signature_ranges,
        docstring_ranges=symbol.docstring_ranges,
        enclosing_symbol_id=None if symbol.enclosing is None else f"{prefix}{symbol.enclosing}",
        declaration_kind=symbol.declaration_kind,
        exported=symbol.exported,
        complete=symbol.complete,
        parser_profile_version=facts.profile_version,
    )


@dataclass(frozen=True)
class AffectedSymbols:
    """Innermost declarations touched by a file's new-side changes."""

    symbols: tuple[AstSymbol, ...]
    module_level: bool


def affected_symbols(facts: AstFacts, file: DiffFile) -> AffectedSymbols:
    """Symbols that intersect added lines; a change between symbols is module-level."""
    found: dict[int, AstSymbol] = {}
    module_level = False

    def innermost(first: int, last: int) -> AstSymbol | None:
        holders = [
            item
            for item in facts.symbols
            if item.declaration_range.start_line <= first
            and last <= item.declaration_range.end_line
        ]
        return max(holders, key=lambda item: item.declaration_range.start_line, default=None)

    for hunk in file.hunks:
        added = [row.new_line for row in hunk.rows if row.kind is RowKind.ADDED and row.new_line]
        if added:
            spans = [(line, line) for line in added]
        elif hunk.new_count:
            # Lines were only removed inside the hunk: its surviving lines locate the edit.
            spans = [(hunk.new_start, hunk.new_start + hunk.new_count - 1)]
        else:
            # Everything of the hunk was removed between two surviving lines.
            spans = [(max(hunk.new_start, 1), hunk.new_start + 1)]
        for first, last in spans:
            symbol = innermost(first, last)
            if symbol is None:
                module_level = True
            else:
                found[symbol.index] = symbol
    return AffectedSymbols(tuple(found[index] for index in sorted(found)), module_level)


@dataclass(frozen=True)
class ModuleTarget:
    """Files an import depends on, or the reason it has no target."""

    resolution: ImportResolution
    reason: str | None = None
    files: tuple[str, ...] = ()


def _unique(candidates: list[str], *, missing: ImportResolution) -> ModuleTarget:
    if not candidates:
        return ModuleTarget(missing, "no_candidate")
    if len(candidates) > 1:
        return ModuleTarget(ImportResolution.AMBIGUOUS, "several_candidates")
    return ModuleTarget(ImportResolution.RESOLVED, files=(candidates[0],))


def _python_module_files(module_path: str, tree: RevisionTree) -> list[str]:
    """Runtime candidates of a module path; a stub next to its runtime file is not a rival."""
    files = [
        candidate
        for candidate in (f"{module_path}.py", f"{module_path}/__init__.py")
        if candidate in tree.blob_shas
    ]
    stub = f"{module_path}.pyi"
    if not files and stub in tree.blob_shas:
        files.append(stub)
    return files


def python_submodule_files(package_init: str, name: str, tree: RevisionTree) -> list[str]:
    """Runtime candidates of the submodule ``name`` of the package of ``package_init``."""
    package = posixpath.dirname(package_init)
    inner = "/".join(part for part in (package, name) if part)
    return _python_module_files(inner, tree)


def _python_relative_path(importer: str, module: str) -> str | None:
    """The module path of a relative ``module``; ``None`` above the repository root."""
    dots = len(module) - len(module.lstrip("."))
    base = posixpath.dirname(importer)
    for _ in range(dots - 1):
        if not base:
            return None
        base = posixpath.dirname(base)
    parts = [part for part in module[dots:].split(".") if part]
    return "/".join([base, *parts] if base else parts)


def python_module_target(importer: str, module: str, tree: RevisionTree) -> ModuleTarget:
    if module.startswith("."):
        module_path = _python_relative_path(importer, module)
        if module_path is None:
            return ModuleTarget(ImportResolution.MISSING, "above_repository_root")
        if not module_path:
            init = "__init__.py"
            return _unique(
                [init] if init in tree.blob_shas else [], missing=ImportResolution.MISSING
            )
        return _unique(_python_module_files(module_path, tree), missing=ImportResolution.MISSING)
    relative = module.replace(".", "/")
    candidates = [
        file for root in _PYTHON_ROOTS for file in _python_module_files(root + relative, tree)
    ]
    return _unique(candidates, missing=ImportResolution.EXTERNAL)


def _script_candidates(
    importer: str, module: str, *, typescript: bool
) -> ModuleTarget | tuple[str, ...]:
    """The candidate files of a script import, or its outcome when no path can decide it."""
    if not module.startswith(("./", "../")):
        if module.startswith(_ALIAS_PREFIXES) or module.startswith("/"):
            return ModuleTarget(ImportResolution.UNSUPPORTED, "path_alias")
        return ModuleTarget(ImportResolution.EXTERNAL, "package")
    target = posixpath.normpath(posixpath.join(posixpath.dirname(importer), module))
    if target.startswith("../") or target == "..":
        return ModuleTarget(ImportResolution.MISSING, "above_repository_root")
    extensions = _TS_EXTENSIONS if typescript else _JS_EXTENSIONS
    name = posixpath.basename(target)
    if "." in name:
        if target.endswith(extensions):
            return (target,)
        # A JS extension in TypeScript may stand for a TS source; other kinds are not code.
        return ModuleTarget(ImportResolution.UNSUPPORTED, "extension_not_resolved")
    candidates = tuple(target + extension for extension in extensions)
    return candidates + tuple(
        f"{target}/index{extension}" for extension in extensions if extension != ".d.ts"
    )


def script_module_target(
    importer: str, module: str, tree: RevisionTree, *, typescript: bool
) -> ModuleTarget:
    candidates = _script_candidates(importer, module, typescript=typescript)
    if isinstance(candidates, ModuleTarget):
        return candidates
    return _unique(
        [item for item in candidates if item in tree.blob_shas], missing=ImportResolution.MISSING
    )


def _go_platform_file(path: str) -> bool:
    parts = posixpath.basename(path).removesuffix(".go").split("_")
    return any(part in _GO_PLATFORMS for part in parts[1:])


def go_module_path(go_mod: str) -> str | None:
    """The module path of a root ``go.mod``; ``None`` when ``replace`` changes resolution."""
    if _GO_REPLACE.search(go_mod):
        return None
    match = _GO_MODULE.search(go_mod)
    return None if match is None else match.group(1).strip('"')


def go_package_target(module: str, tree: RevisionTree, go_module: str | None) -> ModuleTarget:
    if "." not in module.split("/")[0]:
        return ModuleTarget(ImportResolution.EXTERNAL, "standard_library")
    if "go.work" in tree.blob_shas or not tree.complete:
        return ModuleTarget(ImportResolution.UNSUPPORTED, "workspace_or_incomplete_tree")
    if go_module is None:
        return ModuleTarget(ImportResolution.UNSUPPORTED, "module_not_resolved")
    if module != go_module and not module.startswith(go_module + "/"):
        return ModuleTarget(ImportResolution.EXTERNAL, "other_module")
    directory = module[len(go_module) :].strip("/")
    prefix = f"{directory}/" if directory else ""
    nested = [
        path
        for path in tree.blob_shas
        if path.endswith("/go.mod") and prefix.startswith(path.removesuffix("go.mod"))
    ]
    if nested:
        return ModuleTarget(ImportResolution.UNSUPPORTED, "nested_module")
    members = sorted(
        path
        for path in tree.blob_shas
        if path.startswith(prefix)
        and "/" not in path[len(prefix) :]
        and path.endswith(".go")
        and not path.endswith("_test.go")
        and not posixpath.basename(path).startswith(("_", "."))
    )
    if not members:
        return ModuleTarget(ImportResolution.MISSING, "no_package_files")
    if any(_go_platform_file(path) for path in members):
        return ModuleTarget(ImportResolution.UNSUPPORTED, "build_constrained")
    return ModuleTarget(ImportResolution.RESOLVED, files=tuple(members))


def module_target(
    importer: str,
    module: str,
    language: AstLanguage,
    tree: RevisionTree,
    go_module: str | None,
) -> ModuleTarget:
    """Where ``module`` imported by ``importer`` resolves in ``tree``, by path only."""
    if language is AstLanguage.PYTHON:
        return python_module_target(importer, module, tree)
    if language is AstLanguage.GO:
        return go_package_target(module, tree, go_module)
    return script_module_target(
        importer,
        module,
        tree,
        typescript=language in {AstLanguage.TYPESCRIPT, AstLanguage.TSX},
    )


def go_package_name(module: str, members: Iterable[AstFacts] | None) -> str | None:
    """The default binding of a Go import: the package clause of its members.

    Without readable members, or when their clauses differ, the path tail stands in, the
    usual name, only to report why the import is not followed.
    """
    tail = module.rsplit("/", 1)[-1]
    if members is None:
        return tail
    names = {facts.package_name for facts in members}
    return next(iter(names)) if len(names) == 1 else tail


_CHILD_MODULE_SUFFIXES = (".py", ".pyi", ".go")
_NO_PATHS: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ImportWatch:
    """The paths whose presence decides where an import resolves (#142, rule W).

    ``paths`` are the candidate files of the target; a child module added to or removed
    from one of ``packages`` (``<package>/<name>.py``, ``.pyi`` or ``.go``, or
    ``<package>/<name>/__init__.py``) can change the outcome too. Contents, ``go.mod``
    and ``go.work`` are outside the watch: the indexer has separate rules for them.
    """

    paths: frozenset[str]
    packages: frozenset[str]

    def touched_by(self, path: str) -> bool:
        if path in self.paths:
            return True
        parent = posixpath.dirname(path)
        if path.endswith(_CHILD_MODULE_SUFFIXES) and parent in self.packages:
            return True
        return posixpath.basename(path) == "__init__.py" and (
            posixpath.dirname(parent) in self.packages
        )


def _python_watch(importer: str, module: str) -> ImportWatch:
    if module.startswith("."):
        module_path = _python_relative_path(importer, module)
        if module_path is None:
            return ImportWatch(_NO_PATHS, _NO_PATHS)
        if not module_path:
            return ImportWatch(frozenset({"__init__.py"}), frozenset({""}))
        bases: tuple[str, ...] = (module_path,)
    else:
        relative = module.replace(".", "/")
        bases = tuple(root + relative for root in _PYTHON_ROOTS)
    paths = frozenset(
        path for base in bases for path in (f"{base}.py", f"{base}/__init__.py", f"{base}.pyi")
    )
    return ImportWatch(paths, frozenset(bases))


def _go_watch(module: str, go_module: str | None) -> ImportWatch:
    if go_module is None or "." not in module.split("/")[0]:
        return ImportWatch(_NO_PATHS, _NO_PATHS)
    if module != go_module and not module.startswith(go_module + "/"):
        return ImportWatch(_NO_PATHS, _NO_PATHS)
    return ImportWatch(_NO_PATHS, frozenset({module[len(go_module) :].strip("/")}))


def import_watch(
    importer: str, module: str, language: AstLanguage, go_module: str | None
) -> ImportWatch:
    """What ``module_target`` of ``module`` from ``importer`` reads of the path set."""
    if language is AstLanguage.PYTHON:
        return _python_watch(importer, module)
    if language is AstLanguage.GO:
        return _go_watch(module, go_module)
    candidates = _script_candidates(
        importer, module, typescript=language in {AstLanguage.TYPESCRIPT, AstLanguage.TSX}
    )
    if isinstance(candidates, ModuleTarget):
        return ImportWatch(_NO_PATHS, _NO_PATHS)
    return ImportWatch(frozenset(candidates), _NO_PATHS)


def exported_symbols(facts: AstFacts, name: str) -> tuple[AstSymbol, ...]:
    if facts.language in {AstLanguage.PYTHON, AstLanguage.GO}:
        return tuple(item for item in facts.symbol_named(name) if item.exported)
    index = dict(facts.exports).get(name)
    return () if index is None else (facts.symbols[index],)


_Outcome = tuple[ImportResolution, str | None, tuple[SymbolRef, ...]]
_Loaded = list[tuple[str, AstFacts]]


class ImportResolver:
    """Resolve the imports a changed symbol really uses, within the limits of a run."""

    def __init__(
        self, *, tree: RevisionTree, provider: FactsProvider, go_module: str | None = None
    ) -> None:
        self._tree = tree
        self._provider = provider
        self._go_module = go_module
        self._symbols_in_run = 0

    async def resolve(
        self, *, importer_path: str, facts: AstFacts, symbols: Iterable[AstSymbol]
    ) -> tuple[ImportRef, ...]:
        refs: list[ImportRef] = []
        seen: set[tuple[str, str, tuple[str, ...]]] = set()
        symbols_in_file = 0
        for symbol in symbols:
            importer_id = f"{self._tree.revision[:12]}:{importer_path}#{symbol.index}"
            for item in facts.imports:
                for binding in item.names:
                    alias = binding.alias
                    if alias is None and facts.language is AstLanguage.GO:
                        alias = await self._go_package_name(item, binding)
                    if alias is None:
                        continue
                    members = tuple(
                        sorted({name for owner, name in symbol.member_references if owner == alias})
                    )
                    named = binding.kind in {ImportKind.NAMED, ImportKind.DEFAULT}
                    if not (alias in symbol.references if named else members):
                        continue
                    wanted = (binding.name,) if named and binding.name is not None else members
                    key = (item.module, alias, wanted)
                    if key in seen:
                        continue
                    seen.add(key)
                    over = (
                        symbols_in_file >= MAX_IMPORTED_SYMBOLS_PER_FILE
                        or self._symbols_in_run >= MAX_IMPORTED_SYMBOLS_PER_RUN
                    )
                    outcome: _Outcome
                    if over:
                        outcome = (ImportResolution.UNSUPPORTED, "ast_limit", ())
                    elif alias in symbol.local_bindings:
                        outcome = (ImportResolution.AMBIGUOUS, "shadowed", ())
                    else:
                        outcome = await self._resolve(
                            importer_path, facts.language, item, binding, wanted, members
                        )
                    resolution, reason, found = outcome
                    # A Python submodule binding is resolved through the members taken from it.
                    through_members = (
                        facts.language is AstLanguage.PYTHON
                        and binding.kind is ImportKind.NAMED
                        and bool(found)
                        and found[0].name != binding.name
                    )
                    names = members if through_members else wanted
                    # One import can bring several definitions: only those that fit both
                    # remaining limits are shown, the rest are reported, not presented.
                    room = min(
                        MAX_IMPORTED_SYMBOLS_PER_FILE - symbols_in_file,
                        MAX_IMPORTED_SYMBOLS_PER_RUN - self._symbols_in_run,
                    )
                    kept, dropped = found[:room], found[room:]
                    symbols_in_file += len(kept)
                    self._symbols_in_run += len(kept)
                    if kept or not found:
                        shown = names[: len(kept)] if dropped else names
                        refs.append(
                            ImportRef(importer_id, item.module, shown, kept, resolution, reason)
                        )
                    if dropped:
                        refs.append(
                            ImportRef(
                                importer_id,
                                item.module,
                                names[len(kept) :],
                                (),
                                ImportResolution.UNSUPPORTED,
                                "ast_limit",
                            )
                        )
        return tuple(refs)

    async def _go_package_name(self, item: AstImport, binding: AstImportName) -> str | None:
        """The default binding of a Go import is the package clause of its target."""
        if binding.kind is not ImportKind.MODULE:
            return None
        target = go_package_target(item.module, self._tree, self._go_module)
        if target.resolution is not ImportResolution.RESOLVED:
            return go_package_name(item.module, None)
        loaded = await self._load(list(target.files))
        if not isinstance(loaded, list):
            return go_package_name(item.module, None)
        return go_package_name(item.module, (facts for _, facts in loaded))

    async def _resolve(
        self,
        importer: str,
        language: AstLanguage,
        item: AstImport,
        binding: AstImportName,
        wanted: tuple[str, ...],
        members: tuple[str, ...],
    ) -> _Outcome:
        if item.form is ImportForm.DYNAMIC:
            return ImportResolution.DYNAMIC, "dynamic_import", ()
        if item.form is not ImportForm.STATIC:
            return ImportResolution.UNSUPPORTED, item.form.value, ()
        if binding.kind in {ImportKind.WILDCARD, ImportKind.SIDE_EFFECT} or not wanted:
            return ImportResolution.UNSUPPORTED, "no_named_symbol", ()
        if language is AstLanguage.PYTHON:
            dotted_package = binding.kind is ImportKind.MODULE and "." in item.module.strip(".")
            if dotted_package and binding.alias == item.module.split(".")[0]:
                return ImportResolution.UNSUPPORTED, "dotted_module_access", ()
        target = module_target(importer, item.module, language, self._tree, self._go_module)
        if target.resolution is not ImportResolution.RESOLVED:
            return target.resolution, target.reason, ()
        if language_for_path(target.files[0]) is None:
            return ImportResolution.UNSUPPORTED, "not_a_source_file", ()
        outcome = await self._lookup(list(target.files), wanted)
        package_init = target.files[0].endswith("__init__.py")
        if (
            outcome[0] is ImportResolution.MISSING
            and language is AstLanguage.PYTHON
            and binding.kind is ImportKind.NAMED
            and binding.name is not None
            and package_init
        ):
            # ``from package import name`` can bind a submodule instead of a symbol.
            submodule = python_submodule_files(target.files[0], binding.name, self._tree)
            if len(submodule) == 1:
                if not members:
                    return ImportResolution.UNSUPPORTED, "submodule_binding", ()
                return await self._lookup(submodule, members)
        return outcome

    async def _load(self, files: list[str]) -> _Loaded | FileUnavailable:
        loaded: _Loaded = []
        for path in files:
            facts = await self._provider.facts_for(path)
            if isinstance(facts, FileUnavailable):
                # One unusable member leaves the whole package unresolved: uniqueness of
                # a name cannot be shown on the remaining subset.
                return facts
            loaded.append((path, facts))
        return loaded

    async def _lookup(self, files: list[str], wanted: tuple[str, ...]) -> _Outcome:
        loaded = await self._load(files)
        if isinstance(loaded, FileUnavailable):
            excluded = loaded.reason in {"excluded", BINARY_SOURCE}
            return (
                ImportResolution.EXCLUDED if excluded else ImportResolution.UNSUPPORTED,
                loaded.reason,
                (),
            )
        if loaded and loaded[0][1].language is AstLanguage.GO:
            if any(facts.build_constrained for _, facts in loaded):
                return ImportResolution.UNSUPPORTED, "build_constrained", ()
            if len({facts.package_name for _, facts in loaded}) > 1:
                return ImportResolution.AMBIGUOUS, "several_package_names", ()
        found: list[SymbolRef] = []
        for name in wanted:
            matches = [
                (path, facts, symbol)
                for path, facts in loaded
                for symbol in exported_symbols(facts, name)
            ]
            if len(matches) > 1:
                return ImportResolution.AMBIGUOUS, "several_declarations", ()
            if not matches:
                reexports = any(
                    item.form is ImportForm.REEXPORT
                    for _, facts in loaded
                    for item in facts.imports
                )
                if reexports:
                    return ImportResolution.UNSUPPORTED, "reexport_chain", ()
                return ImportResolution.MISSING, "name_not_declared", ()
            path, facts, symbol = matches[0]
            found.append(
                symbol_ref(
                    facts,
                    symbol,
                    path=path,
                    revision=self._tree.revision,
                    blob_sha=self._tree.blob_shas.get(path),
                )
            )
        return ImportResolution.RESOLVED, None, tuple(found)
