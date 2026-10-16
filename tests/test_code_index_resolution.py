"""Import records and edges of the graph index: incremental = full (ADR 0001 §3; #142).

AC11: every branch of the #130 resolution table gives its record, and only resolved
imports give the ADR edge kinds. AC5: `affected_importers` covers the rules W, I, G and F.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from app.modules.code_index.application.index_resolution import (
    INDEX_RESOLVER_VERSION,
    IndexedImport,
    IndexImportResolver,
    affected_importers,
)
from app.modules.code_index.application.index_store import ImportRecord, IndexedEdge
from app.modules.code_index.domain.enums import EdgeKind
from app.modules.reviews.application.ast_resolution import (
    FileUnavailable,
    ImportResolution,
    RevisionTree,
)
from app.modules.reviews.application.ast_symbols import AstFacts, language_for_path
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_HEAD = "a" * 40
_MODULE = "example.com/m"
RESOLVED = ImportResolution.RESOLVED
EXTERNAL = ImportResolution.EXTERNAL
AMBIGUOUS = ImportResolution.AMBIGUOUS
DYNAMIC = ImportResolution.DYNAMIC
EXCLUDED = ImportResolution.EXCLUDED
UNSUPPORTED = ImportResolution.UNSUPPORTED
MISSING = ImportResolution.MISSING


def _facts(path: str, source: str) -> AstFacts:
    language = language_for_path(path)
    assert language is not None
    return extract_facts(language, source.encode(), cpu_seconds=5.0)


@dataclass
class Provider:
    """Facts of in-memory sources; a path missing from `sources` must never be read."""

    sources: Mapping[str, str]
    unavailable: Mapping[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        self.asked.append(path)
        if path in self.unavailable:
            return FileUnavailable(self.unavailable[path])
        return _facts(path, self.sources[path])


def _resolver(
    provider: Provider,
    paths: Iterable[str],
    *,
    excluded: Mapping[str, str] | None = None,
    go_module: str | None = _MODULE,
) -> IndexImportResolver:
    tree = RevisionTree(_HEAD, dict.fromkeys(paths, "b" * 40), True)
    return IndexImportResolver(
        tree=tree,
        facts=provider,
        path_exclusion=dict(excluded or {}).get,
        go_module=go_module,
    )


def _resolve(
    importer: str,
    source: str,
    *,
    paths: Iterable[str] = (),
    sources: Mapping[str, str] | None = None,
    unavailable: Mapping[str, str] | None = None,
    excluded: Mapping[str, str] | None = None,
    go_module: str | None = _MODULE,
) -> tuple[tuple[IndexedImport, ...], Provider]:
    provider = Provider(dict(sources or {}), dict(unavailable or {}))
    tree = {importer, *paths, *provider.sources, *provider.unavailable}
    resolver = _resolver(provider, tree, excluded=excluded, go_module=go_module)
    imports = asyncio.run(resolver.resolve_file(importer, _facts(importer, source)))
    for item in imports:
        # AC11: only a resolved import gives edges, and only of the ADR kinds.
        assert item.edges == () or item.record.resolution is RESOLVED, item
        assert {edge.kind for edge in item.edges} <= {EdgeKind.IMPORTS, EdgeKind.USES_IMPORTED}
    return imports, provider


def _one(importer: str, source: str, **options: object) -> IndexedImport:
    imports, _ = _resolve(importer, source, **options)  # type: ignore[arg-type]
    assert len(imports) == 1, imports
    return imports[0]


def _record(
    path: str, specifier: str, resolution: ImportResolution, target: str | None = None
) -> ImportRecord:
    return ImportRecord(path, specifier, resolution, target)


def _imports(path: str, target: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.IMPORTS, target, "")


def _uses(path: str, target: str, name: str) -> IndexedEdge:
    return IndexedEdge(path, EdgeKind.USES_IMPORTED, target, name)


def _unresolved(
    path: str, specifier: str, resolution: ImportResolution, reason: str, target: str | None = None
) -> IndexedImport:
    return IndexedImport(_record(path, specifier, resolution, target), reason, ())


def test_resolver_version() -> None:
    assert INDEX_RESOLVER_VERSION == "resolver/v1+index/v1"


# --- Python ----------------------------------------------------------------------------

_APP = "pkg/app.py"


def test_python_named_import_stores_the_name_without_reading_the_target() -> None:
    source = "from .models import User, Role, Gone\n\ndef make():\n    return User(), Gone\n"

    item, provider = _resolve(_APP, source, paths=["pkg/models.py"])

    assert item == (
        IndexedImport(
            _record(_APP, ".models", RESOLVED, "pkg/models.py"),
            None,
            (
                _imports(_APP, "pkg/models.py"),
                _uses(_APP, "pkg/models.py", "Gone"),
                _uses(_APP, "pkg/models.py", "User"),
            ),
        ),
    )
    assert provider.asked == []


def test_python_module_import_stores_its_member_names() -> None:
    source = "import util\nimport a.b\nimport a.b as ab\n\ndef run():\n"
    source += "    return util.helper(), util.other, a.b.c(), ab.d()\n"
    imports, _ = _resolve("app.py", source, paths=["src/util.py", "a/b.py"])

    assert imports == (
        IndexedImport(
            _record("app.py", "util", RESOLVED, "src/util.py"),
            None,
            (
                _imports("app.py", "src/util.py"),
                _uses("app.py", "src/util.py", "helper"),
                _uses("app.py", "src/util.py", "other"),
            ),
        ),
        # `a.b.c` is not a member reference of `a.b`: only the module is imported.
        IndexedImport(
            _record("app.py", "a.b", RESOLVED, "a/b.py"), None, (_imports("app.py", "a/b.py"),)
        ),
        IndexedImport(
            _record("app.py", "a.b", RESOLVED, "a/b.py"),
            None,
            (_imports("app.py", "a/b.py"), _uses("app.py", "a/b.py", "d")),
        ),
    )


def test_python_module_targets_keep_every_reason() -> None:
    def item(source: str, *paths: str) -> IndexedImport:
        return _one(_APP, source + "\ndef f():\n    return x\n", paths=paths)

    assert item("from ...x import x\n") == _unresolved(
        _APP, "...x", MISSING, "above_repository_root"
    )
    assert item("from .gone import x\n") == _unresolved(_APP, ".gone", MISSING, "no_candidate")
    assert item("from util import x\n", "util.py", "src/util.py") == _unresolved(
        _APP, "util", AMBIGUOUS, "several_candidates"
    )
    assert item("from requests import x\n") == _unresolved(
        _APP, "requests", EXTERNAL, "no_candidate"
    )
    stub = item("from .lib import x\n", "pkg/lib.pyi")
    assert stub.record == _record(_APP, ".lib", RESOLVED, "pkg/lib.pyi")
    runtime = item("from .lib import x\n", "pkg/lib.py", "pkg/lib.pyi")
    assert runtime.record == _record(_APP, ".lib", RESOLVED, "pkg/lib.py")
    root = "from . import x\n\ndef f():\n    return x\n"
    assert _one("app.py", root, paths=["__init__.py"]).record == _record(
        "app.py", ".", RESOLVED, "__init__.py"
    )
    assert _one("app.py", root) == _unresolved("app.py", ".", MISSING, "no_candidate")


def test_python_bindings_without_a_named_use_give_only_the_imports_edge() -> None:
    target = ["pkg/models.py"]
    only_module = (_imports(_APP, "pkg/models.py"),)

    wildcard = _one(_APP, "from .models import *\n\ndef f():\n    return User()\n", paths=target)
    shadowed = _one(
        _APP, "from .models import User\n\ndef f(User):\n    return User()\n", paths=target
    )
    unused = _one(_APP, "from .models import User\n\ndef f():\n    return 1\n", paths=target)

    for item in (wildcard, shadowed, unused):
        assert (item.record.resolution, item.reason, item.edges) == (RESOLVED, None, only_module)


def test_python_excluded_target_has_no_target_path_and_no_edges() -> None:
    source = "from .secrets import KEY\n\ndef f():\n    return KEY\n"

    item = _one(_APP, source, paths=["pkg/secrets.py"], excluded={"pkg/secrets.py": "secret"})

    assert item == _unresolved(_APP, ".secrets", EXCLUDED, "secret")


_PACKAGE_APP = "from . import helpers\n\ndef run():\n    return helpers.load()\n"


def test_python_package_binding_prefers_a_symbol_of_init_then_the_submodule() -> None:
    init = "pkg/__init__.py"

    submodule, provider = _resolve(
        _APP, _PACKAGE_APP, paths=["pkg/helpers.py"], sources={init: "VERSION = 1\n"}
    )
    assert submodule == (
        IndexedImport(
            _record(_APP, ".", RESOLVED, init),
            None,
            (
                _imports(_APP, init),
                _imports(_APP, "pkg/helpers.py"),
                _uses(_APP, "pkg/helpers.py", "load"),
            ),
        ),
    )
    assert provider.asked == [init]

    declared, _ = _resolve(
        _APP, _PACKAGE_APP, paths=["pkg/helpers.py"], sources={init: "helpers = object()\n"}
    )
    assert declared[0].edges == (_imports(_APP, init), _uses(_APP, init, "helpers"))

    unreadable, _ = _resolve(
        _APP, _PACKAGE_APP, paths=["pkg/helpers.py"], unavailable={init: "ast_failed"}
    )
    assert unreadable[0].edges == (_imports(_APP, init),)

    # Without a single submodule candidate the name is stored as it is; init is not read.
    no_submodule, provider = _resolve(_APP, _PACKAGE_APP, paths=[init])
    assert no_submodule[0].edges == (_imports(_APP, init), _uses(_APP, init, "helpers"))
    assert provider.asked == []

    excluded, _ = _resolve(
        _APP,
        _PACKAGE_APP,
        paths=["pkg/helpers.py"],
        sources={init: ""},
        excluded={"pkg/helpers.py": "ignored"},
    )
    assert excluded[0].edges == (_imports(_APP, init),)


def test_python_unused_submodule_binding_still_imports_the_submodule() -> None:
    source = "from . import helpers\n\ndef run():\n    return 1\n"

    item = _one(_APP, source, paths=["pkg/helpers.py"], sources={"pkg/__init__.py": ""})

    assert item.edges == (_imports(_APP, "pkg/__init__.py"), _imports(_APP, "pkg/helpers.py"))


# --- TypeScript and JavaScript -------------------------------------------------------------

_TS = "src/app.ts"


def test_typescript_named_default_and_namespace_imports_store_exported_names() -> None:
    source = (
        'import d, { a as b } from "./x";\n'
        'import * as ns from "./dir";\n'
        "export function run() { return d() + b() + ns.k() + ns.j }\n"
    )

    imports, provider = _resolve(_TS, source, paths=["src/x.ts", "src/dir/index.ts"])

    assert imports == (
        IndexedImport(
            _record(_TS, "./x", RESOLVED, "src/x.ts"),
            None,
            (
                _imports(_TS, "src/x.ts"),
                _uses(_TS, "src/x.ts", "a"),
                _uses(_TS, "src/x.ts", "default"),
            ),
        ),
        # The barrel index is not followed: its content is never read.
        IndexedImport(
            _record(_TS, "./dir", RESOLVED, "src/dir/index.ts"),
            None,
            (
                _imports(_TS, "src/dir/index.ts"),
                _uses(_TS, "src/dir/index.ts", "j"),
                _uses(_TS, "src/dir/index.ts", "k"),
            ),
        ),
    )
    assert provider.asked == []


def test_a_rebound_namespace_alias_gives_no_member_edge() -> None:
    source = 'import * as ns from "./y";\nexport function run(ns) { return ns.k() }\n'

    item = _one(_TS, source, paths=["src/y.ts"])

    assert item.edges == (_imports(_TS, "src/y.ts"),)


def test_script_module_targets_keep_every_reason() -> None:
    def item(specifier: str, *paths: str, importer: str = _TS) -> IndexedImport:
        source = f'import {{ a }} from "{specifier}";\nexport function run() {{ return a() }}\n'
        return _one(importer, source, paths=paths)

    assert item("@/x") == _unresolved(_TS, "@/x", UNSUPPORTED, "path_alias")
    assert item("lodash") == _unresolved(_TS, "lodash", EXTERNAL, "package")
    assert item("../../x") == _unresolved(_TS, "../../x", MISSING, "above_repository_root")
    assert item("./x.js", "src/x.ts") == _unresolved(
        _TS, "./x.js", UNSUPPORTED, "extension_not_resolved"
    )
    assert item("./gone") == _unresolved(_TS, "./gone", MISSING, "no_candidate")
    assert item("./x", "src/x.ts", "src/x.tsx") == _unresolved(
        _TS, "./x", AMBIGUOUS, "several_candidates"
    )
    exact = item("./x.d.ts", "src/x.d.ts")
    assert exact.record == _record(_TS, "./x.d.ts", RESOLVED, "src/x.d.ts")
    script = item("./x", "src/x.js", "src/x.mjs", importer="src/app.js")
    assert script == _unresolved("src/app.js", "./x", AMBIGUOUS, "several_candidates")


def test_script_forms_outside_static_imports_are_not_followed() -> None:
    source = (
        'import "./side";\n'
        'export * from "./barrel";\n'
        "export async function run() {\n"
        '  const lazy = await import("./lazy");\n'
        '  const old = require("./old");\n'
        "  return lazy.a() + old.b();\n"
        "}\n"
    )
    paths = ["src/side.js", "src/barrel.js", "src/lazy.js", "src/old.js"]

    imports, _ = _resolve("src/app.js", source, paths=paths)

    by_specifier = {item.record.specifier: item for item in imports}
    assert by_specifier == {
        "./side": IndexedImport(
            _record("src/app.js", "./side", RESOLVED, "src/side.js"),
            None,
            (_imports("src/app.js", "src/side.js"),),
        ),
        "./barrel": _unresolved("src/app.js", "./barrel", UNSUPPORTED, "reexport"),
        "./lazy": _unresolved("src/app.js", "./lazy", DYNAMIC, "dynamic_import"),
        "./old": _unresolved("src/app.js", "./old", UNSUPPORTED, "commonjs"),
    }


# --- Go --------------------------------------------------------------------------------

_GO = "cmd/main.go"
_GO_APP = """package main

