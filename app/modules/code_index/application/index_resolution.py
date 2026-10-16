"""Import records and edges of the graph index (ADR 0001 §3, §6.3; #142).

Incremental = full: an edge depends on the path set and on the importer's own content,
not on the facts of its target, so an importer needs re-extraction only when
`affected_importers` says so. Python and TypeScript store `(target path, imported
name)` and leave the name to a join at read time; barrels are not followed. Two target
facts are read, each under its own rule: the members of a Go package (alias, build
constraints, package clause and which member declares a name; rule G), and a package
`__init__.py` that decides whether `from package import name` binds a symbol or a
submodule (rule I). A Go record points at the package directory, its edges at the member
files.

The #130 resolver of the review is reused path by path, but without its per-run caps:
every import of an indexed file gets a record, resolved or not, and every name it binds
gets its edge. One cap stays, the extractor's: it keeps the first 400 sorted
`references`, `member_references` and `local_bindings` of each symbol
(`MAX_REFERENCES_PER_SYMBOL`), so a declaration that reads more names loses the edges of
the last ones, and one that binds more locals may lose a rebinding. Full and incremental
builds read the same facts, so they agree.
"""

from __future__ import annotations

import posixpath
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field

from app.modules.code_index.application.index_store import ImportRecord, IndexedEdge
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.ast_resolution import (
    BINARY_SOURCE,
    RESOLVER_VERSION,
    FactsProvider,
    FileUnavailable,
    GoLayout,
    ImportResolution,
    ModuleTarget,
    RevisionTree,
    exported_symbols,
    go_layout,
    go_package_name,
    go_package_target,
    import_watch,
    module_target,
    python_submodule_files,
)
from app.modules.reviews.application.ast_symbols import (
    AstFacts,
    AstImport,
    AstImportName,
    AstLanguage,
    ImportForm,
    ImportKind,
    SymbolKind,
    language_for_path,
)

INDEX_RESOLVER_VERSION = f"{RESOLVER_VERSION}+index/v1"

_GO_MODULE_FILES = frozenset({"go.mod", "go.work"})


@dataclass(frozen=True)
class IndexedImport:
    """One import of a file: its record, why it is not resolved, and the edges it gave.

    An unresolved import gives no edge. `file_rows` keeps the edges with their record,
    so a record that cannot be stored takes its edges with it.
    """

    record: ImportRecord
    reason: str | None
    edges: tuple[IndexedEdge, ...]


@dataclass(frozen=True)
class _GoPackage:
    """A Go import target: its outcome and directory; when resolved, its default binding
    name, its member files and the members that declare each package-level name."""

    resolution: ImportResolution
    reason: str | None
    directory: str | None
    name: str | None = None
    members: tuple[str, ...] = ()
    declarations: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _declared_names(facts: AstFacts) -> set[str]:
    """Exported package-level names of a Go member; a method is `Recv.Name`, not `Name`."""
    return {
        symbol.name
        for symbol in facts.symbols
        if symbol.name is not None
        and symbol.enclosing is None
        and symbol.exported
        and symbol.kind is not SymbolKind.METHOD
    }


def _edges(edges: Iterable[IndexedEdge]) -> tuple[IndexedEdge, ...]:
    return tuple(
        sorted(set(edges), key=lambda edge: (edge.kind, edge.target_path, edge.target_name))
    )


def _uses(facts: AstFacts, alias: str) -> bool:
    """`alias` is referenced by a declaration that does not rebind it.

    Both name sets are the extractor's first 400 sorted names of the symbol
    (`MAX_REFERENCES_PER_SYMBOL`), not all of them.
    """
    return any(
        alias in symbol.references and alias not in symbol.local_bindings
        for symbol in facts.symbols
    )


def _members(facts: AstFacts, alias: str) -> list[str]:
    """The names read as `alias.name` by declarations that do not rebind `alias`.

    Up to the extractor's first 400 sorted member references and local bindings of each
    symbol (`MAX_REFERENCES_PER_SYMBOL`).
    """
    return sorted(
        {
            name
            for symbol in facts.symbols
            if alias not in symbol.local_bindings
            for owner, name in symbol.member_references
            if owner == alias
        }
    )


