"""Affected symbols and one-step import resolution: resolve exactly or keep a reason."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from uuid import UUID

from app.modules.reviews.application.ast_resolution import (
    FileUnavailable,
    ImportRef,
    ImportResolution,
    ImportResolver,
    RevisionTree,
    affected_symbols,
    go_module_path,
)
from app.modules.reviews.application.ast_symbols import AstFacts, language_for_path
from app.modules.reviews.application.diff_map import DiffFile, VcsProviderName, build_diff_map
from app.modules.reviews.application.prompt_builder import PullRequestMeta
from app.modules.reviews.application.vcs_diff import PullRequestLocator, VcsFile, VcsPullRequest
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


def _diff_file(patch: str, additions: int, deletions: int) -> DiffFile:
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
        "app.py", "modified", "d" * 40, None, additions, deletions, additions + deletions, patch
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
    assert refs == ()
    assert provider.asked == []


def test_unused_import_is_never_loaded() -> None:
    sources = {
        "pkg/app.py": "from .models import User\n\ndef f():\n    return 1\n",
        "pkg/models.py": "class User:\n    pass\n",
    }

    refs, provider = _resolve(sources, "pkg/app.py", "f")

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


def test_go_module_with_replace_is_not_resolved() -> None:
    assert go_module_path(_GO_MOD) == "example.com/m"
    assert go_module_path(_GO_MOD + "\nreplace example.com/x => ../x\n") is None
    assert go_module_path("go 1.22\n") is None


def test_excluded_python_target_is_reported_and_not_shown() -> None:
    sources = {
        "pkg/app.py": "from .client import Client\n\ndef f():\n    return Client()\n",
        "pkg/client.py": "class Client:\n    pass\n",
    }

    ref = _one(sources, "pkg/app.py", "f", unavailable={"pkg/client.py": "excluded"})

    assert (ref.resolution, ref.resolved_symbols) == (ImportResolution.EXCLUDED, ())
