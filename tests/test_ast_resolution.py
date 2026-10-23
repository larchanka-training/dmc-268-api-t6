"""Affected symbols and one-step import resolution: resolve exactly or keep a reason."""

from __future__ import annotations

import asyncio
import posixpath
import random
import time
from dataclasses import dataclass, field
from uuid import UUID

import pytest

from app.modules.reviews.application import ast_resolution
from app.modules.reviews.application.ast_resolution import (
    FileUnavailable,
    GoLayout,
    ImportRef,
    ImportResolution,
    ImportResolver,
    ImportWatch,
    ModuleTarget,
    RevisionTree,
    affected_symbols,
    exported_symbols,
    go_layout,
    go_module_path,
    go_package_name,
    go_package_target,
    import_watch,
    module_target,
    python_module_target,
    python_submodule_files,
    script_module_target,
)
from app.modules.reviews.application.ast_symbols import AstFacts, AstLanguage, language_for_path
from app.modules.reviews.application.diff_map import DiffFile, VcsProviderName, build_diff_map
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import (
    PullRequestLocator,
    VcsFile,
    VcsPullRequest,
    _is_binary,
    _is_generated,
    is_binary_content,
    is_generated_path,
)
from app.modules.reviews.infrastructure.tree_sitter_facts import extract_facts

_HEAD = "a" * 40


def _facts(path: str, source: str) -> AstFacts:
    language = language_for_path(path)
    assert language is not None
    return extract_facts(language, source.encode(), cpu_seconds=5.0)


@dataclass
class Provider:
    """Facts of in-memory sources; ``unavailable`` maps a path to a failure reason."""

    sources: dict[str, str]
    unavailable: dict[str, str] = field(default_factory=dict)
    asked: list[str] = field(default_factory=list)

    async def facts_for(self, path: str) -> AstFacts | FileUnavailable:
        self.asked.append(path)
        if path in self.unavailable:
            return FileUnavailable(self.unavailable[path])
        return _facts(path, self.sources[path])


def _resolve(
    sources: dict[str, str],
    importer: str,
    symbol: str,
    *,
    unavailable: dict[str, str] | None = None,
    complete: bool = True,
) -> tuple[tuple[ImportRef, ...], Provider]:
    provider = Provider(sources, unavailable or {})
    tree = RevisionTree(_HEAD, {path: "b" * 40 for path in sources}, complete)
    go_mod = sources.get("go.mod")
    resolver = ImportResolver(
        tree=tree,
        provider=provider,
        go_module=None if go_mod is None else go_module_path(go_mod),
    )
    facts = _facts(importer, sources[importer])
    symbols = [item for item in facts.symbols if item.name == symbol]
    refs = asyncio.run(resolver.resolve(importer_path=importer, facts=facts, symbols=symbols))
    return refs, provider


def _one(sources: dict[str, str], importer: str, symbol: str, **options: object) -> ImportRef:
    refs, _ = _resolve(sources, importer, symbol, **options)  # type: ignore[arg-type]
    assert len(refs) == 1, refs
    return refs[0]


def _diff_file(patch: str, additions: int, deletions: int, path: str = "app.py") -> DiffFile:
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
            files_changed=1,
            lines_added=0,
            lines_removed=0,
            is_draft=False,
            is_fork=False,
        ),
    )
    file = VcsFile(
        path, "modified", "d" * 40, None, additions, deletions, additions + deletions, patch
    )
    return build_diff_map(
        pull_request,
        (file,),
        provider=VcsProviderName.GITHUB,
        repository_id=UUID(int=1),
        diff_old_sha=None,
    ).files[0]


_MODULE = """import os

LIMIT = 10


def first():
    return 1


class Service:
    def run(self):
        return 2

    def stop(self):
        return 3
"""


def test_added_line_selects_the_innermost_declaration() -> None:
    facts = _facts(
        "app.py", _MODULE.replace("        return 2\n", "        x = 1\n        return 2\n")
    )
    in_method = _diff_file(
        "@@ -11,2 +11,3 @@\n     def run(self):\n+        x = 1\n         return 2", 1, 0
    )

    affected = affected_symbols(facts, in_method)

    assert [item.name for item in affected.symbols] == ["run"]
    assert affected.module_level is False


def test_change_between_declarations_is_module_level_not_a_neighbour() -> None:
    facts = _facts("app.py", _MODULE.replace("LIMIT = 10\n\n", "LIMIT = 10\n\nprint(LIMIT)\n"))
    between = _diff_file("@@ -4,2 +4,3 @@\n \n+print(LIMIT)\n ", 1, 0)

    affected = affected_symbols(facts, between)

    assert affected.symbols == ()
    assert affected.module_level is True


def test_several_hunks_collect_each_symbol_once_in_source_order() -> None:
    # The facts describe the new file, where both hunks have already added their lines.
    changed = _MODULE.replace("    return 1\n", "    a = 1\n    return 1\n").replace(
        "        return 3\n", "        b = 1\n        c = 2\n        return 3\n"
    )
    facts = _facts("app.py", changed)
    patch = (
        "@@ -6,2 +6,3 @@\n def first():\n+    a = 1\n     return 1\n"
        "@@ -14,2 +15,4 @@\n     def stop(self):\n+        b = 1\n+        c = 2\n         return 3"
    )

    affected = affected_symbols(facts, _diff_file(patch, 3, 0))

    assert [item.name for item in affected.symbols] == ["first", "stop"]
    assert affected.module_level is False


def test_removed_lines_locate_the_symbol_by_the_surviving_lines() -> None:
    facts = _facts("app.py", _MODULE)
    removed = _diff_file(
        "@@ -11,3 +11,2 @@\n     def run(self):\n-        gone = 1\n         return 2", 0, 1
    )

    assert [item.name for item in affected_symbols(facts, removed).symbols] == ["run"]


_TS_MODULE = """import { log } from "./log";

export function first(): number {
  return 1;
}

export class Service {
  run(): number {
    return 2;
  }

  stop(): number {
    return 3;
  }
}
"""
_JS_MODULE = _TS_MODULE.replace("(): number {", "() {")