class IndexImportResolver:
    """Records and edges of the imports of indexed files at one commit.

    `tree` lists every path of the archive; its blob ids are not read. `path_exclusion`
    names the exclusion class of a path (ADR 0001 §7, classes 1-7), and such a target is
    `excluded` and never stored. `facts` gives the facts of a Go package member or of a
    package `__init__.py`. One resolver serves one build: Go packages are memoized, and
    the Go layout of `tree` is read once, at the first Go import.
    """

    def __init__(
        self,
        *,
        tree: RevisionTree,
        facts: FactsProvider,
        path_exclusion: Callable[[str], str | None],
        go_module: str | None,
    ) -> None:
        self._tree = tree
        self._facts = facts
        self._path_exclusion = path_exclusion
        self._go_module = go_module
        self._go_packages: dict[str, _GoPackage] = {}
        self._go_layout: GoLayout | None = None

    async def resolve_file(self, path: str, facts: AstFacts) -> tuple[IndexedImport, ...]:
        return tuple([await self.resolve_import(path, facts, item) for item in facts.imports])

    async def resolve_import(self, path: str, facts: AstFacts, item: AstImport) -> IndexedImport:
        """The record and edges of one import specifier of the file at `path`."""

        def unresolved(
            resolution: ImportResolution, reason: str | None, target: str | None = None
        ) -> IndexedImport:
            return IndexedImport(ImportRecord(path, item.module, resolution, target), reason, ())

        if item.form is ImportForm.DYNAMIC:
            return unresolved(ImportResolution.DYNAMIC, "dynamic_import")
        if item.form is not ImportForm.STATIC:
            return unresolved(ImportResolution.UNSUPPORTED, item.form.value)
        if facts.language is AstLanguage.GO:
            package = await self._go_package(item.module)
            if package.resolution is not ImportResolution.RESOLVED or package.directory is None:
                return unresolved(package.resolution, package.reason, package.directory)
            edges = [IndexedEdge(path, EdgeKind.IMPORTS, member, "") for member in package.members]
            for binding in item.names:
                alias = binding.alias or package.name
                if binding.kind is not ImportKind.MODULE or alias is None:
                    continue
                for name in _members(facts, alias):
                    # A name declared by no member or by several gets no edge.
                    declaring = package.declarations.get(name, ())
                    if len(declaring) == 1:
                        edges.append(IndexedEdge(path, EdgeKind.USES_IMPORTED, declaring[0], name))
            return IndexedImport(
                ImportRecord(path, item.module, ImportResolution.RESOLVED, package.directory),
                None,
                _edges(edges),
            )
        target = module_target(path, item.module, facts.language, self._tree, self._go_module)
        if target.resolution is not ImportResolution.RESOLVED:
            return unresolved(target.resolution, target.reason)
        target_path = target.files[0]
        excluded = self._path_exclusion(target_path)
        if excluded is not None:
            return unresolved(ImportResolution.EXCLUDED, excluded)
        edges = [IndexedEdge(path, EdgeKind.IMPORTS, target_path, "")]
        for binding in item.names:
            edges += await self._binding_edges(path, facts, item, binding, target_path)
        return IndexedImport(
            ImportRecord(path, item.module, ImportResolution.RESOLVED, target_path),
            None,
            _edges(edges),
        )

    async def _binding_edges(
        self,
        path: str,
        facts: AstFacts,
        item: AstImport,
        binding: AstImportName,
        target_path: str,
    ) -> list[IndexedEdge]:
        """The edges of one name a resolved Python or script import binds."""
        alias = binding.alias
        if alias is None or binding.kind in {ImportKind.WILDCARD, ImportKind.SIDE_EFFECT}:
            return []
        python = facts.language is AstLanguage.PYTHON
        if binding.kind in {ImportKind.NAMED, ImportKind.DEFAULT}:
            if binding.name is None:
                return []
            if python and binding.kind is ImportKind.NAMED and target_path.endswith("__init__.py"):
                return await self._package_binding(path, facts, target_path, binding.name, alias)
            if not _uses(facts, alias):
                return []
            return [IndexedEdge(path, EdgeKind.USES_IMPORTED, target_path, binding.name)]
        dotted = python and "." in item.module.strip(".") and alias == item.module.split(".")[0]
        if dotted:
            # `import a.b` binds `a`: `a.b.c` is not a member reference of `a.b`.
            return []
        return [
            IndexedEdge(path, EdgeKind.USES_IMPORTED, target_path, name)
            for name in _members(facts, alias)
        ]

    async def _package_binding(
        self, path: str, facts: AstFacts, package_init: str, name: str, alias: str
    ) -> list[IndexedEdge]:
        """`from package import name`: a symbol of `__init__.py`, else the submodule."""
        submodule = python_submodule_files(package_init, name, self._tree)
        symbol = [IndexedEdge(path, EdgeKind.USES_IMPORTED, package_init, name)]
        if len(submodule) != 1:
            return symbol if _uses(facts, alias) else []
        init = await self._facts.facts_for(package_init)
        if isinstance(init, FileUnavailable):
            # A symbol cannot be told from the submodule: neither is guessed.
            return []
        if exported_symbols(init, name):
            return symbol if _uses(facts, alias) else []
        module = submodule[0]
        if self._path_exclusion(module) is not None:
            return []
        return [
            IndexedEdge(path, EdgeKind.IMPORTS, module, ""),
            *(
                IndexedEdge(path, EdgeKind.USES_IMPORTED, module, member)
                for member in _members(facts, alias)
            ),
        ]

    async def _go_package(self, module: str) -> _GoPackage:
        if module not in self._go_packages:
            if self._go_layout is None:
                self._go_layout = go_layout(self._tree)
            target = go_package_target(module, self._tree, self._go_module, layout=self._go_layout)
            self._go_packages[module] = await self._go_members(module, target)
        return self._go_packages[module]

    async def _go_members(self, module: str, target: ModuleTarget) -> _GoPackage:
        if target.resolution is not ImportResolution.RESOLVED:
            return _GoPackage(target.resolution, target.reason, None, None)
        for member in target.files:
            excluded = self._path_exclusion(member)
            if excluded is not None:
                return _GoPackage(ImportResolution.EXCLUDED, excluded, None, None)
        directory = posixpath.dirname(target.files[0])
        loaded: list[AstFacts] = []
        for member in target.files:
            facts = await self._facts.facts_for(member)
            if isinstance(facts, FileUnavailable):
                # As in the review: one unusable member leaves the whole package unresolved.
                excluded_content = facts.reason in {"excluded", BINARY_SOURCE}
                resolution = (
                    ImportResolution.EXCLUDED if excluded_content else ImportResolution.UNSUPPORTED
                )
                return _GoPackage(resolution, facts.reason, directory, None)
            loaded.append(facts)
        if any(facts.build_constrained for facts in loaded):
            return _GoPackage(ImportResolution.UNSUPPORTED, "build_constrained", directory, None)
        if len({facts.package_name for facts in loaded}) > 1:
            return _GoPackage(ImportResolution.AMBIGUOUS, "several_package_names", directory, None)
        declarations: defaultdict[str, list[str]] = defaultdict(list)
        for member, facts in zip(target.files, loaded, strict=True):
            for name in _declared_names(facts):
                declarations[name].append(member)
        return _GoPackage(
            ImportResolution.RESOLVED,
            None,
            directory,
            go_package_name(module, loaded),
            target.files,
            {name: tuple(files) for name, files in declarations.items()},
        )