import (
	"fmt"
	"example.com/m/internal/store"
	al "example.com/m/pkg/util"
)

func Run() error {
	fmt.Println(al.Name())
	return db.Open()
}
"""
_STORE = {"internal/store/open.go": "package db\n\nfunc Open() error { return nil }\n"}


def test_go_record_is_the_directory_and_the_edges_go_to_the_member_files() -> None:
    sources = {
        **_STORE,
        "pkg/util/name.go": 'package util\n\nfunc Name() string { return "n" }\n',
    }

    imports, provider = _resolve(
        _GO, _GO_APP, sources=sources, paths=["internal/store/open_test.go"]
    )

    assert imports == (
        _unresolved(_GO, "fmt", EXTERNAL, "standard_library"),
        IndexedImport(
            _record(_GO, "example.com/m/internal/store", RESOLVED, "internal/store"),
            None,
            (
                _imports(_GO, "internal/store/open.go"),
                _uses(_GO, "internal/store/open.go", "Open"),
            ),
        ),
        IndexedImport(
            _record(_GO, "example.com/m/pkg/util", RESOLVED, "pkg/util"),
            None,
            (_imports(_GO, "pkg/util/name.go"), _uses(_GO, "pkg/util/name.go", "Name")),
        ),
    )
    assert sorted(provider.asked) == ["internal/store/open.go", "pkg/util/name.go"]


def test_go_root_package_directory_is_the_empty_path() -> None:
    source = 'package main\n\nimport "example.com/m"\n\nfunc Run() { m.Do() }\n'

    item = _one(_GO, source, sources={"lib.go": "package m\n\nfunc Do() {}\n"})

    assert item == IndexedImport(
        _record(_GO, _MODULE, RESOLVED, ""),
        None,
        (_imports(_GO, "lib.go"), _uses(_GO, "lib.go", "Do")),
    )


def test_go_blank_and_dot_imports_give_only_the_imports_edge() -> None:
    source = (
        'package main\n\nimport (\n\t_ "example.com/m/internal/store"\n'
        '\t. "example.com/m/pkg/util"\n)\n\nfunc Run() { Name() }\n'
    )
    sources = {**_STORE, "pkg/util/name.go": "package util\n\nfunc Name() {}\n"}

    imports, _ = _resolve(_GO, source, sources=sources)

    assert [(item.record.target_path, item.edges) for item in imports] == [
        ("internal/store", (_imports(_GO, "internal/store/open.go"),)),
        ("pkg/util", (_imports(_GO, "pkg/util/name.go"),)),
    ]


_PACKAGE = {
    "internal/store/open.go": (
        "package store\n\nfunc Open() error { return nil }\n\n"
        "type Conn struct{}\n\nfunc (c Conn) Close() {}\n"
    ),
    "internal/store/close.go": "package store\n\nfunc Shared() {}\n\nfunc local() {}\n",
    "internal/store/both.go": "package store\n\nfunc Shared() {}\n",
}
_USER = (
    'package main\n\nimport "example.com/m/internal/store"\n\n'
    "func Run() { store.Open(); store.Shared(); store.Close(); store.Gone(); store.local() }\n"
)


def test_go_uses_edge_goes_to_the_one_member_that_declares_the_name() -> None:
    item = _one(_GO, _USER, sources=_PACKAGE, paths=["internal/store/open_test.go"])

    # `Shared` is declared twice, `Close` only as a method, `Gone` nowhere and `local` is
    # not exported: none of them gets an edge. The test file is not a member.
    assert item == IndexedImport(
        _record(_GO, "example.com/m/internal/store", RESOLVED, "internal/store"),
        None,
        (
            _imports(_GO, "internal/store/both.go"),
            _imports(_GO, "internal/store/close.go"),
            _imports(_GO, "internal/store/open.go"),
            _uses(_GO, "internal/store/open.go", "Open"),
        ),
    )


def test_go_declaration_moving_between_members_moves_the_edge_under_rule_g() -> None:
    before = {
        "internal/store/open.go": "package store\n\nfunc Open() {}\n",
        "internal/store/close.go": "package store\n",
    }
    after = {
        "internal/store/open.go": "package store\n",
        "internal/store/close.go": "package store\n\nfunc Open() {}\n",
    }
    source = (
        'package main\n\nimport "example.com/m/internal/store"\n\nfunc Run() { store.Open() }\n'
    )

    old = _one(_GO, source, sources=before)
    new = _one(_GO, source, sources=after)

    assert old.record == new.record
    assert [edge for edge in old.edges if edge.kind is EdgeKind.USES_IMPORTED] == [
        _uses(_GO, "internal/store/open.go", "Open")
    ]
    assert [edge for edge in new.edges if edge.kind is EdgeKind.USES_IMPORTED] == [
        _uses(_GO, "internal/store/close.go", "Open")
    ]
    assert _affected([old.record], changed=sorted(before)) == frozenset({_GO})


def test_go_package_outcomes_keep_every_reason() -> None:
    specifier = "example.com/m/internal/store"
    app = _GO_APP.replace("db.Open()", "store.Open()")

    def store(**options: object) -> IndexedImport:
        imports, _ = _resolve(_GO, app, **options)  # type: ignore[arg-type]
        return next(item for item in imports if item.record.specifier == specifier)

    package = {"internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n"}
    util = {"pkg/util/name.go": "package util\n\nfunc Name() {}\n"}
    sources = {**package, **util}

    assert store(sources=sources).edges == (
        _imports(_GO, "internal/store/open.go"),
        _uses(_GO, "internal/store/open.go", "Open"),
    )
    assert store(sources=sources, go_module=None) == _unresolved(
        _GO, specifier, UNSUPPORTED, "module_not_resolved"
    )
    assert store(sources=sources, paths=["go.work"]) == _unresolved(
        _GO, specifier, UNSUPPORTED, "workspace_or_incomplete_tree"
    )
    assert store(sources=sources, paths=["internal/go.mod"]) == _unresolved(
        _GO, specifier, UNSUPPORTED, "nested_module"
    )
    assert store(sources=util, paths=["internal/store/README.md"]) == _unresolved(
        _GO, specifier, MISSING, "no_package_files"
    )
    assert store(sources={**sources, "internal/store/open_linux.go": "package store\n"}) == (
        _unresolved(_GO, specifier, UNSUPPORTED, "build_constrained")
    )
    # Outcomes decided by member facts keep the directory, so rule G finds the importer.
    tagged = {**sources, "internal/store/tag.go": "//go:build linux\n\npackage store\n"}
    assert store(sources=tagged) == _unresolved(
        _GO, specifier, UNSUPPORTED, "build_constrained", "internal/store"
    )
    other = {**sources, "internal/store/other.go": "package other\n"}
    assert store(sources=other) == _unresolved(
        _GO, specifier, AMBIGUOUS, "several_package_names", "internal/store"
    )
    assert store(sources=util, unavailable={"internal/store/open.go": "ast_failed"}) == (
        _unresolved(_GO, specifier, UNSUPPORTED, "ast_failed", "internal/store")
    )
    assert store(sources=util, unavailable={"internal/store/open.go": "binary"}) == (
        _unresolved(_GO, specifier, EXCLUDED, "binary", "internal/store")
    )
    # A member excluded by its path excludes the package; the path is not stored.
    assert store(sources=sources, excluded={"internal/store/open.go": "ignored"}) == (
        _unresolved(_GO, specifier, EXCLUDED, "ignored")
    )
    other_module = _GO_APP.replace("example.com/m/internal/store", "example.com/x/store")
    imports, _ = _resolve(_GO, other_module, sources=util)
    assert imports[1] == _unresolved(_GO, "example.com/x/store", EXTERNAL, "other_module")


def test_go_package_facts_are_read_once_per_build() -> None:
    provider = Provider({**_STORE, "pkg/util/name.go": "package util\n"})
    resolver = _resolver(provider, {_GO, "cmd/other.go", *provider.sources})

    async def run() -> None:
        for path in (_GO, "cmd/other.go"):
            await resolver.resolve_file(path, _facts(path, _GO_APP))

    asyncio.run(run())

    assert sorted(provider.asked) == ["internal/store/open.go", "pkg/util/name.go"]


# --- No per-run caps -------------------------------------------------------------------


def test_no_caps_every_imported_name_of_a_large_file_resolves() -> None:
    modules = [f"m{index}" for index in range(25)]
    names = {module: [f"n{module}_{index}" for index in range(5)] for module in modules}
    source = "".join(f"from .{module} import {', '.join(names[module])}\n" for module in modules)
    used = [name for module in modules for name in names[module]]
    source += "\ndef run():\n    return [" + ", ".join(used) + "]\n"

    imports, _ = _resolve(_APP, source, paths=[f"pkg/{module}.py" for module in modules])

    uses = [edge for item in imports for edge in item.edges if edge.kind is EdgeKind.USES_IMPORTED]
    assert len(imports) == 25
    assert len(uses) == 125
    assert {(edge.target_path, edge.target_name) for edge in uses} == {
        (f"pkg/{module}.py", name) for module in modules for name in names[module]
    }


def test_no_caps_a_namespace_import_keeps_every_member() -> None:
    calls = " + ".join(f"lib.f{index}()" for index in range(120))
    source = f'import * as lib from "./lib";\nexport function run() {{ return {calls} }}\n'

    item = _one(_TS, source, paths=["src/lib.ts"])

    assert len(item.edges) == 121
    assert item.edges[-1] == _uses(_TS, "src/lib.ts", "f99")


# --- Rules W, I, G and F ---------------------------------------------------------------


def _affected(
    records: Iterable[ImportRecord],
    *,
    delta: Iterable[str] = (),
    changed: Iterable[str] = (),
    limit: int = 500,
) -> frozenset[str] | None:
    return affected_importers(records, delta=delta, changed=changed, go_module=_MODULE, limit=limit)


_RECORDS = (
    _record("pkg/a.py", ".models", MISSING),
    _record("pkg/b.py", ".", RESOLVED, "pkg/__init__.py"),
    _record("src/c.ts", "./dir", RESOLVED, "src/dir/index.ts"),
    _record("cmd/d.go", "example.com/m/internal/store", RESOLVED, "internal/store"),
    _record("cmd/e.go", _MODULE, RESOLVED, ""),
    _record("app/f.py", "requests", EXTERNAL),
)


def test_rule_w_a_candidate_or_a_child_module_entering_or_leaving_the_path_set() -> None:
    # MISSING -> RESOLVED and EXTERNAL -> RESOLVED when a file is added; `pkg/models.py`
    # is also a child module of the package `pkg/b.py` imports from.
    assert _affected(_RECORDS, delta=["pkg/models.py"]) == frozenset({"pkg/a.py", "pkg/b.py"})
    assert _affected(_RECORDS, delta=["src/requests/__init__.py"]) == frozenset({"app/f.py"})
    # A child module of a watched package: `from . import x` may now bind it.
    assert _affected(_RECORDS, delta=["pkg/new.py"]) == frozenset({"pkg/b.py"})
    assert _affected(_RECORDS, delta=["pkg/sub/__init__.py"]) == frozenset({"pkg/b.py"})
    assert _affected(_RECORDS, delta=["internal/store/new.go"]) == frozenset({"cmd/d.go"})
    # Both paths of a rename.
    assert _affected(_RECORDS, delta=["src/dir/index.ts", "src/dir/main.ts"]) == frozenset(
        {"src/c.ts"}
    )
    assert _affected(_RECORDS, delta=["docs/readme.md", "pkg/deep/x.py"]) == frozenset()


def test_rule_i_a_changed_init_or_index_that_is_a_target() -> None:
    assert _affected(_RECORDS, changed=["pkg/__init__.py"]) == frozenset({"pkg/b.py"})
    assert _affected(_RECORDS, changed=["src/dir/index.ts"]) == frozenset({"src/c.ts"})
    assert _affected(_RECORDS, changed=["pkg/models.py", "src/dir/other.ts"]) == frozenset()


def test_rule_g_a_changed_go_file_in_the_target_directory() -> None:
    assert _affected(_RECORDS, changed=["internal/store/open.go"]) == frozenset({"cmd/d.go"})
    assert _affected(_RECORDS, changed=["lib.go"]) == frozenset({"cmd/e.go"})
    assert _affected(_RECORDS, changed=["internal/store/sub/x.go", "main.py"]) == frozenset()


def test_rule_f_go_module_files_or_too_many_paths_force_a_full_build() -> None:
    for path in ("go.mod", "internal/go.mod", "go.work", "tools/go.work"):
        assert _affected(_RECORDS, changed=[path]) is None
        assert _affected(_RECORDS, delta=[path]) is None
    changed = [f"x/{index}.txt" for index in range(499)]
    assert _affected(_RECORDS, changed=changed, delta=["pkg/new.py"]) is None
    assert _affected(_RECORDS, changed=changed[:498], delta=["pkg/new.py"]) == frozenset(
        {"pkg/b.py"}
    )


# --- Incremental = full ----------------------------------------------------------------

_IMPORTERS = {
    "pkg/app.py": (
        "from . import helpers, Thing\nfrom .models import User\nimport util\n\n"
        "def run():\n    return helpers.load(), Thing, User(), util.go()\n"
    ),
    "pkg/sub/deep.py": (
        "from .. import helpers\nfrom ..models import User\n\n"
        "def run():\n    return helpers.load(), User\n"
    ),
    "src/app.ts": (
        'import { a } from "./x";\nimport * as ns from "./y";\n'
        "export function run() { return a() + ns.b() }\n"
    ),
    "cmd/main.go": (
        'package main\n\nimport "example.com/m/internal/store"\n\n'
        "func Run() { store.Open(); db.Close() }\n"
    ),
}
_FAILED = "<unavailable>"
_VARIANTS: dict[str, tuple[str | None, ...]] = {
    "pkg/__init__.py": (None, "", "helpers = 1\n", "Thing = 2\n", _FAILED),
    "pkg/helpers.py": (None, "def load():\n    pass\n"),
    "pkg/helpers/__init__.py": (None, ""),
    "pkg/models.py": (None, "class User:\n    pass\n"),
    "pkg/models.pyi": (None, "class User: ...\n"),
    "util.py": (None, "def go():\n    pass\n"),
    "src/util.py": (None, "def go():\n    pass\n"),
    "src/x.ts": (None, "export function a() {}\n"),
    "src/x/index.ts": (None, 'export * from "./a";\n'),
    "src/y.ts": (None, "export function b() {}\n"),
    "src/y.tsx": (None, "export function b() {}\n"),
    "internal/store/open.go": (
        *(None, "package store\n\nfunc Open() {}\n", "package db\n\nfunc Close() {}\n"),
        *("//go:build linux\n\npackage store\n", _FAILED),
    ),
    "internal/store/extra.go": (
        *(None, "package store\n", "package other\n", "package store\n\nfunc Open() {}\n"),
        "package db\n\nfunc Close() {}\n",
    ),
    "internal/store/open_linux.go": (None, "package store\n"),
}


def _build(state: Mapping[str, str | None]) -> dict[str, tuple[IndexedImport, ...]]:
    present = {path: content for path, content in state.items() if content is not None}
    provider = Provider(
        {path: content for path, content in present.items() if content != _FAILED},
        {path: "ast_failed" for path, content in present.items() if content == _FAILED},
    )
    resolver = _resolver(provider, {*_IMPORTERS, *present})

    async def run() -> dict[str, tuple[IndexedImport, ...]]:
        return {
            path: await resolver.resolve_file(path, _facts(path, source))
            for path, source in _IMPORTERS.items()
        }

    return asyncio.run(run())


def test_importers_outside_the_rules_resolve_as_a_full_build_would() -> None:
    """AC6 at the resolver: an importer the rules skip keeps the edges of a full build."""
    rng = random.Random(142)
    kept = 0
    for _ in range(300):
        before = {path: rng.choice(variants) for path, variants in _VARIANTS.items()}
        after = dict(before)
        for path in rng.sample(sorted(_VARIANTS), rng.randint(1, 3)):
            after[path] = rng.choice(_VARIANTS[path])
        delta = {path for path in _VARIANTS if (before[path] is None) != (after[path] is None)}
        changed = {path for path in _VARIANTS if before[path] != after[path]}
        old, new = _build(before), _build(after)

        records = [item.record for imports in old.values() for item in imports]
        affected = _affected(records, delta=delta, changed=changed)

        assert affected is not None
        for importer in _IMPORTERS.keys() - affected:
            kept += 1
            assert old[importer] == new[importer], (importer, before, after)
    # The rules must leave importers alone often enough for the check to mean something.
    assert kept > 300