@pytest.mark.parametrize(
    ("path", "source"),
    [
        pytest.param("src/app.ts", _TS_MODULE, id="ts"),
        pytest.param("src/app.js", _JS_MODULE, id="js"),
    ],
)
def test_script_added_line_selects_the_method_and_a_statement_between_is_module_level(
    path: str, source: str
) -> None:
    in_method = affected_symbols(
        _facts(path, source.replace("    return 2;\n", "    const x = 1;\n    return 2;\n")),
        _diff_file("@@ -8,2 +8,3 @@\n   run() {\n+    const x = 1;\n     return 2;", 1, 0, path),
    )
    between = affected_symbols(
        _facts(path, source.replace("}\n\nexport class", "}\n\nlog(1);\nexport class")),
        _diff_file("@@ -6,2 +6,3 @@\n \n+log(1);\n export class Service {", 1, 0, path),
    )

    assert ([item.name for item in in_method.symbols], in_method.module_level) == (["run"], False)
    assert (between.symbols, between.module_level) == ((), True)


def test_python_relative_import_resolves_the_used_name_only() -> None:
    sources = {
        "pkg/app.py": "from .models import User, Role\n\ndef make():\n    return User()\n",
        "pkg/models.py": 'class User:\n    """A user."""\n\nclass Role:\n    pass\n',
    }

    ref = _one(sources, "pkg/app.py", "make")

    assert (ref.resolution, ref.module, ref.names) == (
        ImportResolution.RESOLVED,
        ".models",
        ("User",),
    )
    symbol = ref.resolved_symbols[0]
    assert (symbol.path, symbol.name, symbol.revision) == ("pkg/models.py", "User", _HEAD)
    assert (symbol.docstring_ranges[0].start_line, symbol.blob_sha) == (2, "b" * 40)
    assert ref.resolution_profile_version == "resolver/v1"


def test_python_absolute_import_needs_a_single_candidate() -> None:
    importer = "import util\n\ndef run():\n    return util.helper()\n"
    target = "def helper():\n    return 1\n"

    one = _one({"app.py": importer, "src/util.py": target}, "app.py", "run")
    assert one.resolution is ImportResolution.RESOLVED
    assert one.resolved_symbols[0].path == "src/util.py"

    two = _one({"app.py": importer, "util.py": target, "src/util.py": target}, "app.py", "run")
    assert (two.resolution, two.resolved_symbols) == (ImportResolution.AMBIGUOUS, ())

    assert _one({"app.py": importer}, "app.py", "run").resolution is ImportResolution.EXTERNAL


def test_python_stub_beside_runtime_is_not_a_rival_and_submodule_is_followed() -> None:
    sources = {
        "pkg/app.py": "from . import helpers\n\ndef run():\n    return helpers.load()\n",
        "pkg/__init__.py": "",
        "pkg/helpers.py": "def load():\n    return 1\n",
        "pkg/helpers.pyi": "def load() -> int: ...\n",
    }

    ref = _one(sources, "pkg/app.py", "run")

    assert ref.resolution is ImportResolution.RESOLVED
    assert (ref.resolved_symbols[0].path, ref.names) == ("pkg/helpers.py", ("load",))


def test_python_missing_name_shadowing_and_wildcard_are_not_guessed() -> None:
    target = {"pkg/models.py": "class User:\n    pass\n"}

    missing = _one(
        {"pkg/app.py": "from .models import Gone\n\ndef f():\n    return Gone()\n", **target},
        "pkg/app.py",
        "f",
    )
    assert missing.resolution is ImportResolution.MISSING

    shadowed = _one(
        {"pkg/app.py": "from .models import User\n\ndef f(User):\n    return User()\n", **target},
        "pkg/app.py",
        "f",
    )
    assert (shadowed.resolution, shadowed.reason) == (ImportResolution.AMBIGUOUS, "shadowed")

    refs, provider = _resolve(
        {"pkg/app.py": "from .models import *\n\ndef f():\n    return User()\n", **target},
        "pkg/app.py",
        "f",
    )
    assert [
        (ref.module, ref.names, ref.resolved_symbols, ref.resolution, ref.reason) for ref in refs
    ] == [(".models", (), (), ImportResolution.UNSUPPORTED, "no_named_symbol")]
    assert provider.asked == []


def test_unused_import_is_never_loaded() -> None:
    sources = {
        "pkg/app.py": "from .models import User\n\ndef f():\n    return 1\n",
        "pkg/models.py": "class User:\n    pass\n",
    }

    refs, provider = _resolve(sources, "pkg/app.py", "f")

    assert refs == ()
    assert provider.asked == []


def test_go_dot_import_used_by_the_change_is_reported_not_loaded() -> None:
    sources = {
        "go.mod": "module example.com/m\n\ngo 1.22\n",
        "cmd/main.go": (
            'package main\n\nimport . "example.com/m/internal/store"\n\n'
            "func Run() error {\n\treturn Open()\n}\n"
        ),
        "internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n",
    }

    refs, provider = _resolve(sources, "cmd/main.go", "Run")

    assert [
        (ref.module, ref.names, ref.resolved_symbols, ref.resolution, ref.reason) for ref in refs
    ] == [
        (
            "example.com/m/internal/store",
            (),
            (),
            ImportResolution.UNSUPPORTED,
            "no_named_symbol",
        )
    ]
    assert provider.asked == []


@pytest.mark.parametrize(
    ("importer", "symbol", "sources"),
    [
        pytest.param(
            "pkg/app.py",
            "f",
            {
                "pkg/app.py": "from .models import *\n\ndef f():\n    return 1\n",
                "pkg/models.py": "class User:\n    pass\n",
            },
            id="python-wildcard-unused",
        ),
        pytest.param(
            "pkg/app.py",
            "run",
            {
                "pkg/app.py": (
                    "from .models import *\n\n\nclass Service:\n"
                    "    def run(self):\n        return 1\n"
                ),
                "pkg/models.py": "class User:\n    pass\n",
            },
            id="python-wildcard-unused-in-a-method",
        ),
        pytest.param(
            "pkg/app.py",
            "f",
            {
                "pkg/app.py": (
                    "from .models import *\nimport os\n\n\ndef helper():\n    return 1\n\n\n"
                    "def f(x):\n    y = x\n    return os, helper(), y\n"
                ),
                "pkg/models.py": "class User:\n    pass\n",
            },
            id="python-wildcard-only-names-the-file-binds",
        ),
        pytest.param(
            "cmd/main.go",
            "Run",
            {
                "go.mod": "module example.com/m\n\ngo 1.22\n",
                "cmd/main.go": (
                    'package main\n\nimport . "example.com/m/internal/store"\n\n'
                    "func Run() int {\n\treturn 1\n}\n"
                ),
                "internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n",
            },
            id="go-dot-import-only-predeclared-names",
        ),
        pytest.param(
            "cmd/main.go",
            "Run",
            {
                "go.mod": "module example.com/m\n\ngo 1.22\n",
                "cmd/main.go": (
                    'package main\n\nimport _ "example.com/m/internal/store"\n\n'
                    "func Run() error {\n\treturn Open()\n}\n"
                ),
                "internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n",
            },
            id="go-blank-import",
        ),
        pytest.param(
            "src/app.ts",
            "f",
            {
                "src/app.ts": 'export * from "./x";\nexport function f() { return g(); }\n',
                "src/x.ts": "export function g() { return 1 }\n",
            },
            id="ts-export-star",
        ),
    ],
)
def test_wildcard_the_change_cannot_use_by_name_gives_no_ref(
    importer: str, symbol: str, sources: dict[str, str]
) -> None:
    refs, provider = _resolve(sources, importer, symbol)

    assert refs == ()
    assert provider.asked == []