def affected_importers(
    records: Iterable[ImportRecord],
    *,
    delta: Iterable[str],
    changed: Iterable[str],
    go_module: str | None,
    limit: int,
) -> frozenset[str] | None:
    """The importers an incremental build re-extracts, or `None` for a full build.

    `records` are every visible import record, unresolved ones included. `delta` holds
    the changes of the path set: added and removed paths, both paths of a rename, copies,
    and paths whose entry type changed (file to symlink and back). `changed` is the
    change set C. An importer is affected when:

    - W: a path of `delta` touches the `import_watch` of one of its records;
    - I: a changed `__init__.py` or `index.*` is the target of one of its records;
    - G: a changed `.go` file sits in the target directory of one of its Go records.

    F: a `go.mod` or `go.work` anywhere in the changes, or more than `limit` paths in
    C and the affected importers together, makes the build full.
    """
    delta_paths = frozenset(delta)
    everything = delta_paths | frozenset(changed)
    if any(posixpath.basename(path) in _GO_MODULE_FILES for path in everything):
        return None
    # Each delta path under the keys a watch can match it by: itself, its directory
    # (a child module) and the directory above (a child package's `__init__.py`).
    near: defaultdict[str, list[str]] = defaultdict(list)
    for path in delta_paths:
        parent = posixpath.dirname(path)
        for key in {path, parent, posixpath.dirname(parent)}:
            near[key].append(path)
    changed_go_directories = {
        posixpath.dirname(path) for path in everything if path.endswith(".go")
    }
    affected: set[str] = set()
    for record in records:
        if record.path in affected:
            continue
        language = language_for_path(record.path)
        if language is None:
            continue
        target = record.target_path
        if target is not None and target in everything:
            name = posixpath.basename(target)
            if name == "__init__.py" or name.startswith("index."):
                affected.add(record.path)
                continue
        if language is AstLanguage.GO and target in changed_go_directories:
            affected.add(record.path)
            continue
        watch = import_watch(record.path, record.specifier, language, go_module)
        candidates = (path for key in watch.paths | watch.packages for path in near.get(key, ()))
        if any(watch.touched_by(path) for path in candidates):
            affected.add(record.path)
    if len(everything | affected) > limit:
        return None
    return frozenset(affected)