def test_typescript_named_default_and_namespace_imports() -> None:
    sources = {
        "src/app.ts": (
            'import make, { helper as h } from "./lib";\n'
            'import * as ns from "./types";\n'
            "export function run(x: ns.Shape): number { return h(make(x)); }\n"
        ),
        "src/lib.ts": (
            "export default function make(x: unknown) { return x }\n"
            "export function helper(x: unknown) { return 1 }\n"
        ),
        "src/types/index.ts": "export interface Shape { a: number }\n",
    }

    refs, _ = _resolve(sources, "src/app.ts", "run")

    assert {(ref.names, ref.resolution, ref.resolved_symbols[0].path) for ref in refs} == {
        (("default",), ImportResolution.RESOLVED, "src/lib.ts"),
        (("helper",), ImportResolution.RESOLVED, "src/lib.ts"),
        (("Shape",), ImportResolution.RESOLVED, "src/types/index.ts"),
    }


def test_typescript_cases_outside_resolver_v1_keep_their_reason() -> None:
    def ref(specifier: str, extra: dict[str, str]) -> ImportRef:
        source = f'import {{ a }} from "{specifier}";\nexport function run() {{ return a(); }}\n'
        return _one({"src/app.ts": source, **extra}, "src/app.ts", "run")

    target = "export function a() { return 1 }\n"
    assert (
        ref("./x", {"src/x.ts": target, "src/x.tsx": target}).resolution
        is ImportResolution.AMBIGUOUS
    )
    assert ref("./x", {}).resolution is ImportResolution.MISSING
    assert ref("./x.js", {"src/x.ts": target}).resolution is ImportResolution.UNSUPPORTED
    assert ref("@/x", {"src/x.ts": target}).resolution is ImportResolution.UNSUPPORTED
    assert ref("lodash", {}).resolution is ImportResolution.EXTERNAL
    barrel = ref("./x", {"src/x.ts": 'export * from "./y";\n'})
    assert (barrel.resolution, barrel.reason) == (ImportResolution.UNSUPPORTED, "reexport_chain")
    assert ref("./x.d", {"src/x.d.ts": "export declare function a(): number;\n"}).resolution is (
        ImportResolution.UNSUPPORTED
    )
    assert ref("./x", {"src/x.d.ts": "export declare function a(): number;\n"}).resolution is (
        ImportResolution.RESOLVED
    )


def test_javascript_relative_es_imports_and_the_forms_it_does_not_follow() -> None:
    target = "export function a() { return 1 }\nexport default function () { return 2 }\n"

    explicit = _one(
        {
            "src/app.js": (
                'import d, { a } from "./lib.js";\nexport function run() { return a() + 1 }\n'
            ),
            "src/lib.js": target,
        },
        "src/app.js",
        "run",
    )
    assert (explicit.resolution, explicit.resolved_symbols[0].path) == (
        ImportResolution.RESOLVED,
        "src/lib.js",
    )

    anonymous = _one(
        {
            "src/app.js": 'import d from "./lib";\nexport function run() { return d() }\n',
            "src/lib.mjs": target,
        },
        "src/app.js",
        "run",
    )
    assert anonymous.resolution is ImportResolution.RESOLVED
    assert anonymous.resolved_symbols[0].name is None

    def ref(source: str, extra: dict[str, str]) -> ImportRef:
        return _one({"src/app.js": source, **extra}, "src/app.js", "run")

    required = ref(
        'const lib = require("./lib");\nfunction run() { return lib.a() }\n', {"src/lib.js": target}
    )
    assert (required.resolution, required.reason) == (ImportResolution.UNSUPPORTED, "commonjs")
    cjs = ref(
        'import { a } from "./lib.cjs";\nfunction run() { return a() }\n', {"src/lib.cjs": target}
    )
    assert cjs.resolution is ImportResolution.UNSUPPORTED
    mixed = ref(
        'import { a } from "./lib.ts";\nfunction run() { return a() }\n', {"src/lib.ts": target}
    )
    assert mixed.resolution is ImportResolution.UNSUPPORTED


def test_tsx_and_jsx_components_resolve_to_their_own_extension() -> None:
    tsx = _one(
        {
            "src/App.tsx": (
                'import { Button } from "./Button";\n\n'
                'export function App() {\n  return <Button label="ok" />;\n}\n'
            ),
            "src/Button.tsx": (
                "export function Button(props: { label: string }) {\n"
                "  return <button>{props.label}</button>;\n}\n"
            ),
        },
        "src/App.tsx",
        "App",
    )
    jsx = _one(
        {
            "src/App.jsx": (
                'import Button from "./Button";\n\n'
                'export function App() {\n  return <Button label="ok" />;\n}\n'
            ),
            "src/Button.jsx": (
                "export default function Button({ label }) {\n"
                "  return <button>{label}</button>;\n}\n"
            ),
        },
        "src/App.jsx",
        "App",
    )

    assert (tsx.resolution, tsx.names) == (ImportResolution.RESOLVED, ("Button",))
    assert (tsx.resolved_symbols[0].path, tsx.resolved_symbols[0].name) == (
        "src/Button.tsx",
        "Button",
    )
    assert (jsx.resolution, jsx.names) == (ImportResolution.RESOLVED, ("default",))
    assert (jsx.resolved_symbols[0].path, jsx.resolved_symbols[0].name) == (
        "src/Button.jsx",
        "Button",
    )


_GO_MOD = "module example.com/m\n\ngo 1.22\n"
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


def test_go_local_package_is_bound_by_its_package_clause_not_its_directory() -> None:
    sources = {
        "go.mod": _GO_MOD,
        "cmd/main.go": _GO_APP,
        "internal/store/open.go": (
            "package db\n\n// Open opens.\nfunc Open() error { return nil }\n"
        ),
        "internal/store/open_test.go": "package db\n\nfunc Open() {}\n",
        "pkg/util/name.go": 'package util\n\nfunc Name() string { return "n" }\n',
    }

    refs, provider = _resolve(sources, "cmd/main.go", "Run")

    by_module = {ref.module: ref for ref in refs}
    store = by_module["example.com/m/internal/store"]
    assert (store.resolution, store.names) == (ImportResolution.RESOLVED, ("Open",))
    assert store.resolved_symbols[0].path == "internal/store/open.go"
    assert by_module["example.com/m/pkg/util"].resolved_symbols[0].name == "Name"
    assert by_module["fmt"].resolution is ImportResolution.EXTERNAL
    assert "internal/store/open_test.go" not in provider.asked


def test_go_resolution_is_refused_when_the_package_is_not_fully_known() -> None:
    # Here the package is named after its directory, so an unresolved import is still
    # attributed to the call that uses it.
    package = {"internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n"}
    app = _GO_APP.replace("db.Open()", "store.Open()")

    def store(extra: dict[str, str], **options: object) -> ImportRef:
        sources = {"go.mod": _GO_MOD, "cmd/main.go": app, **package, **extra}
        refs, _ = _resolve(sources, "cmd/main.go", "Run", **options)  # type: ignore[arg-type]
        return next(ref for ref in refs if ref.module.endswith("/store"))

    def reason(ref: ImportRef) -> tuple[ImportResolution, str | None]:
        return ref.resolution, ref.reason

    assert reason(store({"internal/store/open_linux.go": "package store\n"})) == (
        ImportResolution.UNSUPPORTED,
        "build_constrained",
    )
    assert reason(store({"internal/store/tag.go": "//go:build linux\n\npackage store\n"})) == (
        ImportResolution.UNSUPPORTED,
        "build_constrained",
    )
    assert reason(store({"internal/store/other.go": "package other\n"})) == (
        ImportResolution.AMBIGUOUS,
        "several_package_names",
    )
    assert reason(
        store({"internal/store/dup.go": "package store\n\nfunc Open() error { return nil }\n"})
    ) == (
        ImportResolution.AMBIGUOUS,
        "several_declarations",
    )
    assert reason(store({"internal/go.mod": "module example.com/m/internal\n"})) == (
        ImportResolution.UNSUPPORTED,
        "nested_module",
    )
    assert store({"go.work": "go 1.22\n"}).resolution is ImportResolution.UNSUPPORTED
    assert store({}, complete=False).resolution is ImportResolution.UNSUPPORTED
    assert reason(store({}, unavailable={"internal/store/open.go": "excluded"})) == (
        ImportResolution.EXCLUDED,
        "excluded",
    )
    assert reason(store({}, unavailable={"internal/store/open.go": "ast_limit"})) == (
        ImportResolution.UNSUPPORTED,
        "ast_limit",
    )
    assert reason(store({}, unavailable={"internal/store/open.go": "source_decode_failed"})) == (
        ImportResolution.UNSUPPORTED,
        "source_decode_failed",
    )


@pytest.mark.parametrize(
    ("path", "source", "extracted"),
    [
        pytest.param(
            "internal/store/broken.go",
            "package store\n\nfunc Close( {\n}\n",
            ["Close"],
            id="no-duplicate",
        ),
        pytest.param(
            "internal/store/broken.go",
            "package store\n\nvar x = (\nfunc Open() error { return nil }\n",
            [],
            id="declaration-inside-the-error",
        ),
        pytest.param(
            "internal/store/broken.go",
            "packag store\n\nfunc Close() {}\n",
            ["Close"],
            id="broken-clause-beside-a-valid-member",
        ),
        pytest.param(
            "internal/store/open.go",
            "packag store\n\nfunc Open() error { return nil }\n",
            ["Open"],
            id="only-member-with-a-broken-clause",
        ),
    ],
)
def test_go_member_with_a_parse_error_refuses_the_whole_package(
    path: str, source: str, extracted: list[str]
) -> None:
    # CONTEXT §3.3: a member that does not parse can hide a declaration or its package
    # clause, so a name is not unique just because the parsed members declare it once.
    facts = _facts(path, source)
    assert facts.has_errors is True
    assert [symbol.name for symbol in facts.symbols] == extracted
    sources = {
        "go.mod": _GO_MOD,
        "cmd/main.go": _GO_APP.replace("db.Open()", "store.Open()"),
        "internal/store/open.go": "package store\n\nfunc Open() error { return nil }\n",
        path: source,
    }

    refs, _ = _resolve(sources, "cmd/main.go", "Run")

    assert [
        (ref.resolution, ref.reason, ref.resolved_symbols)
        for ref in refs
        if ref.module == "example.com/m/internal/store"
    ] == [(ImportResolution.UNSUPPORTED, "ast_failed", ())]


_GO_OPEN = "package CLAUSE\n\nfunc Open() error { return nil }\n"


@pytest.mark.parametrize("clause", ["db", "store"])
@pytest.mark.parametrize(
    ("members", "unavailable", "expected"),
    [
        pytest.param(
            {"internal/store/open.go": _GO_OPEN + "\nfunc Close( {\n}\n"},
            {},
            (ImportResolution.UNSUPPORTED, "ast_failed"),
            id="parse-error",
        ),
        pytest.param(
            {"internal/store/a.go": "packag CLAUSE\n", "internal/store/open.go": _GO_OPEN},
            {},
            (ImportResolution.UNSUPPORTED, "ast_failed"),
            id="member-without-a-clause-first",
        ),
        pytest.param(
            {
                "internal/store/a.go": "package CLAUSE\n",
                "internal/store/open.go": _GO_OPEN,
                "internal/store/zz.go": "package CLAUSE\n\nfunc Close() {}\n",
            },
            {"internal/store/a.go": "source_decode_failed"},
            (ImportResolution.UNSUPPORTED, "source_decode_failed"),
            id="decode-failure",
        ),
        pytest.param(
            {"internal/store/a.go": "package CLAUSE\n", "internal/store/open.go": _GO_OPEN},
            {"internal/store/a.go": "excluded"},
            (ImportResolution.EXCLUDED, "excluded"),
            id="excluded-member",
        ),
        pytest.param(
            {"internal/store/a.go": _GO_OPEN, "internal/store/b.go": "package CLAUSE\n"},
            {"internal/store/b.go": "ast_limit"},
            (ImportResolution.UNSUPPORTED, "ast_limit"),
            id="file-limit-after-a-read",
        ),
    ],
)
def test_refused_go_package_is_named_by_a_clause_that_can_be_read(
    members: dict[str, str],
    unavailable: dict[str, str],
    expected: tuple[ImportResolution, str],
    clause: str,
) -> None:
    # A clause that differs from the directory still binds the use, so the refusal is
    # reported on it: the same outcome as for a package named after its directory.
    sources = {
        "go.mod": _GO_MOD,
        "cmd/main.go": _GO_APP.replace("db.Open()", f"{clause}.Open()"),
        **{path: source.replace("CLAUSE", clause) for path, source in members.items()},
    }

    refs, provider = _resolve(sources, "cmd/main.go", "Run", unavailable=unavailable)

    assert [
        (ref.resolution, ref.reason, ref.resolved_symbols)
        for ref in refs
        if ref.module == "example.com/m/internal/store"
    ] == [(*expected, ())]
    # The first member with a readable clause names the package: later ones are not read.
    assert "internal/store/zz.go" not in provider.asked


def test_go_module_with_replace_is_not_resolved() -> None:
    assert go_module_path(_GO_MOD) == "example.com/m"
    assert go_module_path(_GO_MOD + "\nreplace example.com/x => ../x\n") is None
    assert go_module_path("go 1.22\n") is None


def test_go_module_path_reads_a_go_mod_of_blank_lines_in_linear_time() -> None:
    # Review #142: a `replace` pattern whose leading blanks crossed newlines was quadratic
    # on a `go.mod` of empty lines, on the shared event loop: 5 s for these 64,000 lines,
    # 90 s at 256 KiB.
    go_mod = "module example.com/m\n" + "\n" * 64_000
    started = time.monotonic()

    assert go_module_path(go_mod) == "example.com/m"
    assert go_module_path(go_mod + " \treplace example.com/x => ../x\n") is None
    assert time.monotonic() - started < 1.0


def test_excluded_python_target_is_reported_and_not_shown() -> None:
    sources = {
        "pkg/app.py": "from .client import Client\n\ndef f():\n    return Client()\n",
        "pkg/client.py": "class Client:\n    pass\n",
    }

    ref = _one(sources, "pkg/app.py", "f", unavailable={"pkg/client.py": "excluded"})

    assert (ref.resolution, ref.resolved_symbols) == (ImportResolution.EXCLUDED, ())


@pytest.mark.parametrize(
    ("importer", "specifier", "target"),
    [
        pytest.param("src/app.ts", "./models", "src/models.ts", id="ts"),
        pytest.param("src/app.js", "./models.js", "src/models.js", id="js"),
    ],
)
def test_excluded_script_target_is_reported_and_not_shown(
    importer: str, specifier: str, target: str
) -> None:
    sources = {
        importer: (
            f'import {{ User }} from "{specifier}";\n'
            "export function run() { return new User(); }\n"
        ),
        target: "export class User {}\n",
    }

    ref = _one(sources, importer, "run", unavailable={target: "excluded"})

    assert (ref.resolution, ref.reason, ref.resolved_symbols) == (
        ImportResolution.EXCLUDED,
        "excluded",
        (),
    )


_SCRIPT_IMPORTER = (
    'import { User } from "./models";\nexport function run() { return new User(); }\n'
)
_SCRIPT_ERROR_ELSEWHERE = "export class User {}\nexport function broken( { }\n"


@pytest.mark.parametrize(
    ("importer", "target", "name", "sources"),
    [
        pytest.param(
            "pkg/app.py",
            "pkg/models.py",
            "User",
            {
                "pkg/app.py": "from .models import User\n\ndef run():\n    return User()\n",
                "pkg/models.py": "class User:\n    pass\n\ndef broken(:\n    pass\n",
            },
            id="py",
        ),
        *(
            pytest.param(
                f"src/app{extension}",
                f"src/models{extension}",
                "User",
                {
                    f"src/app{extension}": _SCRIPT_IMPORTER,
                    f"src/models{extension}": _SCRIPT_ERROR_ELSEWHERE,
                },
                id=extension[1:],
            )
            for extension in (".ts", ".tsx", ".js", ".jsx")
        ),
        pytest.param(
            "cmd/main.go",
            "internal/store/open.go",
            "Open",
            {
                "go.mod": _GO_MOD,
                "cmd/main.go": (
                    'package main\n\nimport "example.com/m/internal/store"\n\n'
                    "func run() error {\n\treturn store.Open()\n}\n"
                ),
                "internal/store/open.go": (
                    "package store\n\nfunc Open() error { return nil }\n\nfunc Close( {\n}\n"
                ),
            },
            id="go",
        ),
    ],
)
def test_import_target_that_is_not_parsed_keeps_the_parse_reason(
    importer: str, target: str, name: str, sources: dict[str, str]
) -> None:
    # The error is outside the imported declaration: the whole file is refused anyway,
    # because an error region can hide a second declaration or export of the name.
    facts = _facts(target, sources[target])
    assert facts.has_errors is True
    assert [symbol.complete for symbol in facts.symbols if symbol.name == name] == [True]

    broken = _one(sources, importer, "run")
    timed_out = _one(sources, importer, "run", unavailable={target: "ast_timeout"})

    assert (broken.resolution, broken.reason, broken.resolved_symbols) == (
        ImportResolution.UNSUPPORTED,
        "ast_failed",
        (),
    )
    assert (timed_out.resolution, timed_out.reason) == (ImportResolution.UNSUPPORTED, "ast_timeout")


@pytest.mark.parametrize(
    ("importer", "target", "sources"),
    [
        pytest.param(
            "pkg/app.py",
            "pkg/models.py",
            {
                "pkg/app.py": "from .models import User\n\ndef run():\n    return User()\n",
                "pkg/models.py": "class User(:\n    pass\n",
            },
            id="py",
        ),
        pytest.param(
            "src/app.ts",
            "src/models.ts",
            {"src/app.ts": _SCRIPT_IMPORTER, "src/models.ts": "export class User { x = ; }\n"},
            id="ts",
        ),
    ],
)
def test_import_target_with_an_error_over_the_imported_symbol_is_not_resolved(
    importer: str, target: str, sources: dict[str, str]
) -> None:
    # Resolved, the incomplete symbol would only be withheld later as an L4 `ast_limit`.
    facts = _facts(target, sources[target])
    assert [symbol.complete for symbol in facts.symbols if symbol.name == "User"] == [False]

    ref = _one(sources, importer, "run")

    assert (ref.resolution, ref.reason, ref.resolved_symbols) == (
        ImportResolution.UNSUPPORTED,
        "ast_failed",
        (),
    )


def _many_exports(count: int) -> dict[str, str]:
    exports = "".join(f"export function f{n}() {{ return {n} }}\n" for n in range(count))
    calls = " + ".join(f"lib.f{n}()" for n in range(count))
    return {
        "src/app.ts": (
            f'import * as lib from "./lib";\nexport function run() {{ return {calls} }}\n'
        ),
        "src/lib.ts": exports,
    }


def test_one_import_cannot_bring_more_symbols_than_the_file_limit() -> None:
    refs, _ = _resolve(_many_exports(11), "src/app.ts", "run")

    shown, over = refs
    assert (shown.resolution, len(shown.resolved_symbols), len(shown.names)) == (
        ImportResolution.RESOLVED,
        10,
        10,
    )
    assert (over.resolution, over.reason, over.resolved_symbols) == (
        ImportResolution.UNSUPPORTED,
        "ast_limit",
        (),
    )
    assert len(over.names) == 1
    assert set(shown.names).isdisjoint(over.names)


def test_last_import_of_a_run_is_cut_to_the_remaining_run_limit() -> None:
    sources = _many_exports(8)
    provider = Provider(sources)
    tree = RevisionTree(_HEAD, {path: "b" * 40 for path in sources}, True)
    resolver = ImportResolver(tree=tree, provider=provider)
    resolver._symbols_in_run = 95
    facts = _facts("src/app.ts", sources["src/app.ts"])
    symbols = [item for item in facts.symbols if item.name == "run"]

    shown, over = asyncio.run(
        resolver.resolve(importer_path="src/app.ts", facts=facts, symbols=symbols)
    )

    assert len(shown.resolved_symbols) == 5
    assert (over.reason, len(over.names)) == ("ast_limit", 3)
    assert resolver._symbols_in_run == 100


# --- Public helpers shared with the graph indexer (#142) -------------------------------


def _tree(*paths: str) -> RevisionTree:
    return RevisionTree(_HEAD, dict.fromkeys(paths, "b" * 40), True)


def test_python_module_target_is_the_resolver_lookup_of_a_module_path() -> None:
    tree = _tree("pkg/models.py", "pkg/__init__.py", "src/util.py", "util.py", "lib.pyi")

    assert python_module_target("pkg/app.py", ".models", tree) == ModuleTarget(
        ImportResolution.RESOLVED, None, ("pkg/models.py",)
    )
    assert python_module_target("pkg/app.py", ".", tree) == ModuleTarget(
        ImportResolution.RESOLVED, None, ("pkg/__init__.py",)
    )
    assert python_module_target("pkg/app.py", "...x", tree) == ModuleTarget(
        ImportResolution.MISSING, "above_repository_root"
    )
    assert python_module_target("pkg/app.py", ".gone", tree) == ModuleTarget(
        ImportResolution.MISSING, "no_candidate"
    )
    assert python_module_target("app.py", "util", tree) == ModuleTarget(
        ImportResolution.AMBIGUOUS, "several_candidates"
    )
    assert python_module_target("app.py", "lib", tree) == ModuleTarget(
        ImportResolution.RESOLVED, None, ("lib.pyi",)
    )
    assert python_module_target("app.py", "requests", tree) == ModuleTarget(
        ImportResolution.EXTERNAL, "no_candidate"
    )


def test_script_module_target_keeps_every_reason_of_the_resolver() -> None:
    tree = _tree("src/x.ts", "src/y/index.tsx", "src/z.js", "src/z.mjs", "src/w.d.ts")

    def target(module: str, *, typescript: bool = True) -> ModuleTarget:
        return script_module_target("src/app.ts", module, tree, typescript=typescript)

    assert target("./x") == ModuleTarget(ImportResolution.RESOLVED, None, ("src/x.ts",))
    assert target("./x.ts") == ModuleTarget(ImportResolution.RESOLVED, None, ("src/x.ts",))
    assert target("./y") == ModuleTarget(ImportResolution.RESOLVED, None, ("src/y/index.tsx",))
    assert target("./w") == ModuleTarget(ImportResolution.RESOLVED, None, ("src/w.d.ts",))
    assert target("./x.js") == ModuleTarget(ImportResolution.UNSUPPORTED, "extension_not_resolved")
    assert target("@/x") == ModuleTarget(ImportResolution.UNSUPPORTED, "path_alias")
    assert target("/x") == ModuleTarget(ImportResolution.UNSUPPORTED, "path_alias")
    assert target("lodash") == ModuleTarget(ImportResolution.EXTERNAL, "package")
    assert target("../../x") == ModuleTarget(ImportResolution.MISSING, "above_repository_root")
    assert target("./gone") == ModuleTarget(ImportResolution.MISSING, "no_candidate")
    assert target("./z", typescript=False) == ModuleTarget(
        ImportResolution.AMBIGUOUS, "several_candidates"
    )


def test_go_package_target_lists_the_members_of_one_local_package() -> None:
    tree = _tree(
        "go.mod",
        "internal/store/open.go",
        "internal/store/close.go",
        "internal/store/open_test.go",
        "internal/store/_skip.go",
        "internal/empty/README.md",
        "internal/cfg/cfg_linux.go",
        "nested/go.mod",
        "nested/pkg/a.go",
    )
    module = "example.com/m"

    def target(path: str, go_module: str | None = module) -> ModuleTarget:
        return go_package_target(path, tree, go_module)

    assert target("example.com/m/internal/store") == ModuleTarget(
        ImportResolution.RESOLVED, None, ("internal/store/close.go", "internal/store/open.go")
    )
    assert target("fmt") == ModuleTarget(ImportResolution.EXTERNAL, "standard_library")
    assert target("example.com/other/x") == ModuleTarget(ImportResolution.EXTERNAL, "other_module")
    assert target("example.com/m/internal/store", None) == ModuleTarget(
        ImportResolution.UNSUPPORTED, "module_not_resolved"
    )
    assert target("example.com/m/nested/pkg") == ModuleTarget(
        ImportResolution.UNSUPPORTED, "nested_module"
    )
    assert target("example.com/m/internal/empty") == ModuleTarget(
        ImportResolution.MISSING, "no_package_files"
    )
    assert target("example.com/m/internal/cfg") == ModuleTarget(
        ImportResolution.UNSUPPORTED, "build_constrained"
    )
    workspace = RevisionTree(_HEAD, {**tree.blob_shas, "go.work": "c" * 40}, True)
    assert go_package_target("example.com/m/internal/store", workspace, module) == ModuleTarget(
        ImportResolution.UNSUPPORTED, "workspace_or_incomplete_tree"
    )


def test_go_package_target_reads_a_prebuilt_layout_as_it_reads_the_tree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tree = _tree(
        "go.mod",
        "main.go",
        "main_test.go",
        "internal/store/open.go",
        "internal/store/close.go",
        "internal/store/open_test.go",
        "internal/store/_skip.go",
        "internal/store/.hidden.go",
        "internal/store/sub/inner.go",
        "internal/storex/x.go",
        "internal/cfg/cfg_linux.go",
        "internal/empty/README.md",
        "nested/go.mod",
        "nested/pkg/a.go",
        "own/go.mod",
        "own/own.go",
    )
    module = "example.com/m"
    layout = go_layout(tree)
    resolved, unsupported = ImportResolution.RESOLVED, ImportResolution.UNSUPPORTED
    expected = {
        module: ModuleTarget(resolved, None, ("main.go",)),
        f"{module}/internal/store": ModuleTarget(
            resolved, None, ("internal/store/close.go", "internal/store/open.go")
        ),
        f"{module}/internal/store/sub": ModuleTarget(
            resolved, None, ("internal/store/sub/inner.go",)
        ),
        f"{module}/internal/storex": ModuleTarget(resolved, None, ("internal/storex/x.go",)),
        f"{module}/internal/cfg": ModuleTarget(unsupported, "build_constrained"),
        f"{module}/internal/empty": ModuleTarget(ImportResolution.MISSING, "no_package_files"),
        # A `go.mod` above the package directory, and one in it.
        f"{module}/nested/pkg": ModuleTarget(unsupported, "nested_module"),
        f"{module}/own": ModuleTarget(unsupported, "nested_module"),
        "fmt": ModuleTarget(ImportResolution.EXTERNAL, "standard_library"),
        "example.com/other": ModuleTarget(ImportResolution.EXTERNAL, "other_module"),
    }

    def no_layout(tree: RevisionTree) -> GoLayout:
        raise AssertionError("the review path walks the tree itself")

    # Without a layout, the review's two walks of the tree answer; none is built.
    monkeypatch.setattr(ast_resolution, "go_layout", no_layout)
    for path, target in expected.items():
        assert go_package_target(path, tree, module) == target, path
        assert go_package_target(path, tree, module, layout=layout) == target, path
    assert layout == GoLayout(
        members={
            "": ("main.go",),
            "internal/store/": ("internal/store/close.go", "internal/store/open.go"),
            "internal/store/sub/": ("internal/store/sub/inner.go",),
            "internal/storex/": ("internal/storex/x.go",),
            "internal/cfg/": ("internal/cfg/cfg_linux.go",),
            "nested/pkg/": ("nested/pkg/a.go",),
            "own/": ("own/own.go",),
        },
        module_prefixes=frozenset({"nested/", "own/"}),
    )
    # A workspace or an incomplete tree is refused before the layout is read.
    for other in (
        RevisionTree(_HEAD, {**tree.blob_shas, "go.work": "c" * 40}, True),
        RevisionTree(_HEAD, tree.blob_shas, False),
    ):
        assert go_package_target(
            f"{module}/internal/store", other, module, layout=layout
        ) == ModuleTarget(unsupported, "workspace_or_incomplete_tree")


def test_module_target_dispatches_by_the_importer_language() -> None:
    tree = _tree("pkg/x.py", "src/x.ts", "src/x.js", "internal/x/x.go")

    def target(importer: str, module: str, language: AstLanguage) -> ModuleTarget:
        return module_target(importer, module, language, tree, "example.com/m")

    resolved = ImportResolution.RESOLVED
    assert target("pkg/a.py", ".x", AstLanguage.PYTHON) == ModuleTarget(
        resolved, None, ("pkg/x.py",)
    )
    assert target("src/a.ts", "./x", AstLanguage.TYPESCRIPT) == ModuleTarget(
        resolved, None, ("src/x.ts",)
    )
    assert target("src/a.tsx", "./x", AstLanguage.TSX) == ModuleTarget(
        resolved, None, ("src/x.ts",)
    )
    assert target("src/a.js", "./x", AstLanguage.JAVASCRIPT) == ModuleTarget(
        resolved, None, ("src/x.js",)
    )
    assert target("cmd/a.go", "example.com/m/internal/x", AstLanguage.GO) == ModuleTarget(
        resolved, None, ("internal/x/x.go",)
    )


def test_python_submodule_files_follow_the_package_of_its_init() -> None:
    tree = _tree("pkg/__init__.py", "pkg/helpers.py", "pkg/helpers.pyi", "pkg/sub/__init__.py")

    assert python_submodule_files("pkg/__init__.py", "helpers", tree) == ["pkg/helpers.py"]
    assert python_submodule_files("pkg/__init__.py", "sub", tree) == ["pkg/sub/__init__.py"]
    assert python_submodule_files("pkg/__init__.py", "gone", tree) == []
    assert python_submodule_files("__init__.py", "pkg", tree) == ["pkg/__init__.py"]


def test_exported_symbols_follow_the_export_rules_of_each_language() -> None:
    python = _facts("a.py", "def f():\n    pass\n\nclass C:\n    def f(self):\n        pass\n")
    go = _facts("a.go", "package a\n\nfunc Open() {}\n\nfunc open() {}\n")
    script = _facts(
        "a.ts", "function local() {}\nexport { local as shown };\nexport default function () {}\n"
    )

    assert [(item.name, item.enclosing) for item in exported_symbols(python, "f")] == [("f", None)]
    assert [item.name for item in exported_symbols(go, "Open")] == ["Open"]
    assert exported_symbols(go, "open") == ()
    assert [item.name for item in exported_symbols(script, "shown")] == ["local"]
    assert [item.name for item in exported_symbols(script, "default")] == [None]
    assert exported_symbols(script, "local") == ()


def test_go_package_name_is_the_one_package_clause_or_the_path_tail() -> None:
    db = _facts("s/a.go", "package db\n")
    other = _facts("s/b.go", "package other\n")

    assert go_package_name("example.com/m/store", [db, db]) == "db"
    assert go_package_name("example.com/m/store", [db, other]) == "store"
    assert go_package_name("example.com/m/store", None) == "store"


def test_import_watch_lists_the_paths_that_decide_a_python_target() -> None:
    relative = import_watch("pkg/app.py", ".models", AstLanguage.PYTHON, None)
    assert relative == ImportWatch(
        frozenset({"pkg/models.py", "pkg/models/__init__.py", "pkg/models.pyi"}),
        frozenset({"pkg/models"}),
    )
    assert import_watch("app.py", ".", AstLanguage.PYTHON, None) == ImportWatch(
        frozenset({"__init__.py"}), frozenset({""})
    )
    assert import_watch("pkg/sub/app.py", "..", AstLanguage.PYTHON, None) == ImportWatch(
        frozenset({"pkg.py", "pkg/__init__.py", "pkg.pyi"}), frozenset({"pkg"})
    )
    assert import_watch("app.py", "a.b", AstLanguage.PYTHON, None) == ImportWatch(
        frozenset(
            {
                *("a/b.py", "a/b/__init__.py", "a/b.pyi"),
                *("src/a/b.py", "src/a/b/__init__.py", "src/a/b.pyi"),
            }
        ),
        frozenset({"a/b", "src/a/b"}),
    )
    assert import_watch("pkg/app.py", "...x", AstLanguage.PYTHON, None) == ImportWatch(
        frozenset(), frozenset()
    )
    package = import_watch("pkg/app.py", ".", AstLanguage.PYTHON, None)
    assert package.touched_by("pkg/helpers.py")
    assert package.touched_by("pkg/helpers.pyi")
    assert package.touched_by("pkg/helpers/__init__.py")
    assert not package.touched_by("pkg/helpers/inner.py")
    assert not package.touched_by("pkg/README.md")


def test_import_watch_lists_the_candidates_of_a_script_target() -> None:
    def watch(module: str, language: AstLanguage = AstLanguage.TYPESCRIPT) -> ImportWatch:
        return import_watch("src/app.ts", module, language, None)

    assert watch("./x") == ImportWatch(
        frozenset({"src/x.ts", "src/x.tsx", "src/x.d.ts", "src/x/index.ts", "src/x/index.tsx"}),
        frozenset(),
    )
    assert watch("./x", AstLanguage.JAVASCRIPT) == ImportWatch(
        frozenset(
            {"src/x.js", "src/x.jsx", "src/x.mjs", "src/x/index.js", "src/x/index.jsx"}
            | {"src/x/index.mjs"}
        ),
        frozenset(),
    )
    assert watch("./x.ts") == ImportWatch(frozenset({"src/x.ts"}), frozenset())
    for fixed in ("./x.js", "@/x", "lodash", "../../x"):
        assert watch(fixed) == ImportWatch(frozenset(), frozenset())


def test_import_watch_of_a_go_import_is_its_package_directory() -> None:
    def watch(module: str, go_module: str | None = "example.com/m") -> ImportWatch:
        return import_watch("cmd/main.go", module, AstLanguage.GO, go_module)

    assert watch("example.com/m/internal/store") == ImportWatch(
        frozenset(), frozenset({"internal/store"})
    )
    assert watch("example.com/m") == ImportWatch(frozenset(), frozenset({""}))
    for fixed in ("fmt", "example.com/other/x"):
        assert watch(fixed) == ImportWatch(frozenset(), frozenset())
    assert watch("example.com/m/x", None) == ImportWatch(frozenset(), frozenset())
    store = watch("example.com/m/internal/store")
    assert store.touched_by("internal/store/new.go")
    assert not store.touched_by("internal/store/sub/new.go")
    assert watch("example.com/m").touched_by("main.go")


_UNIVERSE = (
    *("pkg/models.py", "pkg/models/__init__.py", "pkg/models.pyi", "pkg/__init__.py"),
    *("pkg/helpers.py", "pkg/sub/x.py", "util.py", "util/__init__.py", "src/util.py"),
    *("src/util/__init__.py", "src/util.pyi", "__init__.py", "x.py", "other.py"),
    *("src/x.ts", "src/x.tsx", "src/x.d.ts", "src/x/index.ts", "src/x/index.tsx"),
    *("src/x.js", "src/x.jsx", "src/x.mjs", "src/x/index.js", "src/x/index.mjs", "src/y.ts"),
    *("internal/store/open.go", "internal/store/open_linux.go", "internal/store/open_test.go"),
    *("internal/store/_skip.go", "internal/store/sub/a.go", "main.go"),
    *("internal/go.mod", "go.work"),
)
_SPECIFIERS = (
    *(("pkg/app.py", ".models"), ("pkg/app.py", "."), ("pkg/sub/app.py", "..")),
    *(("app.py", "util"), ("app.py", "pkg.models"), ("app.py", "."), ("pkg/app.py", "...x")),
    *(("src/app.ts", "./x"), ("src/app.ts", "./x.ts"), ("src/app.ts", "./x.js")),
    *(("src/a/app.ts", "../x"), ("src/app.js", "./x"), ("src/app.ts", "@/x")),
    *(("cmd/main.go", "example.com/m/internal/store"), ("cmd/main.go", "example.com/m")),
    *(("cmd/main.go", "fmt"), ("cmd/main.go", "example.com/other/x")),
)


def test_a_path_outside_the_watch_never_changes_the_module_target() -> None:
    """Rule W of #142: the path set decides a target only through its watch."""
    rng = random.Random(142)
    bases = [frozenset(), frozenset(_UNIVERSE)]
    bases += [frozenset(rng.sample(_UNIVERSE, rng.randrange(len(_UNIVERSE)))) for _ in range(30)]
    for importer, module in _SPECIFIERS:
        language = language_for_path(importer)
        assert language is not None
        watch = import_watch(importer, module, language, "example.com/m")
        for base in bases:
            before = module_target(
                importer, module, language, _tree(*sorted(base)), "example.com/m"
            )
            for path in _UNIVERSE:
                if watch.touched_by(path) or posixpath.basename(path) in {"go.mod", "go.work"}:
                    continue
                toggled = _tree(*sorted(base ^ {path}))
                after = module_target(importer, module, language, toggled, "example.com/m")
                assert after == before, (importer, module, path)


def test_generated_and_binary_checks_are_public_with_the_private_aliases_kept() -> None:
    assert _is_generated is is_generated_path
    assert _is_binary is is_binary_content
    assert is_generated_path("web/package-lock.json")
    assert is_generated_path("dist/app.js")
    assert not is_generated_path("src/app.js")
    assert is_binary_content(b"\x89PNG\r\n\x1a\nrest")
    assert is_binary_content(b"text\x00more")
    assert not is_binary_content(b"print('hello')\n")
